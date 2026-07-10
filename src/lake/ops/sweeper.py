"""Nightly hygiene: clear stale staging, notice a filling quarantine.

Quarantine that grows and nobody looks at is the same as no validation at all.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from lake.core.logging import get_logger
from lake.ops.alerts import notify
from lake.settings import get_settings

log = get_logger(__name__)


def sweep_staging(older_than_hours: int = 24, dry_run: bool = False) -> dict:
    """Delete abandoned staging dirs. A crashed run leaves bytes on the NUC's SSD."""
    settings = get_settings()
    cutoff = time.time() - older_than_hours * 3600
    removed, freed = [], 0

    if not settings.staging_root.is_dir():
        return {"removed": 0, "freed_bytes": 0}

    for source_dir in settings.staging_root.iterdir():
        if not source_dir.is_dir():
            continue
        for run_dir in source_dir.iterdir():
            if not run_dir.is_dir() or run_dir.stat().st_mtime >= cutoff:
                continue
            size = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
            if not dry_run:
                shutil.rmtree(run_dir, ignore_errors=True)
            removed.append(str(run_dir))
            freed += size

    log.info("sweep.staging", removed=len(removed), freed_bytes=freed, dry_run=dry_run)
    return {"removed": len(removed), "freed_bytes": freed, "paths": removed}


def check_quarantine(alert: bool = True) -> dict:
    settings = get_settings()
    qroot = settings.quarantine_root
    if not qroot.is_dir():
        return {"failures": 0}

    failures = sorted(qroot.rglob("_FAILURE_*.json"))
    by_source: dict[str, int] = {}
    for f in failures:
        source = next(
            (p.name.split("=", 1)[1] for p in f.parents if p.name.startswith("source=")),
            "unknown",
        )
        by_source[source] = by_source.get(source, 0) + 1

    log.info("sweep.quarantine", failures=len(failures), by_source=by_source)

    if failures and alert:
        lines = "\n".join(f"  {s}: {n}" for s, n in sorted(by_source.items()))
        notify(
            f"lake: {len(failures)} quarantined run(s)",
            f"Quarantine is not empty:\n\n{lines}\n\nls {qroot}",
            priority="default",
            tags="warning",
        )
    return {"failures": len(failures), "by_source": by_source}


def sweep_empty_run_dirs(dry_run: bool = True) -> dict:
    """Run dirs with no manifest are the residue of a crash. Report, don't delete.

    Deleting them by default would destroy the evidence you need to explain the
    gap. Pass --no-dry-run only when you have already looked.
    """
    settings = get_settings()
    orphans: list[Path] = []

    if not settings.raw_root.is_dir():
        return {"orphans": 0}

    for run_dir in settings.raw_root.rglob("run=*"):
        if run_dir.is_dir() and not (run_dir / "_MANIFEST.json").is_file():
            orphans.append(run_dir)
            if not dry_run:
                shutil.rmtree(run_dir, ignore_errors=True)

    log.info("sweep.orphan_run_dirs", count=len(orphans), dry_run=dry_run)
    return {"orphans": len(orphans), "paths": [str(p) for p in orphans]}
