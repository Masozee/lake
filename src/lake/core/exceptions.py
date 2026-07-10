"""Exception hierarchy.

The `transient` flag decides whether a failure is worth retrying. Network blips
and 5xx are transient; a 404 or a schema violation means the source changed or
our code is wrong, and retrying just hammers someone else's server.
"""

from __future__ import annotations


class LakeError(Exception):
    """Base for everything this project raises."""

    transient: bool = False


class NasNotMountedError(LakeError):
    """The NAS is not mounted. Refuse to write — do not fill the root disk."""

    transient = True


class ChecksumMismatch(LakeError):
    """Bytes on disk do not match the digest we computed in memory."""

    transient = True


class SourceUnchanged(LakeError):
    """Upstream returned 304 Not Modified. Not an error — the run is 'skipped'."""

    transient = False


class ValidationFailed(LakeError):
    """Fetched bytes failed a structural, schema, or statistical check."""

    transient = False

    def __init__(self, message: str, *, check_name: str = "unknown", detail: dict | None = None):
        super().__init__(message)
        self.check_name = check_name
        self.detail = detail or {}


class FetchError(LakeError):
    """Upstream fetch failed after in-run retries were exhausted."""

    def __init__(self, message: str, *, transient: bool = True, http_status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.http_status = http_status


class ConfigError(LakeError):
    """sources.yaml is malformed, or a source_id does not exist."""

    transient = False
