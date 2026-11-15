"""指标刷新结果快照：每个指标最近一次被刷新的结果指纹与来源批次。

单独建表（而不是给 `metrics` 加列），启动时 `create_all` 会直接建出来，
对已有库无需手工迁移；报表/下游引用可以据这里判断自己拿的是不是新结果。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class MetricRefreshResult(Base):
    __tablename__ = "metric_refresh_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(
        ForeignKey("metrics.id", ondelete="CASCADE"), unique=True, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    signature: Mapped[str] = mapped_column(String(64), default="")
    source_refresh_no: Mapped[str] = mapped_column(String(40), default="", index=True)
    source_batch_id: Mapped[int] = mapped_column(Integer, default=0)
    wave_index: Mapped[int] = mapped_column(Integer, default=0)
    upstream: Mapped[dict] = mapped_column(JSON, default=dict)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
