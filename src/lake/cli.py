"""`lake` — the one entrypoint. systemd calls it, you call it, cron would too.

lake scrape worldbank_gdp
lake scrape-schedule daily        # what the systemd dispatcher runs
lake backfill bps_inflation --start 2024-01-01 --end 2026-06-01
lake status
lake retry
lake check-freshness
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta

import typer

from lake.core.exceptions import ConfigError, LakeError
from lake.core.logging import configure, get_logger
from lake.settings import get_settings

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="A small, durable data lake: scrapers -> NAS -> catalog -> Parquet.",
)
log = get_logger("lake.cli")


def _boot() -> None:
    settings = get_settings()
    configure(log_dir=settings.log_dir, level=settings.log_level, json=settings.log_json)


def _parse_date(value: str | None) -> date:
    if not value:
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from None


@app.callback()
def main() -> None:
    _boot()


# -- scraping -----------------------------------------------------------------


@app.command()
def scrape(
    source_id: str = typer.Argument(..., help="source_id from configs/sources.yaml"),
    logical_date: str = typer.Option(None, "--logical-date", "-d", help="YYYY-MM-DD"),
    force: bool = typer.Option(False, "--force", help="re-run even if this date already succeeded"),
    trigger: str = typer.Option("manual", hidden=True),
) -> None:
    """Run one scraper for one logical date."""
    from lake.registry import build_scraper

    target = _parse_date(logical_date)
    try:
        scraper = build_scraper(source_id)
        ctx = scraper.run(target, force=force, trigger=trigger)
    except ConfigError as exc:
        typer.secho(f"config error: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    except LakeError as exc:
        typer.secho(f"{type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:
        typer.secho(f"unhandled {type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc

    typer.secho(f"ok  {source_id}  {target}  run_id={ctx.short_id}", fg="green")


@app.command("scrape-schedule")
def scrape_schedule(
    schedule: str = typer.Argument(..., help="daily | weekly | monthly | yearly"),
    logical_date: str = typer.Option(None, "--logical-date", "-d"),
) -> None:
    """Run every enabled source on a schedule. This is what the systemd timers call.

    Sources run sequentially: a NUC has four cores and one NAS link, and a
    stampede helps nobody. One source failing does not stop the others; the
    command exits non-zero so systemd's OnFailure= still fires.
    """
    from lake.registry import build_scraper, sources_for_schedule

    target = _parse_date(logical_date)
    source_ids = sources_for_schedule(schedule)
    if not source_ids:
        typer.secho(f"no enabled sources with schedule={schedule!r}", fg="yellow")
        return

    failed: list[str] = []
    for source_id in source_ids:
        try:
            build_scraper(source_id).run(target, trigger="schedule")
            typer.secho(f"ok    {source_id}", fg="green")
        except Exception as exc:
            failed.append(source_id)
            typer.secho(f"FAIL  {source_id}: {type(exc).__name__}: {exc}", fg="red", err=True)

    typer.echo(f"\n{len(source_ids) - len(failed)}/{len(source_ids)} succeeded")
    if failed:
        raise typer.Exit(1)


@app.command()
def backfill(
    source_id: str,
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD, inclusive"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD, inclusive"),
    force: bool = typer.Option(False, "--force"),
    step_days: int = typer.Option(0, help="0 = infer from the source's schedule"),
) -> None:
    """Re-run a source across a date range. Already-successful dates are skipped."""
    from dateutil.relativedelta import relativedelta

    from lake.registry import build_scraper, get_source_config

    begin, finish = _parse_date(start), _parse_date(end)
    if begin > finish:
        raise typer.BadParameter("--start must not be after --end")

    schedule = get_source_config(source_id).get("schedule", "daily")
    scraper = build_scraper(source_id)

    def advance(d: date) -> date:
        if step_days:
            return d + timedelta(days=step_days)
        return {
            "daily": lambda x: x + timedelta(days=1),
            "weekly": lambda x: x + timedelta(weeks=1),
            "monthly": lambda x: x + relativedelta(months=1),
            "yearly": lambda x: x + relativedelta(years=1),
        }.get(schedule, lambda x: x + timedelta(days=1))(d)

    current, ok, failed = begin, 0, 0
    while current <= finish:
        try:
            scraper.run(current, force=force, trigger="backfill")
            ok += 1
            typer.secho(f"ok    {source_id}  {current}", fg="green")
        except Exception as exc:
            failed += 1
            typer.secho(f"FAIL  {source_id}  {current}: {exc}", fg="red", err=True)
        current = advance(current)

    typer.echo(f"\nbackfill complete: {ok} ok, {failed} failed")
    if failed:
        raise typer.Exit(1)


@app.command()
def retry(
    max_attempts: int = typer.Option(3, help="give up after this many attempts"),
    older_than_minutes: int = typer.Option(15, help="ignore runs that just failed"),
) -> None:
    """Cross-run retry. Driven by lake-retry.timer every 30 minutes.

    Only the latest attempt of each (source, logical_date) is eligible, and any
    date that has since succeeded is excluded — a manual re-run is never clobbered.
    """
    from lake.metadata.repo import MetadataRepo
    from lake.registry import build_scraper

    meta = MetadataRepo()
    candidates = meta.failed_runs_to_retry(max_attempts, older_than_minutes)
    if not candidates:
        typer.echo("nothing to retry")
        return

    failed = 0
    for row in candidates:
        source_id, logical_date = row["source_id"], row["logical_date"]
        attempt = meta.next_attempt(source_id, logical_date)
        typer.echo(f"retry {source_id} {logical_date} attempt={attempt}")
        try:
            build_scraper(source_id).run(logical_date, attempt=attempt, trigger="retry")
            typer.secho(f"ok    {source_id}  {logical_date}", fg="green")
        except Exception as exc:
            failed += 1
            typer.secho(f"FAIL  {source_id}  {logical_date}: {exc}", fg="red", err=True)

    if failed:
        raise typer.Exit(1)


# -- catalog ------------------------------------------------------------------


@app.command("sync-sources")
def sync_sources() -> None:
    """Push configs/sources.yaml into the catalog. Run after editing the YAML."""
    from lake.metadata.repo import MetadataRepo
    from lake.registry import load_sources

    meta = MetadataRepo()
    sources = load_sources()
    for cfg in sources.values():
        meta.upsert_source(cfg)
    typer.secho(f"synced {len(sources)} source(s)", fg="green")


@app.command()
def status(
    source_id: str = typer.Option(None, "--source-id", "-s"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Recent runs, newest first. The catalog is the source of truth, not the logs."""
    from lake.metadata.repo import MetadataRepo

    rows = MetadataRepo().recent_runs(source_id=source_id, limit=limit)
    if not rows:
        typer.echo("no runs recorded")
        return

    colour = {
        "success": "green",
        "failed": "red",
        "running": "yellow",
        "skipped_unchanged": "blue",
    }
    typer.echo(f"{'source':24} {'date':12} {'status':18} {'att':>3} {'files':>5} {'ms':>8}")
    typer.echo("-" * 76)
    for r in rows:
        status_value = r["status"].value if hasattr(r["status"], "value") else str(r["status"])
        typer.secho(
            f"{r['source_id']:24} {r['logical_date']!s:12} {status_value:18} "
            f"{r['attempt']:>3} {r['file_count']:>5} {r['duration_ms'] or 0:>8}",
            fg=colour.get(status_value),
        )


@app.command("check-freshness")
def check_freshness(
    alert: bool = typer.Option(True, "--alert/--no-alert"),
) -> None:
    """Alert on sources past their freshness SLA.

    This catches the failure that OnFailure= cannot see: a scraper that silently
    stopped being scheduled never fails, because it never runs.
    """
    from lake.metadata.repo import MetadataRepo
    from lake.ops.alerts import alert_stale_sources

    stale = MetadataRepo().stale_sources()
    if not stale:
        typer.secho("all sources fresh", fg="green")
        return

    for s in stale:
        hours = s.get("hours_since_success")
        age = f"{hours:.0f}h" if hours is not None else "never"
        typer.secho(f"STALE {s['source_id']:24} last success {age}", fg="red")

    if alert:
        alert_stale_sources(stale)
    raise typer.Exit(1)


@app.command()
def alert(
    source: str = typer.Option(..., "--source"),
    unit: str = typer.Option(None, "--unit"),
) -> None:
    """Send a failure alert. Invoked by systemd OnFailure=lake-alert@%i.service."""
    from lake.ops.alerts import alert_run_failed

    alert_run_failed(source, unit)


# -- transform ----------------------------------------------------------------


@app.command()
def transform(dataset_id: str = typer.Argument(..., help="e.g. gdp_annual")) -> None:
    """Rebuild a processed dataset from raw. Idempotent: rebuilds, never appends."""
    from lake.transform.runner import TRANSFORMS

    fn = TRANSFORMS.get(dataset_id)
    if fn is None:
        typer.secho(
            f"unknown dataset {dataset_id!r}. known: {', '.join(sorted(TRANSFORMS))}",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)

    result = fn()
    typer.secho(f"ok  {result['dataset_id']}  rows={result['rows']}", fg="green")


# -- serving API --------------------------------------------------------------

serve = typer.Typer(no_args_is_help=True, help="Read-only serving API for humans and AI.")
app.add_typer(serve, name="serve")


@serve.command("build")
def serve_build() -> None:
    """Materialise processed/*.parquet into the read-only serving replica.

    Run this after `lake transform`, and on a timer to keep the replica fresh.
    The replica is a DuckDB file on local SSD; the API reads only from it.
    """
    from lake.api.engine import build_replica

    counts = build_replica()
    for dataset_id, rows in counts.items():
        typer.secho(f"  {dataset_id:24} {rows:>12,} rows", fg="green")
    typer.secho(f"replica built: {len(counts)} table(s), {sum(counts.values()):,} rows", fg="green")


@serve.command("run")
def serve_run(
    host: str = typer.Option("127.0.0.1", help="bind address; keep it on localhost"),
    port: int = typer.Option(8000),
    reload: bool = typer.Option(False, "--reload", help="dev auto-reload"),
) -> None:
    """Start the API server.

    Bind to localhost and reach it over Tailscale or an authenticating proxy. The
    API is read-only, but the data is still yours.
    """
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(
            f"warning: binding to {host}. The API has no auth of its own — "
            "put it behind Tailscale or a proxy.",
            fg="yellow",
        )
    uvicorn.run("lake.api.app:app", host=host, port=port, reload=reload)


@serve.command("query")
def serve_query(
    sql: str = typer.Argument(..., help="a single read-only SELECT"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """Run one read-only query from the command line. Handy for a quick check."""
    from lake.api import engine
    from lake.api.sql_guard import UnsafeQuery, validate

    try:
        validated = validate(sql, connection=engine.serving())
    except UnsafeQuery as exc:
        typer.secho(f"rejected: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc

    result = engine.run_query(validated.sql, limit=limit)
    typer.echo(" | ".join(result["columns"]))
    typer.echo("-" * 60)
    for row in result["rows"]:
        typer.echo(" | ".join("" if v is None else str(v) for v in row))
    note = " (truncated)" if result["truncated"] else ""
    typer.secho(f"\n{result['row_count']} rows in {result['elapsed_ms']}ms{note}", fg="cyan")


# -- ops ----------------------------------------------------------------------


@app.command()
def sweep(
    staging_hours: int = typer.Option(24),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Nightly hygiene: clear stale staging, report quarantine and orphan run dirs."""
    from lake.ops.sweeper import check_quarantine, sweep_empty_run_dirs, sweep_staging

    staged = sweep_staging(staging_hours, dry_run=dry_run)
    typer.echo(f"staging: removed {staged['removed']} dir(s), freed {staged['freed_bytes']:,} B")

    quarantined = check_quarantine(alert=not dry_run)
    typer.echo(f"quarantine: {quarantined['failures']} failure record(s)")

    orphans = sweep_empty_run_dirs(dry_run=True)  # always report-only; evidence matters
    if orphans["orphans"]:
        typer.secho(f"orphan run dirs (no manifest): {orphans['orphans']}", fg="yellow")


@app.command()
def archive(
    dry_run: bool = typer.Option(True, "--dry-run/--apply"),
) -> None:
    """Roll cold raw partitions into tar.zst, per source-month."""
    from lake.ops.retention import archive_by_policy

    for r in archive_by_policy(dry_run=dry_run):
        typer.echo(f"{r['source_id']} {r['year']}-{r['month']:02d}: {r}")


@app.command()
def retention(
    apply: bool = typer.Option(False, "--apply", help="actually delete. Default is a dry run."),
) -> None:
    """Enforce raw retention policy. Soft-deletes in the catalog, then unlinks."""
    from lake.ops.retention import apply_retention

    report = apply_retention(apply=apply)
    for source_id, r in report["sources"].items():
        typer.echo(f"{source_id:24} {r['files']:>6} file(s)  {r['bytes']:>14,} B")

    verb = "deleted" if apply else "would delete"
    typer.secho(f"\n{verb} {report['total_bytes']:,} bytes", fg="yellow" if not apply else "red")


@app.command()
def doctor() -> None:
    """Preflight. Run this first when something is wrong."""
    from lake.core.storage import default_storage

    settings = get_settings()
    problems = 0

    typer.echo(f"env:        {settings.env}")
    typer.echo(f"nas_root:   {settings.nas_root}")
    typer.echo(f"staging:    {settings.staging_root}")

    try:
        default_storage().assert_mounted()
        typer.secho("nas:        mounted", fg="green")
    except LakeError as exc:
        typer.secho(f"nas:        {exc}", fg="red")
        problems += 1

    try:
        from lake.metadata.repo import MetadataRepo

        n = len(MetadataRepo().list_sources(enabled_only=False))
        typer.secho(f"database:   ok, {n} source(s) registered", fg="green")
    except Exception as exc:
        typer.secho(f"database:   {type(exc).__name__}: {exc}", fg="red")
        problems += 1

    try:
        from lake.registry import load_sources

        typer.secho(f"registry:   {len(load_sources())} source(s) in yaml", fg="green")
    except LakeError as exc:
        typer.secho(f"registry:   {exc}", fg="red")
        problems += 1

    if settings.alert_ntfy_url:
        typer.secho("alerting:   configured", fg="green")
    else:
        typer.secho("alerting:   LAKE_ALERT_NTFY_URL unset", fg="yellow")

    raise typer.Exit(1 if problems else 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
