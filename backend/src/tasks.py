"""Celery 任务：心跳 + 每日 07:40 自动跑一次口径校验（解析 → 依赖图 → 环/冲突）。"""
from __future__ import annotations

import time

from src.config.celery_app import TASK_DAILY, TASK_HEARTBEAT, TASK_MONTHLY_REFRESH, celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat() -> str:
    try:
        _redis().set(f"{settings.app_name}:worker:heartbeat", time.time())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
        return "error"


@celery_app.task(name=TASK_DAILY)
def daily_refresh() -> dict:
    from src.config.database import session_scope
    from src.services import audit_service, run_service

    operator = "scheduler@" + settings.app_name + ".io"
    with session_scope() as db:
        run = run_service.create_and_execute(db, trigger="schedule", operator_email=operator)
        audit_service.record(
            db,
            actor_email=operator,
            actor_role="admin",
            action="run.create",
            entity="run",
            entity_id=run.id,
            detail={"trigger": "schedule", "run_no": run.run_no, "issues": run.output_count},
            layer="worker",
        )
    logger.info("daily metric validation finished: run=%s issues=%s", run.run_no, run.output_count)
    return {"run_no": run.run_no, "issues": run.output_count}


@celery_app.task(name=TASK_MONTHLY_REFRESH)
def monthly_refresh() -> dict:
    """月结刷新：按月上中旬的变更单目标展开下游闭包并按波次重算，同一批变更幂等。"""
    from src.config.database import session_scope
    from src.constants.enums import ChangeKind
    from src.models.change_request import ChangeRequest
    from sqlalchemy import select

    from src.services import audit_service, refresh_service

    operator = "scheduler@" + settings.app_name + ".io"
    with session_scope() as db:
        approved = db.scalars(
            select(ChangeRequest)
            .where(
                ChangeRequest.status == "approved",
                ChangeRequest.change_kind.in_(
                    [ChangeKind.CALIBER.value, ChangeKind.FORMULA.value, ChangeKind.FIELD_TYPE.value]
                ),
            )
            .order_by(ChangeRequest.id)
        ).all()
        points = []
        for change in approved:
            if change.change_kind == ChangeKind.FIELD_TYPE.value:
                points.append({"kind": "field", "ref": change.target_ref})
            else:
                points.append({"kind": "metric", "ref": change.target_ref})
        if not points:
            logger.info("monthly refresh skipped: no approved change requests")
            return {"skipped": True, "reason": "no approved change requests"}
        batch, created = refresh_service.refresh(
            db,
            period=None,
            change_points=points,
            trigger="schedule",
            operator_email=operator,
        )
        audit_service.record(
            db,
            actor_email=operator,
            actor_role="admin",
            action="refresh.create" if created else "refresh.reuse",
            entity="refresh_batch",
            entity_id=batch.id,
            detail={"refresh_no": batch.refresh_no, "new_record": created, "status": batch.status},
            layer="worker",
        )
    logger.info("monthly refresh finished: batch=%s status=%s", batch.refresh_no, batch.status)
    return {"refresh_no": batch.refresh_no, "created": created, "status": batch.status}
