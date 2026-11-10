"""Content checks shared by the live export tests."""

from __future__ import annotations

import csv
import os
from collections.abc import Callable
from functools import partial
from html.parser import HTMLParser
from io import BytesIO, StringIO
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from gdrive_fsspec.typing_utils import override


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


def _validate_xml_archive(
    data: bytes, *, members: tuple[str, ...], mime_type: str | None = None
) -> None:
    with ZipFile(BytesIO(data)) as archive:
        assert archive.testzip() is None
        if mime_type is not None:
            assert archive.read("mimetype").decode() == mime_type
        for member in members:
            ElementTree.fromstring(archive.read(member))


def _validate_html_archive(data: bytes) -> None:
    with ZipFile(BytesIO(data)) as archive:
        assert archive.testzip() is None
        html_files = [name for name in archive.namelist() if name.endswith(".html")]
        assert html_files, "Expected an HTML document in the exported archive"
        for name in html_files:
            validate_export(archive.read(name), "text/html")


def _validate_pdf(data: bytes) -> None:
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")


def _validate_rtf(data: bytes) -> None:
    assert data.startswith(b"{\\rtf")
    assert data.rstrip().endswith(b"}")


def _validate_png(data: bytes) -> None:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    assert data.endswith(b"IEND\xaeB`\x82")


def _validate_jpeg(data: bytes) -> None:
    assert data.startswith(b"\xff\xd8\xff")
    assert data.endswith(b"\xff\xd9")


def _validate_svg(data: bytes) -> None:
    assert ElementTree.fromstring(data).tag == "{http://www.w3.org/2000/svg}svg"


def _html_tags(text: str) -> set[str]:
    parser = _HtmlStructure()
    parser.feed(text)
    parser.close()
    return parser.tags


def _validate_html(data: bytes) -> None:
    assert _html_tags(data.decode("utf-8-sig")) & {"html", "table"}


def _text_content(data: bytes) -> str:
    text = data.decode("utf-8-sig")
    assert text.strip()
    assert not _html_tags(text) & {"html", "head", "body", "table", "form"}
    return text


def _validate_text(data: bytes) -> None:
    _text_content(data)


def _validate_delimited_text(data: bytes, *, delimiter: str) -> None:
    rows = list(
        csv.reader(StringIO(_text_content(data)), delimiter=delimiter, strict=True)
    )
    assert any(any(cell.strip() for cell in row) for row in rows)


EXPORT_VALIDATORS: dict[str, Callable[[bytes], None]] = {
    **{
        mime_type: partial(
            _validate_xml_archive, members=("[Content_Types].xml", document_part)
        )
        for mime_type, document_part in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word/document.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ppt/presentation.xml",
        }.items()
    },
    **{
        mime_type: partial(
            _validate_xml_archive, members=("content.xml",), mime_type=mime_type
        )
        for mime_type in (
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.presentation",
        )
    },
    "application/epub+zip": partial(
        _validate_xml_archive,
        members=("META-INF/container.xml",),
        mime_type="application/epub+zip",
    ),
    "application/zip": _validate_html_archive,
    "application/pdf": _validate_pdf,
    "application/rtf": _validate_rtf,
    "image/png": _validate_png,
    "image/jpeg": _validate_jpeg,
    "image/svg+xml": _validate_svg,
    "text/html": _validate_html,
    "text/plain": _validate_text,
    "text/markdown": _validate_text,
    "text/csv": partial(_validate_delimited_text, delimiter=","),
    "text/tab-separated-values": partial(_validate_delimited_text, delimiter="\t"),
}


def validate_export(data: bytes, mime_type: str) -> None:
    assert data, f"Empty {mime_type} export; use a nonempty public fixture"
    validator = EXPORT_VALIDATORS.get(mime_type)
    if validator is None:
        raise AssertionError(f"No content validator for export MIME type {mime_type!r}")
    validator(data)
