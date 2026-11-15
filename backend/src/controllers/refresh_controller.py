"""月结刷新控制器：预览计划 / 执行刷新 / 列表与详情。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.constants.enums import TriggerKind
from src.services import audit_service, refresh_service


def preview(db: Session, payload: dict) -> dict:
    plan = refresh_service.plan_for(db, payload.get("change_points"))
    plan["period"] = refresh_service.normalize_period(payload.get("period"))
    return plan


def create_refresh(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    batch, created = refresh_service.refresh(
        db,
        period=payload.get("period"),
        change_points=payload.get("change_points"),
        trigger=payload.get("trigger") or TriggerKind.MANUAL.value,
        operator_email=principal["email"],
    )
    action = "refresh.create" if created else "refresh.reuse"
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action=action,
        entity="refresh_batch",
        entity_id=batch.id,
        detail={
            "refresh_no": batch.refresh_no,
            "period": batch.period,
            "status": batch.status,
            "waves": (batch.metrics or {}).get("wave_count", 0),
            "isolated": (batch.metrics or {}).get("isolated_count", 0),
            "new_record": created,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return {
        "created": created,
        "reused": not created,
        "refresh": refresh_service.get_batch(db, batch.id),
    }


def list_refreshes(db: Session, **filters) -> dict:
    return refresh_service.list_batches(db, **filters)


def change_options(db: Session) -> dict:
    return refresh_service.change_options(db)


def get_refresh(db: Session, batch_id: int) -> dict:
    return refresh_service.get_batch(db, batch_id)
