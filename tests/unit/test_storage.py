"""Storage invariants. If these break, the lake quietly corrupts and nobody notices."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lake.core.exceptions import ChecksumMismatch, NasNotMountedError
from lake.core.models import FetchedFile, StreamedFile
from lake.core.storage import SENTINEL_NAME, Storage


def make_file(
    name: str = "data_20260709_000.json", content: bytes = b'{"ok": true}'
) -> FetchedFile:
    return FetchedFile(
        filename=name, content=content, url="https://example.org/data", http_status=200
    )


def test_commit_writes_file_and_sidecar(storage: Storage, ctx, nas: Path):
    rel = storage.commit(ctx, make_file())

    final = nas / rel
    assert final.is_file()
    assert final.read_bytes() == b'{"ok": true}'

    sidecar = json.loads(final.with_name(final.name + ".meta.json").read_text())
    assert sidecar["source_id"] == "test_source"
    assert sidecar["sha256"] == make_file().sha256
    assert sidecar["logical_date"] == "2026-07-09"


def test_commit_uses_hive_partitions(storage: Storage, ctx, nas: Path):
    rel = storage.commit(ctx, make_file())
    parts = rel.parts
    assert parts[0] == "raw"
    assert parts[1] == "source=test_source"
    assert parts[2] == "year=2026"
    assert parts[3] == "month=07"
    assert parts[4] == "day=09"
    assert parts[5].startswith("run=20260709T060000Z_a1b2c3d4")


def test_raw_files_are_read_only(storage: Storage, ctx, nas: Path):
    """raw/ is immutable. Everything downstream is rebuildable from it."""
    rel = storage.commit(ctx, make_file())
    mode = (nas / rel).stat().st_mode & 0o777
    assert mode == 0o440
    with pytest.raises(PermissionError):
        (nas / rel).write_bytes(b"tamper")


def test_refuses_to_write_when_nas_unmounted(nas: Path, staging: Path, ctx):
    """The failure that fills the NUC's root disk for three weeks."""
    (nas / SENTINEL_NAME).unlink()
    storage = Storage(nas, staging, require_sentinel=True)

    with pytest.raises(NasNotMountedError, match="not mounted"):
        storage.commit(ctx, make_file())


def test_device_guard_catches_a_nas_path_on_the_root_disk(nas: Path, staging: Path, ctx):
    """Second, independent signal: a real NAS is never on the same device as /.

    Armed only in production — a pytest tmpdir shares a device with root.
    """
    storage = Storage(nas, staging, require_sentinel=True, require_separate_device=True)

    with pytest.raises(NasNotMountedError, match="root filesystem"):
        storage.commit(ctx, make_file())


def test_checksum_mismatch_leaves_no_partial_file(storage: Storage, ctx, nas: Path, monkeypatch):
    """A torn write must never leave something that looks like real data."""
    bad = make_file()
    monkeypatch.setattr(type(bad), "sha256", property(lambda self: "0" * 64))

    with pytest.raises(ChecksumMismatch):
        storage.commit(ctx, bad)

    run_dir = storage.raw_dir(ctx)
    leftovers = list(run_dir.iterdir()) if run_dir.exists() else []
    assert leftovers == [], f"partial files left behind: {leftovers}"


def test_no_partial_survives_a_write_failure(storage: Storage, ctx, nas: Path, monkeypatch):
    """Simulate the process dying mid-write. raw/ must stay clean."""

    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", explode)

    with pytest.raises(OSError, match="disk full"):
        storage.commit(ctx, make_file())

    run_dir = storage.raw_dir(ctx)
    assert not any(run_dir.glob("*.json")), "a partial file reached raw/"
    assert not any(run_dir.glob("*.partial"))


def test_manifest_marks_run_trustworthy(storage: Storage, ctx):
    rel = storage.commit(ctx, make_file())
    storage.write_manifest(ctx, [rel], status="complete")

    run_dir = storage.raw_dir(ctx)
    assert storage.is_complete(run_dir)

    manifest = json.loads((run_dir / "_MANIFEST.json").read_text())
    assert manifest["file_count"] == 1
    assert manifest["status"] == "complete"


def test_run_dir_without_manifest_is_not_trustworthy(storage: Storage, ctx):
    """A scraper killed mid-run leaves real-looking files and no manifest.

    Downstream must skip it. This is what makes a crash harmless.
    """
    storage.commit(ctx, make_file())
    assert not storage.is_complete(storage.raw_dir(ctx))


def test_quarantine_captures_failure_and_partials(storage: Storage, ctx, nas: Path):
    partial = storage.staging_path(ctx, "half_downloaded.csv")
    partial.write_bytes(b"col_a,col_b\n1,")

    qdir = storage.quarantine(ctx, ValueError("upstream returned an HTML error page"))

    failure = json.loads((qdir / "_FAILURE_a1b2c3d4.json").read_text())
    assert failure["error_class"] == "ValueError"
    assert "HTML error page" in failure["error_message"]
    assert "Traceback" in failure["traceback"] or failure["traceback"]

    assert (qdir / "partial_a1b2c3d4" / "half_downloaded.csv").is_file()


def test_streamed_file_commits_without_loading_into_memory(storage: Storage, ctx, nas: Path):
    src = storage.staging_path(ctx, "big_2026_000.csv.gz")
    payload = b"x" * (2 << 20)  # 2 MiB
    src.write_bytes(payload)

    artifact = StreamedFile(filename="big_2026_000.csv.gz", path=src, url="https://example.org/big")
    rel = storage.commit(ctx, artifact)

    assert (nas / rel).stat().st_size == len(payload)
    assert (nas / rel).read_bytes() == payload


def test_commit_is_atomic_within_one_filesystem(storage: Storage, ctx, nas: Path, monkeypatch):
    """The temp file must live in the destination dir, or os.replace() is a copy."""
    seen: list[Path] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def spy(*args, **kwargs):
        seen.append(Path(kwargs["dir"]))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr("lake.core.storage.tempfile.mkstemp", spy)
    storage.commit(ctx, make_file())

    assert seen == [storage.raw_dir(ctx)]
