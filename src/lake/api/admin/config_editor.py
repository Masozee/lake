"""Editing configs/sources.yaml from the browser, without making it a footgun.

This file is git-tracked and normally changes only through review. Editing it from
a web form bypasses that, so three things stand in for the missing pull request:

1. **Validate before writing.** The candidate YAML is parsed, merged the same way
   the registry merges it, and every source is checked — the module path has to
   actually import, the schedule has to be one the timers know, the SLA has to be
   a positive number. A file that would break a scraper is refused, and the reason
   comes back to the form.

2. **Back up before overwriting.** The current file is copied to
   configs/backups/sources.<timestamp>.yaml first. That is the undo.

3. **Write atomically.** A temp file in the same directory, fsync, then
   `os.replace`. A reader — `lake sync-sources`, a scraper booting right now —
   sees the whole old file or the whole new one, never half of either.

The audit entry (in auth.record) carries the previous content and the backup path,
so "what changed and who did it" is answerable without git.

What this deliberately will NOT do: resolve `${env:VAR}` references. Secrets stay
in /etc/lake/lake.env. The editor round-trips the reference as written, so a
secret can neither be read out of the panel nor pasted into the YAML by accident.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from lake.core.logging import get_logger
from lake.registry import _deep_merge, _import_scraper_class
from lake.settings import get_settings

log = get_logger(__name__)

#: The schedules systemd has timers for. A source on any other schedule would be
#: written to the file, synced to the catalog, and then never run — the exact
#: silent failure `lake check-freshness` exists to catch. Refuse it at the door.
SCHEDULES = frozenset({"daily", "weekly", "monthly", "yearly"})

#: Matches the registry's own set. A kind it does not know is a typo.
KINDS = frozenset({"api", "html", "file"})

#: Kept out of the editor's way. A literal secret in a git-tracked file is a leak,
#: so the form may only ever carry the ${env:VAR} indirection.
_ENV_REF = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")

#: How many backups to keep. Enough to undo a bad week, not so many that the
#: directory becomes its own problem.
KEEP_BACKUPS = 50


class InvalidConfig(Exception):
    """The candidate YAML would break something. Carries every reason at once, so
    the form can show them all rather than one per submit."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def config_path() -> Path:
    """The registry, as an absolute path.

    `sources_config` defaults to a *relative* path, which resolves against the
    process's working directory — fine for the CLI, which is run from the repo,
    and a landmine for a systemd unit whose WorkingDirectory is somewhere else.
    Resolving here means the panel reads the same file the scrapers do, no matter
    who started it or from where.
    """
    return Path(get_settings().sources_config).resolve()


def backup_dir() -> Path:
    return config_path().parent / "backups"


def read_config() -> str:
    """The file as it is on disk, verbatim — comments, spacing, and all.

    Deliberately text, not a parsed dict round-tripped back to YAML: a round trip
    would silently eat every comment in a file whose comments are the only
    explanation of why a source is configured the way it is.
    """
    path = config_path()
    if not path.is_file():
        raise FileNotFoundError(f"source registry not found: {path}")
    return path.read_text(encoding="utf-8")


def validate(content: str) -> dict[str, dict[str, Any]]:
    """Parse and check a candidate file. Returns the merged sources, or raises.

    Every check here answers a way the file could be valid YAML and still take a
    scraper down.
    """
    errors: list[str] = []

    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise InvalidConfig([f"not valid YAML: {exc}"]) from exc

    if not isinstance(doc, dict):
        raise InvalidConfig(["the file must be a YAML mapping with a `sources:` key"])

    entries = doc.get("sources")
    if not isinstance(entries, list) or not entries:
        raise InvalidConfig(["`sources:` must be a non-empty list"])

    defaults = doc.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise InvalidConfig(["`defaults:` must be a mapping"])

    merged: dict[str, dict[str, Any]] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"source #{i + 1} is not a mapping")
            continue

        sid = entry.get("source_id")
        if not sid or not isinstance(sid, str):
            errors.append(f"source #{i + 1} has no source_id")
            continue
        if sid in merged:
            errors.append(f"{sid}: duplicate source_id")
            continue

        cfg = _deep_merge(defaults, entry)
        merged[sid] = cfg
        errors.extend(_check_source(sid, cfg))

    if errors:
        raise InvalidConfig(errors)
    return merged


def _check_source(sid: str, cfg: dict[str, Any]) -> list[str]:
    """Everything that must be true of one source for it to actually run."""
    errors: list[str] = []

    schedule = cfg.get("schedule")
    if schedule not in SCHEDULES:
        errors.append(
            f"{sid}: schedule {schedule!r} has no timer — use one of {', '.join(sorted(SCHEDULES))}"
        )

    kind = cfg.get("kind")
    if kind not in KINDS:
        errors.append(f"{sid}: kind {kind!r} is not one of {', '.join(sorted(KINDS))}")

    if not cfg.get("display_name"):
        errors.append(f"{sid}: display_name is required — it is what readers see")

    # The one check that catches a rename or a typo'd path: actually import it.
    # A source whose module does not resolve fails at scrape time, hours later,
    # in a log nobody is reading.
    module = cfg.get("module")
    if not module or not isinstance(module, str):
        errors.append(f"{sid}: module is required (e.g. lake.sources.x.scraper:XScraper)")
    else:
        try:
            _import_scraper_class(module)
        except Exception as exc:
            errors.append(f"{sid}: cannot load module {module!r} — {exc}")

    sla = cfg.get("freshness_sla_hours")
    if sla is not None and (not isinstance(sla, int) or sla <= 0):
        errors.append(f"{sid}: freshness_sla_hours must be a positive whole number, got {sla!r}")

    if not isinstance(cfg.get("enabled", True), bool):
        errors.append(f"{sid}: enabled must be true or false")

    # A literal-looking secret in a git-tracked file. The indirection exists for
    # exactly this reason, so say so rather than quietly committing the key.
    for key in ("api_key", "token", "password", "secret"):
        value = cfg.get(key)
        if isinstance(value, str) and value and not _ENV_REF.fullmatch(value.strip()):
            errors.append(
                f"{sid}: {key} looks like a literal secret. Put it in /etc/lake/lake.env "
                f"and reference it here as ${{env:VAR_NAME}}."
            )

    return errors


def write_config(content: str) -> Path:
    """Validate, back up, and replace the registry. Returns the backup's path.

    Raises InvalidConfig before touching the disk — a refused edit leaves the file
    exactly as it was.
    """
    validate(content)  # raises before anything is written

    path = config_path()
    backups = backup_dir()
    backups.mkdir(parents=True, exist_ok=True)

    # Microseconds, not seconds. Two saves inside the same second are entirely
    # possible — a fat-fingered double click, a fix straight after a mistake — and
    # a second-resolution name would have the later one silently overwrite the
    # earlier one's backup. Losing the version you wanted to go back to is the one
    # thing an undo may not do.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backups / f"sources.{stamp}.yaml"
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    # Atomic: a temp file in the *same directory* (so the rename cannot cross a
    # filesystem), fsync, then replace. `lake sync-sources` or a scraper booting
    # mid-write sees the whole old file or the whole new one — never half of one.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sources.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    _prune_backups()
    log.info("admin.config_written", path=str(path), backup=str(backup))
    return backup


def _prune_backups() -> None:
    """Keep the newest KEEP_BACKUPS. The names sort chronologically by design."""
    kept = sorted(backup_dir().glob("sources.*.yaml"), reverse=True)
    for old in kept[KEEP_BACKUPS:]:
        old.unlink(missing_ok=True)


def list_backups() -> list[dict[str, Any]]:
    """Every backup, newest first — the undo list."""
    out = []
    for path in sorted(backup_dir().glob("sources.*.yaml"), reverse=True):
        stat = path.stat()
        out.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "written_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            }
        )
    return out


def read_backup(name: str) -> str:
    """One backup's content, for previewing before a restore.

    The name is resolved against the backup directory and checked to still be
    inside it, so `../../etc/passwd` reads nothing.
    """
    candidate = (backup_dir() / name).resolve()
    if candidate.parent != backup_dir().resolve() or not candidate.is_file():
        raise FileNotFoundError(f"no backup named {name!r}")
    return candidate.read_text(encoding="utf-8")
