import csv
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from export_validation import (
    VALIDATED_EXPORT_MIME_TYPES,
    configured_export_setting,
    validate_export,
)

from gdrive_fsspec._public import PublicDriveClient


def test_every_public_export_format_has_a_content_validator() -> None:
    for targets in PublicDriveClient.export_formats().values():
        assert set(targets) <= VALIDATED_EXPORT_MIME_TYPES


def test_public_export_formats_are_in_the_api_snapshot() -> None:
    path = Path(__file__).parent / "fixtures" / "drive_export_formats.json"
    baseline: object = json.loads(path.read_text())
    assert isinstance(baseline, dict)
    for source, targets in PublicDriveClient.export_formats().items():
        api_targets: object = baseline[source]
        assert isinstance(api_targets, list)
        assert all(target in api_targets for target in targets)


def test_office_export_requires_the_correct_document_parts() -> None:
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    validate_export(
        data.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    with pytest.raises(KeyError, match="xl/workbook.xml"):
        validate_export(
            data.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


@pytest.mark.parametrize(
    "mime_type,data",
    [
        ("application/pdf", b"%PDF-1.7\nincomplete"),
        ("application/rtf", b"{\\rtf1 incomplete"),
        ("image/png", b"\x89PNG\r\n\x1a\n"),
        ("image/jpeg", b"\xff\xd8\xff"),
        ("image/svg+xml", b"<html/>"),
    ],
)
def test_export_rejects_incomplete_or_wrong_content(
    mime_type: str, data: bytes
) -> None:
    with pytest.raises(AssertionError):
        validate_export(data, mime_type)


@pytest.mark.parametrize(
    "mime_type",
    ["text/plain", "text/markdown", "text/csv", "text/tab-separated-values"],
)
@pytest.mark.parametrize(
    "data",
    [
        b"<!DOCTYPE html><html>Sign in</html>",
        b"<head><title>Error 400</title></head>",
        b"<!-- comment --><html>Sign in</html>",
    ],
)
def test_text_export_rejects_html_markup(mime_type: str, data: bytes) -> None:
    with pytest.raises(AssertionError):
        validate_export(data, mime_type)


def test_csv_export_checks_rows_after_the_first() -> None:
    with pytest.raises(csv.Error):
        validate_export(b'header,value\n"unterminated', "text/csv")


@pytest.mark.parametrize(
    "data",
    [
        b"<!doctype html><html><body>Document</body></html>",
        b'<meta charset="utf-8"><table><tr><td>Sheet</td></tr></table>',
    ],
)
def test_html_export_accepts_documents_and_sheet_fragments(data: bytes) -> None:
    validate_export(data, "text/html")


def test_html_export_rejects_plain_text() -> None:
    with pytest.raises(AssertionError):
        validate_export(b"not an HTML export", "text/html")


def test_new_formats_need_a_content_validator() -> None:
    with pytest.raises(AssertionError, match="No content validator"):
        validate_export(b"some content", "application/new-format")


def test_scheduled_export_checks_require_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "GDRIVE_FSSPEC_MISSING_TEST_FIXTURE"
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GDRIVE_FSSPEC_REQUIRE_EXPORT_CHECKS", "1")
    with pytest.raises(pytest.fail.Exception, match=name):
        configured_export_setting(name)
    monkeypatch.delenv("GDRIVE_FSSPEC_REQUIRE_EXPORT_CHECKS")
    with pytest.raises(pytest.skip.Exception, match=name):
        configured_export_setting(name)
