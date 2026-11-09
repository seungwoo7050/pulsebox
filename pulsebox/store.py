from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TypeVar

from sqlalchemy import create_engine, desc, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pulsebox.fingerprint import compute_fingerprint
from pulsebox.models import (
    Base,
    CronMonitorRow,
    ErrorEventRequest,
    IssueEventRow,
    IssueGroupRow,
    IssueGroupSnapshot,
    MonitorRegisterRequest,
    MonitorSnapshot,
)

T = TypeVar("T")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_tz(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


_row_to_issue_snapshot = IssueGroupSnapshot.model_validate
_row_to_monitor_snapshot = MonitorSnapshot.model_validate


class PulseStore:
    """Repository managing issue groups and cron monitors with unified async execution."""

    def __init__(self, database_url: str = "sqlite:///:memory:") -> None:
        self._lock = threading.Lock()
        connect_args: dict[str, Any] = {}
        engine_kwargs: dict[str, Any] = {}

        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            if ":memory:" in database_url:
                engine_kwargs["poolclass"] = StaticPool

        if connect_args:
            engine_kwargs["connect_args"] = connect_args

        self.engine = create_engine(database_url, **engine_kwargs)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def _run_sync(self, fn: Callable[[Session], T]) -> T:
        with self._lock, self.session_factory() as session, session.begin():
            return fn(session)

    async def _execute(self, fn: Callable[[Session], T]) -> T:
        return await asyncio.to_thread(self._run_sync, fn)

    async def ingest_event(
        self, req: ErrorEventRequest, alert_cooldown_seconds: int = 300
    ) -> tuple[IssueGroupSnapshot, bool]:
        now = _utc_now()
        fp = compute_fingerprint(
            service=req.service,
            exception_type=req.exception_type,
            stacktrace=req.stacktrace,
            custom_fingerprint=req.custom_fingerprint,
        )
        msg = req.message.strip()
        title = f"{req.exception_type}: {msg}"[:512] if msg else req.exception_type[:512]

        def _op(session: Session) -> tuple[IssueGroupSnapshot, bool]:
            stmt = select(IssueGroupRow).where(IssueGroupRow.fingerprint == fp).with_for_update()
            group = session.scalars(stmt).first()

            if group is None:
                group = IssueGroupRow(
                    fingerprint=fp,
                    service=req.service,
                    exception_type=req.exception_type,
                    title=title,
                    status="OPEN",
                    occurrence_count=1,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_alerted_at=now,
                )
                session.add(group)
                alert = True
            elif group.status == "RESOLVED":
                group.status = "REGRESSED"
                group.occurrence_count += 1
                group.last_seen_at = now
                group.last_alerted_at = now
                alert = True
            else:
                group.occurrence_count += 1
                group.last_seen_at = now
                last_alerted = _ensure_tz(group.last_alerted_at)
                elapsed = (now - last_alerted).total_seconds() if last_alerted else None
                if elapsed is None or elapsed >= alert_cooldown_seconds:
                    group.last_alerted_at = now
                    alert = True
                else:
                    alert = False

            session.add(
                IssueEventRow(
                    fingerprint=fp,
                    service=req.service,
                    environment=req.environment,
                    exception_type=req.exception_type,
                    message=req.message,
                    stacktrace=req.stacktrace,
                    created_at=now,
                )
            )
            session.flush()
            return IssueGroupSnapshot.model_validate(group), alert

        return await self._execute(_op)

    async def resolve_issue(self, fingerprint: str) -> IssueGroupSnapshot:
        def _op(session: Session) -> IssueGroupSnapshot:
            group = session.get(IssueGroupRow, fingerprint)
            if group is None:
                raise KeyError(f"Issue group with fingerprint {fingerprint} not found")
            group.status = "RESOLVED"
            session.flush()
            return IssueGroupSnapshot.model_validate(group)

        return await self._execute(_op)

    async def list_issues(
        self, service: str | None = None, status: str | None = None
    ) -> list[IssueGroupSnapshot]:
        def _op(session: Session) -> list[IssueGroupSnapshot]:
            stmt = select(IssueGroupRow).order_by(desc(IssueGroupRow.last_seen_at))
            if service:
                stmt = stmt.where(IssueGroupRow.service == service)
            if status:
                stmt = stmt.where(IssueGroupRow.status == status)
            return [IssueGroupSnapshot.model_validate(r) for r in session.scalars(stmt).all()]

        return await self._execute(_op)

    async def get_issue(self, fingerprint: str) -> IssueGroupSnapshot | None:
        def _op(session: Session) -> IssueGroupSnapshot | None:
            row = session.get(IssueGroupRow, fingerprint)
            return IssueGroupSnapshot.model_validate(row) if row else None

        return await self._execute(_op)

    async def register_monitor(
        self, req: MonitorRegisterRequest, now: datetime | None = None
    ) -> MonitorSnapshot:
        current_time = _utc_now() if now is None else _ensure_tz(now)
        assert current_time is not None
        initial_due = current_time + timedelta(seconds=req.interval_seconds + req.grace_seconds)

        def _op(session: Session) -> MonitorSnapshot:
            row = session.get(CronMonitorRow, req.slug)
            if row is None:
                row = CronMonitorRow(
                    slug=req.slug,
                    service=req.service,
                    interval_seconds=req.interval_seconds,
                    grace_seconds=req.grace_seconds,
                    status="OK",
                    last_ping_at=None,
                    next_due_at=initial_due,
                    consecutive_misses=0,
                    webhook_url=req.webhook_url,
                    updated_at=current_time,
                )
                session.add(row)
            else:
                row.service = req.service
                row.interval_seconds = req.interval_seconds
                row.grace_seconds = req.grace_seconds
                row.webhook_url = req.webhook_url
                row.updated_at = current_time
                if row.next_due_at is None:
                    row.next_due_at = initial_due

            session.flush()
            return MonitorSnapshot.model_validate(row)

        return await self._execute(_op)

    async def record_ping(self, slug: str, now: datetime | None = None) -> MonitorSnapshot:
        current_time = _utc_now() if now is None else _ensure_tz(now)
        assert current_time is not None

        def _op(session: Session) -> MonitorSnapshot:
            row = session.get(CronMonitorRow, slug)
            if row is None:
                raise KeyError(f"Cron monitor with slug {slug} not found")

            row.status = "OK"
            row.last_ping_at = current_time
            row.next_due_at = current_time + timedelta(
                seconds=row.interval_seconds + row.grace_seconds
            )
            row.consecutive_misses = 0
            row.updated_at = current_time
            session.flush()
            return MonitorSnapshot.model_validate(row)

        return await self._execute(_op)

    async def evaluate_overdue_monitors(self, now: datetime | None = None) -> list[MonitorSnapshot]:
        current_time = _utc_now() if now is None else _ensure_tz(now)
        assert current_time is not None

        def _op(session: Session) -> list[MonitorSnapshot]:
            stmt = select(CronMonitorRow).where(
                CronMonitorRow.next_due_at <= current_time,
                CronMonitorRow.status != "ALERTED",
            )
            alerted: list[MonitorSnapshot] = []
            for row in session.scalars(stmt).all():
                row.status = "ALERTED"
                row.consecutive_misses += 1
                row.updated_at = current_time
                alerted.append(MonitorSnapshot.model_validate(row))
            session.flush()
            return alerted

        return await self._execute(_op)

    async def get_monitor(self, slug: str) -> MonitorSnapshot | None:
        def _op(session: Session) -> MonitorSnapshot | None:
            row = session.get(CronMonitorRow, slug)
            return MonitorSnapshot.model_validate(row) if row else None

        return await self._execute(_op)

    async def list_monitors(self) -> list[MonitorSnapshot]:
        def _op(session: Session) -> list[MonitorSnapshot]:
            stmt = select(CronMonitorRow).order_by(CronMonitorRow.slug.asc())
            return [MonitorSnapshot.model_validate(r) for r in session.scalars(stmt).all()]

        return await self._execute(_op)
