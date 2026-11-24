"""
/ai — enqueue wall segmentation and palette recommendation jobs; poll results.
/internal/ai — worker callbacks (not exposed publicly).

Jobs are asynchronous: GPU segmentation takes 1–4 s and a Vision-LLM call
5–20 s — too long to hold an HTTP request on mobile networks. The client polls
GET /ai/jobs/{id} (or receives an FCM push when the job finishes).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy import select

from app.api.deps import DB, CurrentEntitlements, CurrentUser, load_asset, load_project, require_internal_service
from app.api.schemas import AIJobOut, JobResultIn, RecommendIn, SegmentIn
from app.core.errors import Conflict, NotFound
from app.db.models import AIJob, PaintColor, Palette, PaletteColor, Project, ProjectAsset, ProjectWall
from app.services.entitlements import consume_quota, current_period_start, refund_quota
from app.services.jobs import dispatch_job
from app.utils.color import normalize_hex

router = APIRouter(tags=["ai"])
internal_router = APIRouter(prefix="/internal/ai", tags=["internal"],
                            dependencies=[Depends(require_internal_service)], include_in_schema=False)

JOB_METRIC = {"wall_segmentation": "segmentations", "palette_recommendation": "ai_recommendations"}


async def _enqueue(db, *, user, project: Project, job_type: str, payload: dict,
                   ent, idempotency_key: str | None) -> AIJob:
    # Idempotency: a retried request (flaky mobile network) returns the same job
    # instead of charging quota twice.
    if idempotency_key:
        existing = (await db.execute(select(AIJob).where(
            AIJob.user_id == user.id, AIJob.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing:
            return existing

    await consume_quota(db, user.id, ent, JOB_METRIC[job_type])
    job = AIJob(user_id=user.id, project_id=project.id, type=job_type, input=payload,
                idempotency_key=idempotency_key)
    db.add(job)
    if job_type == "wall_segmentation":
        project.status = "processing"
    await db.commit()                       # commit BEFORE dispatch (see services/jobs.py)
    await db.refresh(job)
    await dispatch_job(job.id, job_type)
    return job


@router.post("/projects/{project_id}/ai/segment", response_model=AIJobOut,
             status_code=status.HTTP_202_ACCEPTED)
async def segment_walls(project_id: uuid.UUID, body: SegmentIn, user: CurrentUser,
                        ent: CurrentEntitlements, db: DB,
                        idempotency_key: str | None = Header(None, max_length=100)):
    project = await load_project(db, user, project_id, write=True, lock=True)
    asset, _ = await load_asset(db, user, body.asset_id)
    if asset.project_id != project.id or asset.kind not in ("room_photo", "ar_snapshot"):
        raise NotFound("Room photo not found in this project")
    if asset.upload_status != "verified":
        raise Conflict("Upload not completed")
    job = await _enqueue(db, user=user, project=project, job_type="wall_segmentation",
                         payload={"asset_id": str(asset.id), "s3_key": asset.s3_key,
                                  "exclude_classes": body.exclude_classes},
                         ent=ent, idempotency_key=idempotency_key)
    return AIJobOut.model_validate(job)


@router.post("/projects/{project_id}/ai/recommend-palettes", response_model=AIJobOut,
             status_code=status.HTTP_202_ACCEPTED)
async def recommend_palettes(project_id: uuid.UUID, body: RecommendIn, user: CurrentUser,
                             ent: CurrentEntitlements, db: DB,
                             idempotency_key: str | None = Header(None, max_length=100)):
    project = await load_project(db, user, project_id, write=True, lock=True)
    asset, _ = await load_asset(db, user, body.asset_id)
    if asset.project_id != project.id or asset.upload_status != "verified":
        raise NotFound("Verified room photo not found in this project")
    job = await _enqueue(db, user=user, project=project, job_type="palette_recommendation",
                         payload={"asset_id": str(asset.id), "s3_key": asset.s3_key,
                                  "room_type": project.room_type,
                                  "style_preferences": body.style_preferences, "mood": body.mood,
                                  "brand_ids": [str(b) for b in body.brand_ids],
                                  "organization_id": str(user.organization_id) if user.organization_id else None,
                                  "palette_count": body.palette_count},
                         ent=ent, idempotency_key=idempotency_key)
    return AIJobOut.model_validate(job)


@router.get("/ai/jobs/{job_id}", response_model=AIJobOut)
async def get_job(job_id: uuid.UUID, user: CurrentUser, db: DB):
    job = await db.get(AIJob, job_id)
    if job is None or job.user_id != user.id:
        raise NotFound("Job not found")
    return AIJobOut.model_validate(job)


# --------------------------------------------------------------------------- #
# Worker callback
# --------------------------------------------------------------------------- #
@internal_router.post("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def job_callback(job_id: uuid.UUID, body: JobResultIn, db: DB):
    job = (await db.execute(select(AIJob).where(AIJob.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None:
        raise NotFound("Job not found")
    if job.status in ("succeeded", "failed", "canceled"):
        return  # terminal; duplicate delivery from SQS is a no-op

    now = datetime.now(timezone.utc)
    if body.status == "running":
        job.status, job.started_at = "running", now
        job.attempts += 1
        await db.commit()
        return

    job.status, job.finished_at = body.status, now
    job.output, job.error = body.output, body.error
    job.model_version, job.cost_micros = body.model_version, body.cost_micros
    project = await db.get(Project, job.project_id) if job.project_id else None

    if body.status == "failed":
        await refund_quota(db, job.user_id, JOB_METRIC[job.type], current_period_start(job.queued_at))
        if project and project.status == "processing":
            project.status = "draft"
    elif job.type == "wall_segmentation" and project:
        await _persist_segmentation(db, job, project, body.output or {})
        project.status = "ready"
    elif job.type == "palette_recommendation" and project and body.output:
        await _persist_ai_palettes(db, job, project, body.output)

    await db.commit()
    # TODO: send FCM push "Your room analysis is ready".


async def _persist_segmentation(db, job: AIJob, project: Project, output: dict) -> None:
    """
    Worker output contract (Part 3 produces this):
      {"walls": [{"label": "Wall 1", "polygon": [[x,y],...], "confidence": 0.93,
                  "area_m2": 11.4, "mask_s3_key": "users/.../wall_mask/<id>.png",
                  "mask_bytes": 48211}], "image_width": 4032, "image_height": 3024}
    Replaces previously detected (non-AR) walls for the same source photo.
    """
    source_id = uuid.UUID(job.input["asset_id"])
    old = (await db.execute(select(ProjectWall).where(
        ProjectWall.project_id == project.id, ProjectWall.source_asset_id == source_id,
        ProjectWall.ar_anchor.is_(None)))).scalars().all()
    for w in old:
        await db.delete(w)

    key_prefix = f"users/{job.user_id}/projects/{project.id}/wall_mask/"
    for order, w in enumerate(output.get("walls", [])[:50]):
        mask_asset = None
        mask_key = w.get("mask_s3_key")
        if mask_key and mask_key.startswith(key_prefix):      # worker may only write inside the tenant prefix
            mask_asset = ProjectAsset(project_id=project.id, kind="wall_mask", s3_key=mask_key,
                                      content_type="image/png", size_bytes=w.get("mask_bytes"),
                                      width_px=output.get("image_width"), height_px=output.get("image_height"),
                                      upload_status="verified", parent_asset_id=source_id)
            db.add(mask_asset)
            await db.flush()
        db.add(ProjectWall(project_id=project.id, source_asset_id=source_id,
                           mask_asset_id=mask_asset.id if mask_asset else None,
                           label=str(w.get("label", f"Wall {order + 1}"))[:60],
                           polygon=w.get("polygon"), area_m2=w.get("area_m2"),
                           confidence=w.get("confidence"), sort_order=order))


async def _persist_ai_palettes(db, job: AIJob, project: Project, output: dict) -> None:
    """Save each recommended palette so the user can apply it in AR immediately."""
    if analysis := output.get("room_analysis"):
        project.room_analysis = analysis
    # Only keep paint references that really exist (model/worker output is untrusted).
    candidate_ids = set()
    for rec in output.get("palettes", [])[:5]:
        for c in rec.get("colors", [])[:12]:
            try:
                candidate_ids.add(uuid.UUID(str(c.get("paint_color_id"))))
            except ValueError:
                pass
    valid_paints = set((await db.execute(
        select(PaintColor.id).where(PaintColor.id.in_(candidate_ids)))).scalars()) if candidate_ids else set()

    saved_ids = []
    for rec in output.get("palettes", [])[:5]:
        palette = Palette(owner_user_id=job.user_id, organization_id=project.organization_id,
                          name=str(rec.get("name", "AI palette"))[:80], source="ai",
                          description=rec.get("description"), ai_rationale=rec.get("rationale"),
                          mood_tags=[str(t).lower()[:30] for t in rec.get("mood_tags", [])][:10])
        for pos, c in enumerate(rec.get("colors", [])[:12]):
            try:
                hex_ = normalize_hex(c["hex"])
            except (KeyError, ValueError):
                continue                       # never trust model output blindly
            palette.colors.append(PaletteColor(position=pos, hex=hex_, role=c.get("role"),
                                               label=c.get("label"),
                                               paint_color_id=_valid_paint(c, valid_paints)))
        if palette.colors:
            db.add(palette)
            await db.flush()
            saved_ids.append(str(palette.id))
    job.output = {**(job.output or {}), "saved_palette_ids": saved_ids}


def _valid_paint(color: dict, valid: set[uuid.UUID]) -> uuid.UUID | None:
    try:
        pid = uuid.UUID(str(color.get("paint_color_id")))
    except ValueError:
        return None
    return pid if pid in valid else None
