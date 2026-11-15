from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import REFRESH_CREATE_ROLES
from src.controllers import refresh_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.refresh import RefreshIn, RefreshPreviewIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/refreshes", tags=["refreshes"])


@router.get("")
def list_refreshes(
    status: str | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return refresh_controller.list_refreshes(db, status=status, limit=page.limit, offset=page.offset)


@router.get("/options")
def change_options(
    db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return refresh_controller.change_options(db)


@router.post("/preview")
def preview_refresh(
    payload: RefreshPreviewIn,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REFRESH_CREATE_ROLES)),
) -> dict:
    return refresh_controller.preview(db, payload.model_dump())


@router.post("", status_code=201)
def create_refresh(
    payload: RefreshIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REFRESH_CREATE_ROLES)),
) -> dict:
    return refresh_controller.create_refresh(db, payload.model_dump(), principal, request)


@router.get("/{batch_id}")
def get_refresh(
    batch_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return refresh_controller.get_refresh(db, batch_id)
