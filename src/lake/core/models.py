"""Value objects passed between the scraper, storage, and metadata layers."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

_CHUNK = 1 << 20  # 1 MiB


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


@runtime_checkable
class Artifact(Protocol):
    """What storage.commit() needs to know about a fetched thing."""

    filename: str
    url: str
    http_status: int
    content_type: str | None
    etag: str | None
    last_modified: str | None

    @property
    def sha256(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...


@dataclass(slots=True)
class FetchedFile:
    """A small artifact held in memory. Fine up to ~200 MB."""

    filename: str
    content: bytes
    url: str
    http_status: int = 200
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    _sha256: str | None = field(default=None, repr=False)

    @property
    def sha256(self) -> str:
        if self._sha256 is None:
            self._sha256 = hashlib.sha256(self.content).hexdigest()
        return self._sha256

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(slots=True)
class StreamedFile:
    """A large artifact already written to staging. Never loaded whole into RAM.

    The scraper streams to `path` and reports the digest it computed on the way.
    Storage re-verifies from disk before committing.
    """

    filename: str
    path: Path
    url: str
    http_status: int = 200
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    _sha256: str | None = field(default=None, repr=False)

    @property
    def sha256(self) -> str:
        if self._sha256 is None:
            self._sha256 = sha256_file(self.path)
        return self._sha256

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


@dataclass(slots=True)
class RunContext:
    """Identity of a single scrape attempt. Threaded through every layer."""

    run_id: uuid.UUID
    source_id: str
    logical_date: date
    started_at: datetime
    attempt: int = 1
    trigger: str = "schedule"  # schedule | manual | retry | backfill

    @classmethod
    def new(
        cls,
        source_id: str,
        logical_date: date,
        *,
        attempt: int = 1,
        trigger: str = "schedule",
    ) -> RunContext:
        return cls(
            run_id=uuid.uuid4(),
            source_id=source_id,
            logical_date=logical_date,
            started_at=datetime.now(UTC),
            attempt=attempt,
            trigger=trigger,
        )

    @property
    def short_id(self) -> str:
        return str(self.run_id)[:8]

    @property
    def run_dir_name(self) -> str:
        return f"run={self.started_at:%Y%m%dT%H%M%SZ}_{self.short_id}"
