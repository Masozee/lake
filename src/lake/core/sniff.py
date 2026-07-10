"""Magic-byte checks.

The single most common silent failure in scraping: the server returns a 200 with
an HTML error page, and you write it to disk as `report.xlsx`. Six months later
the parser explodes and nobody knows when the data went bad.

Check the bytes, not the extension, and not the Content-Type header (which lies).
"""

from __future__ import annotations

_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    # xlsx/docx are zip containers; xls is the old OLE2 compound file
    "xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "gz": (b"\x1f\x8b",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "parquet": (b"PAR1",),
}

_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body")


def looks_like_html(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return any(head.startswith(m) or m in head for m in _HTML_MARKERS)


def matches_extension(content: bytes, extension: str) -> bool:
    """True if the bytes are consistent with the extension.

    Text formats (csv, json, txt, html) have no signature; they always pass here
    and are validated by parsing instead.
    """
    ext = extension.lower().lstrip(".")
    signatures = _SIGNATURES.get(ext)
    if signatures is None:
        return True
    return content[:16].startswith(signatures)


def describe(content: bytes) -> str:
    """Best-effort label for an error message."""
    if looks_like_html(content):
        return "html"
    for name, signatures in _SIGNATURES.items():
        if content[:16].startswith(signatures):
            return name
    return "unknown"
