"""Filesystem layer. The only code allowed to write to the NAS.

Three invariants, in order of how much pain they save you:

1. A partial file NEVER appears in raw/. We write into a temp file inside the
   destination directory, fsync it, verify its digest, then os.replace() — which
   is atomic within a filesystem. A reader either sees the whole file or nothing.

2. We never write when the NAS is unmounted. Otherwise scrapers cheerfully fill
   the NUC's root disk with data nobody will ever find. Two independent checks:
   a sentinel file that exists only on the NAS, and a st_dev comparison against /.

3. raw/ is immutable. Files land 0o440. Everything downstream is rebuildable
   from raw/, so raw/ is the one thing that must never be edited in place.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from datetime import date
from pathlib import Path

from lake.core.exceptions import ChecksumMismatch, NasNotMountedError
from lake.core.logging import get_logger
from lake.core.models import Artifact, RunContext, StreamedFile, sha256_file

log = get_logger(__name__)

SENTINEL_NAME = ".lake_mounted"
MANIFEST_NAME = "_MANIFEST.json"
_CHUNK = 1 << 20


def _fsync_dir(path: Path) -> None:
    """Persist the directory entry itself, not just the file contents.

    Without this, a power cut after os.replace() can lose the rename even though
    the data blocks landed.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def partition_path(root: Path, source_id: str, logical_date: date) -> Path:
    """Hive-style partitions so DuckDB/Spark discover them without config."""
    return (
        root
        / f"source={source_id}"
        / f"year={logical_date:%Y}"
        / f"month={logical_date:%m}"
        / f"day={logical_date:%d}"
    )


class Storage:
    def __init__(
        self,
        nas_root: Path,
        staging_root: Path,
        *,
        require_sentinel: bool = True,
        require_separate_device: bool = False,
    ):
        self.nas_root = Path(nas_root)
        self.staging_root = Path(staging_root)
        #: the sentinel file exists only on the NAS. Cheap, portable, and the
        #: check that actually catches an unmounted NFS export.
        self.require_sentinel = require_sentinel
        #: belt and braces for production: a mounted NAS is on its own device.
        #: Off by default because a tmpdir in tests shares a device with /.
        self.require_separate_device = require_separate_device

    # -- guards ---------------------------------------------------------------

    def assert_mounted(self) -> None:
        """Fail loudly and early rather than silently writing to the wrong disk."""
        if self.require_sentinel:
            sentinel = self.nas_root / SENTINEL_NAME
            if not sentinel.is_file():
                raise NasNotMountedError(
                    f"{sentinel} missing — NAS not mounted, refusing to write. "
                    f"Check: systemctl status mnt-nas.mount"
                )

        if self.require_separate_device:
            try:
                if os.stat(self.nas_root).st_dev == os.stat("/").st_dev:
                    raise NasNotMountedError(
                        f"{self.nas_root} is on the root filesystem — NAS not mounted"
                    )
            except FileNotFoundError as exc:  # pragma: no cover
                raise NasNotMountedError(f"{self.nas_root} does not exist") from exc

    # -- paths ----------------------------------------------------------------

    def raw_dir(self, ctx: RunContext) -> Path:
        base = partition_path(self.nas_root / "raw", ctx.source_id, ctx.logical_date)
        return base / ctx.run_dir_name

    def quarantine_dir(self, ctx: RunContext) -> Path:
        return partition_path(self.nas_root / "quarantine", ctx.source_id, ctx.logical_date)

    def staging_dir(self, ctx: RunContext) -> Path:
        d = self.staging_root / ctx.source_id / str(ctx.run_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def staging_path(self, ctx: RunContext, filename: str) -> Path:
        return self.staging_dir(ctx) / filename

    # -- write ----------------------------------------------------------------

    def commit(self, ctx: RunContext, artifact: Artifact) -> Path:
        """Atomically place one artifact into raw/. Returns the NAS-relative path.

        Never leaves a partial file behind, on any failure path.
        """
        self.assert_mounted()

        dest_dir = self.raw_dir(ctx)
        dest_dir.mkdir(parents=True, exist_ok=True)
        final = dest_dir / artifact.filename
        expected = artifact.sha256

        # NOTE: the temp file MUST live in dest_dir. os.replace() is only atomic
        # within one filesystem; /tmp -> NAS would be a copy that can tear.
        fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix=".tmp-", suffix=".partial")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                if isinstance(artifact, StreamedFile):
                    with open(artifact.path, "rb") as src:
                        while chunk := src.read(_CHUNK):
                            fh.write(chunk)
                else:
                    fh.write(artifact.content)  # type: ignore[attr-defined]
                fh.flush()
                os.fsync(fh.fileno())

            actual = sha256_file(tmp)
            if actual != expected:
                raise ChecksumMismatch(
                    f"{artifact.filename}: expected {expected[:12]}, got {actual[:12]}"
                )

            os.replace(tmp, final)  # atomic
            _fsync_dir(dest_dir)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        self._write_sidecar(ctx, artifact, final)
        final.chmod(0o440)  # raw is immutable

        log.debug(
            "storage.committed",
            filename=artifact.filename,
            sha256=expected[:12],
            size_bytes=artifact.size_bytes,
        )
        return final.relative_to(self.nas_root)

    def _write_sidecar(self, ctx: RunContext, artifact: Artifact, final: Path) -> None:
        """Provenance next to the bytes. Survives loss of the metadata DB."""
        meta = {
            "source_id": ctx.source_id,
            "run_id": str(ctx.run_id),
            "logical_date": ctx.logical_date.isoformat(),
            "fetched_at": ctx.started_at.isoformat(),
            "url": artifact.url,
            "http_status": artifact.http_status,
            "content_type": artifact.content_type,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "etag": artifact.etag,
            "last_modified": artifact.last_modified,
        }
        sidecar = final.with_name(final.name + ".meta.json")
        sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        sidecar.chmod(0o440)

    def write_manifest(self, ctx: RunContext, paths: list[Path], status: str = "complete") -> Path:
        """A run directory is trustworthy iff it holds a 'complete' manifest.

        Downstream readers skip any run dir without one. This is what makes a
        crashed mid-run scraper harmless rather than a silent data corruption.

        A run that committed nothing — because every artifact was a byte-identical
        duplicate of one we already hold — still gets a manifest. `file_count: 0`
        is the durable record that we checked and the source published nothing new.
        """
        run_dir = self.raw_dir(ctx)
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = run_dir / MANIFEST_NAME
        manifest.write_text(
            json.dumps(
                {
                    "run_id": str(ctx.run_id),
                    "source_id": ctx.source_id,
                    "logical_date": ctx.logical_date.isoformat(),
                    "started_at": ctx.started_at.isoformat(),
                    "status": status,
                    "attempt": ctx.attempt,
                    "file_count": len(paths),
                    "files": [str(p) for p in paths],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _fsync_dir(manifest.parent)
        return manifest

    def is_complete(self, run_dir: Path) -> bool:
        manifest = run_dir / MANIFEST_NAME
        if not manifest.is_file():
            return False
        try:
            return json.loads(manifest.read_text())["status"] == "complete"
        except (json.JSONDecodeError, KeyError):
            return False

    # -- failure --------------------------------------------------------------

    def quarantine(self, ctx: RunContext, error: BaseException) -> Path:
        """Park the wreckage where it can be inspected but not consumed.

        Partial downloads move out of staging so the sweeper doesn't delete the
        evidence before anyone looks at it.
        """
        qdir = self.quarantine_dir(ctx)
        qdir.mkdir(parents=True, exist_ok=True)

        (qdir / f"_FAILURE_{ctx.short_id}.json").write_text(
            json.dumps(
                {
                    "run_id": str(ctx.run_id),
                    "source_id": ctx.source_id,
                    "logical_date": ctx.logical_date.isoformat(),
                    "attempt": ctx.attempt,
                    "error_class": type(error).__name__,
                    "error_message": str(error),
                    "traceback": "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    ),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        stg = self.staging_root / ctx.source_id / str(ctx.run_id)
        if stg.exists() and any(stg.iterdir()):
            dest = qdir / f"partial_{ctx.short_id}"
            shutil.move(str(stg), str(dest))
            log.info("storage.quarantined_partial", path=str(dest))

        return qdir

    def cleanup_staging(self, ctx: RunContext) -> None:
        shutil.rmtree(self.staging_root / ctx.source_id / str(ctx.run_id), ignore_errors=True)


def default_storage() -> Storage:
    """The Storage every production code path should use.

    In production both mount guards are armed. In development the NAS is usually
    a scratch directory on the same disk as /, so only the sentinel is checked —
    create it with `touch $LAKE_NAS_ROOT/.lake_mounted`.
    """
    from lake.settings import get_settings

    settings = get_settings()
    production = settings.env == "production"
    return Storage(
        settings.nas_root,
        settings.staging_root,
        require_sentinel=True,
        require_separate_device=production,
    )
