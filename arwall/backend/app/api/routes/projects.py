"""
/projects — rooms being redesigned, their wall surfaces, and their media assets.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select, tuple_

from app.api.deps import DB, CurrentEntitlements, CurrentUser, load_asset, load_palette, load_project
from app.api.schemas import (AssetOut, DownloadUrlOut, Page, ProjectCreateIn, ProjectDetailOut,
                             ProjectOut, ProjectUpdateIn, UploadRequestIn, UploadTicketOut, WallOut,
                             WallsReplaceIn)
from app.core.config import get_settings
from app.core.errors import AppError, Conflict, NotFound
from app.db.models import PaintColor, Project, ProjectAsset, ProjectWall
from app.services import storage
from app.services.audit import audit
from app.services.entitlements import enforce_project_limit, enforce_storage_limit
from app.utils.pagination import decode_cursor, encode_cursor

router = APIRouter(prefix="/projects", tags=["projects"])
assets_router = APIRouter(prefix="/assets", tags=["assets"])


class UploadVerificationFailed(AppError):
    status_code, code, title = 422, "upload_verification_failed", "Upload could not be verified"


# --------------------------------------------------------------------------- #
# Projects CRUD
# --------------------------------------------------------------------------- #
@router.get("", response_model=Page)
async def list_projects(user: CurrentUser, db: DB,
                        cursor: str | None = None,
                        limit: int = Query(20, ge=1, le=100),
                        status_: str | None = Query(None, alias="status")):
    stmt = (select(Project)
            .where(Project.owner_user_id == user.id, Project.deleted_at.is_(None))
            .order_by(Project.updated_at.desc(), Project.id.desc())
            .limit(limit + 1))
    if status_:
        stmt = stmt.where(Project.status == status_)
    if cursor:
        ts, rid = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Project.updated_at, Project.id) < tuple_(ts, rid))
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return Page(items=[ProjectOut.model_validate(p) for p in rows],
                next_cursor=encode_cursor(rows[-1].updated_at, rows[-1].id) if has_more else None)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreateIn, user: CurrentUser, ent: CurrentEntitlements,
                         request: Request, db: DB):
    await enforce_project_limit(db, user.id, ent)
    project = Project(owner_user_id=user.id, organization_id=user.organization_id,
                      name=body.name, room_type=body.room_type)
    db.add(project)
    await db.flush()
    await audit(db, "project.created", actor_id=user.id, target_type="project",
                target_id=project.id, request=request)
    await db.commit()
    await db.refresh(project)
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectDetailOut)
async def get_project(project_id: uuid.UUID, user: CurrentUser, db: DB):
    project = await load_project(db, user, project_id)
    walls = (await db.execute(select(ProjectWall).where(ProjectWall.project_id == project.id)
                              .order_by(ProjectWall.sort_order))).scalars().all()
    assets = (await db.execute(select(ProjectAsset).where(ProjectAsset.project_id == project.id,
                                                          ProjectAsset.upload_status != "failed")
                               .order_by(ProjectAsset.created_at))).scalars().all()
    return ProjectDetailOut(**ProjectOut.model_validate(project).model_dump(),
                            walls=[WallOut.model_validate(w) for w in walls],
                            assets=[AssetOut.model_validate(a) for a in assets])


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: uuid.UUID, body: ProjectUpdateIn, user: CurrentUser, db: DB):
    project = await load_project(db, user, project_id, write=True, lock=True)
    data = body.model_dump(exclude_unset=True)
    if data.get("active_palette_id"):
        await load_palette(db, user, data["active_palette_id"])           # visibility check
    if data.get("cover_asset_id"):
        asset = await db.get(ProjectAsset, data["cover_asset_id"])
        if asset is None or asset.project_id != project.id:
            raise NotFound("Asset not found in this project")
    for field, value in data.items():
        setattr(project, field, value)
    await db.commit()
    await db.refresh(project)
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: uuid.UUID, user: CurrentUser, request: Request, db: DB):
    """Soft delete — restorable for 30 days; purge job removes rows + S3 objects."""
    project = await load_project(db, user, project_id, write=True, lock=True)
    project.deleted_at = datetime.now(timezone.utc)
    await audit(db, "project.deleted", actor_id=user.id, target_type="project",
                target_id=project.id, request=request)
    await db.commit()


# --------------------------------------------------------------------------- #
# Walls — the client saves the full wall set of a scene in one call
# --------------------------------------------------------------------------- #
@router.put("/{project_id}/walls", response_model=list[WallOut])
async def replace_walls(project_id: uuid.UUID, body: WallsReplaceIn, user: CurrentUser, db: DB):
    """
    Idempotent "save scene": walls with an `id` are updated, walls without are
    created, existing walls not in the payload are removed. Runs in one
    transaction with the project row locked, so concurrent saves serialise.
    """
    project = await load_project(db, user, project_id, write=True, lock=True)
    existing = {w.id: w for w in (await db.execute(
        select(ProjectWall).where(ProjectWall.project_id == project.id))).scalars()}

    # Validate referenced assets belong to this project and paints exist.
    asset_ids = {a for w in body.walls for a in (w.source_asset_id, w.mask_asset_id) if a}
    if asset_ids:
        found = set((await db.execute(select(ProjectAsset.id).where(
            ProjectAsset.id.in_(asset_ids), ProjectAsset.project_id == project.id))).scalars())
        if missing := asset_ids - found:
            raise NotFound(f"Assets not in project: {sorted(map(str, missing))}")
    paint_ids = {w.paint_color_id for w in body.walls if w.paint_color_id}
    if paint_ids:
        found = set((await db.execute(select(PaintColor.id).where(PaintColor.id.in_(paint_ids)))).scalars())
        if missing := paint_ids - found:
            raise NotFound(f"Unknown paint colours: {sorted(map(str, missing))}")

    keep: set[uuid.UUID] = set()
    for w in body.walls:
        data = w.model_dump(exclude={"id"})
        if w.id:
            wall = existing.get(w.id)
            if wall is None:
                raise NotFound(f"Wall {w.id} not found in project")
            for k, v in data.items():
                setattr(wall, k, v)
            keep.add(w.id)
        else:
            db.add(ProjectWall(project_id=project.id, **data))
    for wid, wall in existing.items():
        if wid not in keep:
            await db.delete(wall)

    project.updated_at = datetime.now(timezone.utc)   # bump for list ordering
    await db.commit()
    walls = (await db.execute(select(ProjectWall).where(ProjectWall.project_id == project.id)
                              .order_by(ProjectWall.sort_order))).scalars().all()
    return [WallOut.model_validate(w) for w in walls]


# --------------------------------------------------------------------------- #
# Assets — direct-to-S3 uploads
# --------------------------------------------------------------------------- #
@router.post("/{project_id}/assets", response_model=UploadTicketOut, status_code=status.HTTP_201_CREATED)
async def request_upload(project_id: uuid.UUID, body: UploadRequestIn, user: CurrentUser,
                         ent: CurrentEntitlements, db: DB):
    s = get_settings()
    max_bytes = s.s3_max_upload_mb * 1024 * 1024
    if body.size_bytes > max_bytes:
        raise UploadVerificationFailed(f"Files larger than {s.s3_max_upload_mb} MB are not accepted")

    project = await load_project(db, user, project_id, write=True)
    await enforce_storage_limit(db, user.id, ent, body.size_bytes)

    asset_id = uuid.uuid4()
    key = storage.build_asset_key(user.id, project.id, asset_id, body.kind, body.content_type)
    asset = ProjectAsset(id=asset_id, project_id=project.id, kind=body.kind, s3_key=key,
                         content_type=body.content_type, size_bytes=body.size_bytes,
                         width_px=body.width_px, height_px=body.height_px, sha256=body.sha256,
                         capture_meta=body.capture_meta)
    db.add(asset)
    await db.commit()
    await db.refresh(asset)

    # The declared size is the S3-enforced ceiling, so quota accounting can't be bypassed.
    ticket = storage.presign_upload(key, body.content_type, max_bytes=body.size_bytes)
    return UploadTicketOut(asset=AssetOut.model_validate(asset), upload_url=ticket.url,
                           upload_fields=ticket.fields, expires_in=ticket.expires_in)


@assets_router.post("/{asset_id}/complete", response_model=AssetOut)
async def complete_upload(asset_id: uuid.UUID, user: CurrentUser, db: DB):
    asset, project = await load_asset(db, user, asset_id, write=True)
    if asset.upload_status == "verified":
        return AssetOut.model_validate(asset)                 # idempotent
    if asset.upload_status != "pending":
        raise Conflict(f"Asset is {asset.upload_status}")

    info = await storage.head_object(asset.s3_key)
    if info is None:
        raise UploadVerificationFailed("Object not found in storage; upload it first")
    if info.content_type != asset.content_type:
        asset.upload_status = "failed"
        await db.commit()
        raise UploadVerificationFailed("Content type mismatch")

    asset.size_bytes = info.size_bytes            # replace declared size with actual
    asset.upload_status = "verified"
    if project.cover_asset_id is None and asset.kind == "room_photo":
        project.cover_asset_id = asset.id
    await db.commit()
    await db.refresh(asset)
    return AssetOut.model_validate(asset)


@assets_router.get("/{asset_id}/download-url", response_model=DownloadUrlOut)
async def asset_download_url(asset_id: uuid.UUID, user: CurrentUser, db: DB):
    asset, _ = await load_asset(db, user, asset_id)
    if asset.upload_status != "verified":
        raise Conflict("Asset is not available yet")
    ttl = 300
    return DownloadUrlOut(url=storage.presign_download(asset.s3_key, ttl), expires_in=ttl)
