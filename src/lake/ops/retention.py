"""Archive and retention. Both refuse to delete anything unless told twice.

Archiving compresses cold raw into one tar.zst per source-month: 5-15x on HTML,
JSON, and CSV. Retention then unlinks bytes past their policy age, but soft-
deletes the catalog row first, so you can always answer "what did we once hold?"
after the bytes are gone.
"""

from __future__ import annotations

import subprocess
import tarfile
import uuid
from pathlib import Path
from typing import Any

import yaml

from lake.core.logging import get_logger
from lake.core.storage import default_storage
from lake.metadata.repo import MetadataRepo
from lake.settings import get_settings

log = get_logger(__name__)

DEFAULT_POLICY = {
    "staging_hours": 24,
    "quarantine_days": 90,
    "raw_days": 1825,  # 5 years
    "archive_after_days": 365,
    "processed_days": None,  # keep forever
}


def load_policy(path: Path | None = None) -> dict[str, Any]:
    path = path or Path("configs/retention.yaml")
    if not path.is_file():
        return {"defaults": DEFAULT_POLICY, "sources": {}}
    doc = yaml.safe_load(path.read_text()) or {}
    return {
        "defaults": {**DEFAULT_POLICY, **(doc.get("defaults") or {})},
        "sources": doc.get("sources") or {},
    }


def policy_for(source_id: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Per-source overrides win, including an explicit `null`.

    `raw_days: null` for a source must mean "never delete", not "fall back to the
    default of 5 years". A dict merge gives us that; `or`-chaining would not.
    """
    return {**policy["defaults"], **(policy["sources"].get(source_id) or {})}


def _zstd_available() -> bool:
    try:
        subprocess.run(["zstd", "--version"], capture_output=True, timeout=5, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def archive_source_month(
    source_id: str, year: int, month: int, *, dry_run: bool = True
) -> dict[str, Any]:
    """Roll one source-month of raw/ into a single compressed tarball."""
    settings = get_settings()
    src_dir = settings.raw_root / f"source={source_id}" / f"year={year:04d}" / f"month={month:02d}"
    if not src_dir.is_dir():
        return {"archived": False, "reason": "no such partition", "path": str(src_dir)}

    out_dir = settings.archive_root / f"source={source_id}" / f"{year:04d}"
    out_path = out_dir / f"{source_id}_{year:04d}-{month:02d}.tar.zst"
    if out_path.exists():
        return {"archived": False, "reason": "already archived", "path": str(out_path)}

    files = [f for f in src_dir.rglob("*") if f.is_file()]
    total = sum(f.stat().st_size for f in files)

    if dry_run:
        log.info("archive.dry_run", source_id=source_id, files=len(files), bytes=total)
        return {"archived": False, "dry_run": True, "files": len(files), "bytes": total}

    out_dir.mkdir(parents=True, exist_ok=True)
    if _zstd_available():
        tar = out_path.with_suffix("")  # strip .zst -> .tar
        with tarfile.open(tar, "w") as tf:
            tf.add(src_dir, arcname=src_dir.name)
        subprocess.run(["zstd", "-19", "--rm", "-q", str(tar), "-o", str(out_path)], check=True)
    else:
        # Fall back to gzip rather than fail; zstd is a nicety, not a requirement.
        out_path = out_path.with_suffix(".gz")
        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(src_dir, arcname=src_dir.name)

    compressed = out_path.stat().st_size
    ratio = round(total / compressed, 1) if compressed else 0
    log.info("archive.complete", path=str(out_path), ratio=ratio, files=len(files))
    return {"archived": True, "path": str(out_path), "files": len(files), "ratio": ratio}


def apply_retention(*, apply: bool = False, config: Path | None = None) -> dict[str, Any]:
    """Enforce per-source raw retention. Soft-delete first, then unlink."""
    settings = get_settings()
    meta = MetadataRepo()
    policy = load_policy(config)
    default_storage().assert_mounted()

    report: dict[str, Any] = {"applied": apply, "sources": {}}
    grand_total = 0

    for source in meta.list_sources(enabled_only=False):
        rules = policy_for(source.source_id, policy)
        # retention.yaml is the operator's file and wins. Only when it says
        # nothing about this source do we fall back to sources.yaml.
        if source.source_id in policy["sources"]:
            raw_days = rules.get("raw_days")
        else:
            raw_days = source.retention_raw_days or rules.get("raw_days")

        if not raw_days:  # None or 0 => never delete
            log.debug("retention.skip", source_id=source.source_id, reason="no raw_days policy")
            continue

        candidates = meta.files_for_retention(source.source_id, raw_days, layer="raw")
        freed = sum(c["size_bytes"] for c in candidates)
        grand_total += freed

        if apply and candidates:
            # Catalog first: if the unlink loop dies halfway, the DB already
            # reflects intent and a re-run is safe.
            meta.mark_deleted([uuid.UUID(str(c["file_id"])) for c in candidates])
            for c in candidates:
                target = settings.nas_root / c["nas_path"]
                target.unlink(missing_ok=True)
                target.with_name(target.name + ".meta.json").unlink(missing_ok=True)

        report["sources"][source.source_id] = {
            "raw_days": raw_days,
            "files": len(candidates),
            "bytes": freed,
        }
        log.info(
            "retention.source",
            source_id=source.source_id,
            files=len(candidates),
            bytes=freed,
            applied=apply,
        )

    report["total_bytes"] = grand_total
    if not apply:
        log.warning("retention.dry_run", total_bytes=grand_total, hint="pass --apply to delete")
    return report


def archive_by_policy(*, dry_run: bool = True, config: Path | None = None) -> list[dict]:
    """Archive every source-month older than its archive_after_days."""
    from datetime import date, timedelta

    settings = get_settings()
    meta = MetadataRepo()
    policy = load_policy(config)
    results = []

    for source in meta.list_sources(enabled_only=False):
        after_days = policy_for(source.source_id, policy).get("archive_after_days")
        if not after_days:
            continue

        cutoff = date.today() - timedelta(days=after_days)
        base = settings.raw_root / f"source={source.source_id}"
        if not base.is_dir():
            continue

        months: set[tuple[int, int]] = set()
        for month_dir in base.glob("year=*/month=*"):
            year = int(month_dir.parent.name.split("=")[1])
            month = int(month_dir.name.split("=")[1])
            if date(year, month, 1) < cutoff.replace(day=1):
                months.add((year, month))

        for year, month in sorted(months):
            results.append(
                archive_source_month(source.source_id, year, month, dry_run=dry_run)
                | {"source_id": source.source_id, "year": year, "month": month}
            )

    return results
