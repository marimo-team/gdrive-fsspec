"""Opt-in export checks; scheduled runs require every fixture to be configured."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from export_validation import validate_export

from gdrive_fsspec import GoogleDriveFileSystem
from gdrive_fsspec._public import _DOCUMENT_TYPES, PublicDriveClient, PublicReference

pytestmark = pytest.mark.integration


def _configured(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        message = f"{name} is required for export checks"
        if os.environ.get("GDRIVE_FSSPEC_REQUIRE_EXPORT_CHECKS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return value


@pytest.mark.parametrize(
    "kind,source_mime,target_mime",
    [
        pytest.param(kind, document.mime_type, mime, id=f"{kind}-{format_name}")
        for kind, document in _DOCUMENT_TYPES.items()
        for mime, format_name in document.formats.items()
    ],
)
def test_public_export_content(kind: str, source_mime: str, target_mime: str) -> None:
    file_id = _configured(f"GDRIVE_FSSPEC_PUBLIC_{kind.upper()}")
    client = PublicDriveClient(PublicReference(file_id))
    try:
        data = client.export(file_id, source_mime, target_mime)
    finally:
        client.close()
    validate_export(data, target_mime)


def test_export_catalog() -> None:
    credentials = _configured("GDRIVE_FSSPEC_CREDENTIALS_PATH")
    fs = GoogleDriveFileSystem(
        token="service_account",
        creds=credentials,
        access="read_only",
        skip_instance_cache=True,
    )
    try:
        actual = {
            source: sorted(set(targets))
            for source, targets in fs.export_formats.items()
        }
    finally:
        fs.close()
    baseline = Path(__file__).parent / "fixtures" / "drive_export_formats.json"
    expected: object = json.loads(baseline.read_text())
    assert actual == expected, (
        "Drive's export catalog changed. Review added/removed MIME types and update "
        "tests/fixtures/drive_export_formats.json. Verify public routes separately "
        "before adding support."
    )
