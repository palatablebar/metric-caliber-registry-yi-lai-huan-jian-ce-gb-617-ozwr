"""月结刷新：一批变更点 -> 一条刷新记录 -> 若干依赖波次。

幂等：`refresh_batches.dedupe_key`（周期 + 归一后的变更点集合）唯一；
同一批变更重跑命中已有记录，已完成波次不重复执行，未完成波次可续跑。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class RefreshBatch(Base):
    __tablename__ = "refresh_batches"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_refresh_batches_dedupe"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    refresh_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    period: Mapped[str] = mapped_column(String(10), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    change_points: Mapped[list] = mapped_column(JSON, default=list)
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    log_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RefreshWave(Base):
    __tablename__ = "refresh_waves"
    __table_args__ = (UniqueConstraint("batch_id", "index", name="uq_refresh_waves_batch_index"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("refresh_batches.id", ondelete="CASCADE"), index=True
    )
    index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    metric_codes: Mapped[list] = mapped_column(JSON, default=list)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    reasons: Mapped[dict] = mapped_column(JSON, default=dict)
    explanation: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[list] = mapped_column(JSON, default=list)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
