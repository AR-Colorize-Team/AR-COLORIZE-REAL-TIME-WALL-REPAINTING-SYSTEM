"""
AI job dispatch.

API → SQS (standard queue + DLQ after 3 receives) → GPU worker on ECS/EC2 (g5/g6)
running the segmentation & recommendation engine (Part 3). Workers report back
through the authenticated internal callback endpoint.

Ordering: the ai_jobs row is committed *before* the message is sent, so a worker
can never receive a job id that doesn't exist yet. If sending fails after commit,
the reaper (`requeue_stale_jobs`, run every minute by EventBridge Scheduler)
re-sends anything still `queued` after 2 minutes — at-least-once delivery, and
workers are idempotent on job id.
"""
from __future__ import annotations

import json
import logging
import uuid
from functools import lru_cache

import boto3
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings

log = logging.getLogger("arwall.jobs")


@lru_cache
def _sqs():
    return boto3.client("sqs", region_name=get_settings().aws_region)


async def dispatch_job(job_id: uuid.UUID, job_type: str) -> bool:
    """Returns True if handed to SQS. Never raises — the reaper is the safety net."""
    s = get_settings()
    body = json.dumps({"job_id": str(job_id), "type": job_type, "v": 1})
    if not s.sqs_ai_queue_url:
        log.info("sqs_disabled_job_not_sent", extra={"job_id": str(job_id), "type": job_type})
        return False
    try:
        await run_in_threadpool(
            _sqs().send_message,
            QueueUrl=s.sqs_ai_queue_url,
            MessageBody=body,
            MessageAttributes={"type": {"DataType": "String", "StringValue": job_type}},
        )
        return True
    except Exception:  # noqa: BLE001 — logged; reaper retries
        log.exception("sqs_send_failed", extra={"job_id": str(job_id)})
        return False


async def requeue_stale_jobs(db) -> int:
    """
    Safety net, scheduled every minute: re-dispatch jobs stuck in `queued`, and
    fail jobs whose worker died mid-run (running > 10 min) after 3 attempts.
    """
    from sqlalchemy import text

    stale = (await db.execute(text("""
        SELECT id, type FROM ai_jobs
        WHERE status = 'queued' AND queued_at < now() - interval '2 minutes'
        ORDER BY queued_at LIMIT 200
        FOR UPDATE SKIP LOCKED
    """))).all()
    if stale:
        # Push the clock forward so a long queue backlog isn't re-sent every minute.
        await db.execute(text("UPDATE ai_jobs SET queued_at = now() WHERE id = ANY(:ids)"),
                         {"ids": [r[0] for r in stale]})
    await db.execute(text("""
        UPDATE ai_jobs SET status = CASE WHEN attempts >= 3 THEN 'failed'::ai_job_status
                                         ELSE 'queued'::ai_job_status END,
                           error = CASE WHEN attempts >= 3 THEN 'worker timeout' ELSE error END,
                           finished_at = CASE WHEN attempts >= 3 THEN now() ELSE finished_at END
        WHERE status = 'running' AND started_at < now() - interval '10 minutes'
    """))
    await db.commit()
    for job_id, job_type in stale:
        await dispatch_job(job_id, job_type)
    return len(stale)
