from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChangePointIn(BaseModel):
    kind: Literal["metric", "field"] = "metric"
    ref: str = Field(min_length=1, max_length=120)


class RefreshIn(BaseModel):
    period: str | None = Field(default=None, max_length=7, description="周期 YYYY-MM，默认当前业务月")
    trigger: Literal["manual", "schedule", "seed"] = "manual"
    change_points: list[ChangePointIn] = Field(min_length=1, max_length=50)


class RefreshPreviewIn(BaseModel):
    period: str | None = Field(default=None, max_length=7)
    change_points: list[ChangePointIn] = Field(min_length=1, max_length=50)
