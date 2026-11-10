"""Content checks shared by the live export tests."""

from __future__ import annotations

import csv
import os
from html.parser import HTMLParser
from io import BytesIO, StringIO
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from gdrive_fsspec.typing_utils import override

_OFFICE_PARTS: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word/document.xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xl/workbook.xml",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ppt/presentation.xml",
}
_OPEN_DOCUMENT_TYPES: set[str] = {
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
}
VALIDATED_EXPORT_MIME_TYPES: frozenset[str] = (
    frozenset(_OFFICE_PARTS)
    | _OPEN_DOCUMENT_TYPES
    | {
        "application/epub+zip",
        "application/zip",
        "application/pdf",
        "application/rtf",
        "image/png",
        "image/jpeg",
        "image/svg+xml",
        "text/html",
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
    }
)


def configured_export_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        message = f"{name} is required for export checks"
        if os.environ.get("GDRIVE_FSSPEC_REQUIRE_EXPORT_CHECKS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return value


class _HtmlStructure(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)


def validate_export(data: bytes, mime_type: str) -> None:
    assert data, f"Empty {mime_type} export; use a nonempty public fixture"
    if mime_type in _OFFICE_PARTS:
        with ZipFile(BytesIO(data)) as archive:
            assert archive.testzip() is None
            ElementTree.fromstring(archive.read("[Content_Types].xml"))
            ElementTree.fromstring(archive.read(_OFFICE_PARTS[mime_type]))
    elif mime_type in _OPEN_DOCUMENT_TYPES:
        with ZipFile(BytesIO(data)) as archive:
            assert archive.testzip() is None
            assert archive.read("mimetype").decode() == mime_type
            ElementTree.fromstring(archive.read("content.xml"))
    elif mime_type == "application/epub+zip":
        with ZipFile(BytesIO(data)) as archive:
            assert archive.testzip() is None
            assert archive.read("mimetype") == b"application/epub+zip"
            ElementTree.fromstring(archive.read("META-INF/container.xml"))
    elif mime_type == "application/zip":
        with ZipFile(BytesIO(data)) as archive:
            assert archive.testzip() is None
            html_files = [name for name in archive.namelist() if name.endswith(".html")]
            assert html_files, "Expected an HTML document in the exported archive"
            for name in html_files:
                validate_export(archive.read(name), "text/html")
    elif mime_type == "application/pdf":
        assert data.startswith(b"%PDF-")
        assert data.rstrip().endswith(b"%%EOF")
    elif mime_type == "application/rtf":
        assert data.startswith(b"{\\rtf")
        assert data.rstrip().endswith(b"}")
    elif mime_type == "image/png":
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert data[12:16] == b"IHDR"
        assert data.endswith(b"IEND\xaeB`\x82")
    elif mime_type == "image/jpeg":
        assert data.startswith(b"\xff\xd8\xff")
        assert data.endswith(b"\xff\xd9")
    elif mime_type == "image/svg+xml":
        assert ElementTree.fromstring(data).tag == "{http://www.w3.org/2000/svg}svg"
    elif mime_type == "text/html":
        parser = _HtmlStructure()
        parser.feed(data.decode("utf-8-sig"))
        parser.close()
        assert parser.tags & {"html", "table"}
    elif mime_type in {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
    }:
        text = data.decode("utf-8-sig")
        assert text.strip()
        parser = _HtmlStructure()
        parser.feed(text)
        parser.close()
        assert not parser.tags & {"html", "head", "body", "table", "form"}
        if mime_type in {"text/csv", "text/tab-separated-values"}:
            delimiter = "," if mime_type == "text/csv" else "\t"
            rows = list(csv.reader(StringIO(text), delimiter=delimiter, strict=True))
            assert any(any(cell.strip() for cell in row) for row in rows)
    else:
        raise AssertionError(f"No content validator for export MIME type {mime_type!r}")
