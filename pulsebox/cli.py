from __future__ import annotations

import asyncio
import sys

import typer
import uvicorn

from pulsebox.app import create_app
from pulsebox.store import PulseStore
from pulsebox.watchdog import PulseWatchdog

app = typer.Typer(
    name="pulsebox",
    help="Lightweight error fingerprinting and cron dead man's switch daemon",
    no_args_is_help=True,
)


@app.command()
def server(
    host: str = typer.Option("127.0.0.1", help="Host address to bind"),
    port: int = typer.Option(8000, help="Port to bind"),
    db: str = typer.Option("sqlite:///pulsebox.db", help="SQLAlchemy database URL"),
    webhook_url: str | None = typer.Option(None, help="Default alert webhook URL"),
    enable_watchdog: bool = typer.Option(True, help="Enable background watchdog loop"),
) -> None:
    """Run the pulsebox API server."""
    store = PulseStore(database_url=db)
    watchdog = PulseWatchdog(store=store, default_webhook_url=webhook_url)
    api = create_app(
        store=store,
        watchdog=watchdog,
        enable_background_watchdog=enable_watchdog,
    )
    uvicorn.run(api, host=host, port=port)


@app.command()
def scan(
    db: str = typer.Option("sqlite:///pulsebox.db", help="SQLAlchemy database URL"),
) -> None:
    """Run an immediate scan for overdue cron monitors."""

    async def _scan() -> None:
        store = PulseStore(database_url=db)
        watchdog = PulseWatchdog(store=store)
        alerted = await watchdog.scan_monitors()
        if not alerted:
            print("All cron monitors are on schedule. No overdue monitors found.")
            return
        print(f"Alerted {len(alerted)} overdue cron monitors:")
        for m in alerted:
            print(f"  - {m.slug} (service={m.service}, misses={m.consecutive_misses})")

    asyncio.run(_scan())


@app.command()
def ping(
    slug: str = typer.Argument(..., help="Monitor slug identifier"),
    db: str = typer.Option("sqlite:///pulsebox.db", help="SQLAlchemy database URL"),
) -> None:
    """Record a check-in ping for a cron monitor."""

    async def _ping() -> None:
        store = PulseStore(database_url=db)
        try:
            mon = await store.record_ping(slug=slug)
            print(
                f"Check-in recorded for {mon.slug}. "
                f"Status={mon.status}, next due at {mon.next_due_at}"
            )
        except KeyError as err:
            print(f"Error: {err}", file=sys.stderr)
            raise typer.Exit(code=1) from err

    asyncio.run(_ping())


@app.command()
def issues(
    db: str = typer.Option("sqlite:///pulsebox.db", help="SQLAlchemy database URL"),
    service: str | None = typer.Option(None, help="Filter issues by service name"),
) -> None:
    """List tracked error issues."""

    async def _issues() -> None:
        store = PulseStore(database_url=db)
        issue_list = await store.list_issues(service=service)
        if not issue_list:
            print("No error issues found.")
            return
        print(f"Found {len(issue_list)} issue groups:")
        for iss in issue_list:
            print(
                f"  [{iss.status}] {iss.fingerprint[:12]}.. {iss.service} "
                f"({iss.occurrence_count} occurrences) - {iss.title}"
            )

    asyncio.run(_issues())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
