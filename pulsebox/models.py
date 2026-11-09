from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

IssueStatus = Literal["OPEN", "RESOLVED", "REGRESSED"]
MonitorStatus = Literal["OK", "LATE", "ALERTED"]


def _ensure_utc(v: Any) -> Any:
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


UTCDateTime = Annotated[datetime, BeforeValidator(_ensure_utc)]


class Base(DeclarativeBase):
    """Declarative base class for pulsebox SQLAlchemy models."""


class IssueGroupRow(Base):
    __tablename__ = "issue_groups"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    service: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    exception_type: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN", index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_alerted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IssueEventRow(Base):
    __tablename__ = "issue_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False, default="production")
    exception_type: Mapped[str] = mapped_column(String(256), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stacktrace: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CronMonitorRow(Base):
    __tablename__ = "cron_monitors"

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    service: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OK", index=True)
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    consecutive_misses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    webhook_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ErrorEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    exception_type: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
    ]
    message: str = Field(default="")
    stacktrace: str = Field(default="")
    environment: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ] = "production"
    custom_fingerprint: str | None = Field(default=None, max_length=64)


class IssueGroupSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    fingerprint: str = Field(..., min_length=64, max_length=64)
    service: str
    exception_type: str
    title: str
    status: IssueStatus
    occurrence_count: int = Field(ge=1)
    first_seen_at: UTCDateTime
    last_seen_at: UTCDateTime
    last_alerted_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_timestamps(self) -> IssueGroupSnapshot:
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at cannot be earlier than first_seen_at")
        return self


class MonitorRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=128,
            pattern=r"^[a-zA-Z0-9_-]+$",
        ),
    ]
    service: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    interval_seconds: int = Field(..., gt=0)
    grace_seconds: int = Field(default=60, ge=0)
    webhook_url: str | None = Field(default=None, max_length=1024)


class MonitorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    slug: str
    service: str
    interval_seconds: int = Field(gt=0)
    grace_seconds: int = Field(ge=0)
    status: MonitorStatus
    last_ping_at: UTCDateTime | None = None
    next_due_at: UTCDateTime | None = None
    consecutive_misses: int = Field(ge=0)
    webhook_url: str | None = None
    updated_at: UTCDateTime
