"""The structural gate. Cheapest check, catches the most embarrassing bug."""

from __future__ import annotations

import gzip

import pytest

from lake.core.exceptions import ValidationFailed
from lake.core.models import FetchedFile
from lake.core.sniff import describe, looks_like_html, matches_extension
from lake.core.validate import check_gzip_decompresses, check_structural


def f(name: str, content: bytes) -> FetchedFile:
    return FetchedFile(filename=name, content=content, url="https://example.org", http_status=200)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"%PDF-1.7\n...", "pdf"),
        (b"PK\x03\x04...", "xlsx"),
        (b"\x1f\x8b\x08\x00", "gz"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"<!DOCTYPE html><html>", "html"),
        (b"just some text", "unknown"),
    ],
)
def test_describe_reads_magic_bytes(content: bytes, expected: str):
    assert describe(content) == expected


def test_html_detection_tolerates_leading_whitespace():
    assert looks_like_html(b"\n\n  <!doctype html><html>")
    assert looks_like_html(b"<HTML><body>oops</body></HTML>")
    assert not looks_like_html(b"%PDF-1.7")


def test_text_formats_have_no_signature_to_check():
    # csv/json/txt always pass the magic-byte gate; parsing validates them.
    assert matches_extension(b"a,b,c\n1,2,3", "csv")
    assert matches_extension(b'{"a": 1}', "json")


def test_html_error_page_wearing_a_data_extension_is_caught():
    html = b"<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head></html>"
    with pytest.raises(ValidationFailed, match="HTML page") as exc:
        check_structural(f("inflation_20260709_000.xlsx", html))
    assert exc.value.check_name == "magic_bytes"


def test_wrong_magic_bytes_are_caught():
    with pytest.raises(ValidationFailed, match="magic bytes") as exc:
        check_structural(f("report_20260709_000.pdf", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32))
    assert exc.value.detail["detected"] == "png"


def test_empty_file_is_caught():
    with pytest.raises(ValidationFailed, match="byte floor"):
        check_structural(f("data.json", b"{}"))


def test_valid_pdf_passes():
    check_structural(f("report_20260709_001.pdf", b"%PDF-1.7\n" + b"\x00" * 64))


def test_html_extension_may_contain_html():
    check_structural(f("gov_news_20260709_index.html", b"<!DOCTYPE html><html>...</html>"))


def test_truncated_gzip_is_caught(tmp_path):
    good = gzip.compress(b"col_a,col_b\n1,2\n" * 1000)
    path = tmp_path / "dump.csv.gz"

    path.write_bytes(good)
    check_gzip_decompresses(path)  # does not raise

    path.write_bytes(good[: len(good) // 2])  # truncated download
    with pytest.raises(ValidationFailed, match="corrupt or truncated"):
        check_gzip_decompresses(path)
