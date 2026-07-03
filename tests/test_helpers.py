# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

import pytest

from gdrive_fsspec.core import DIR_MIME_TYPE, _finfo_from_response, _normalize_path
from gdrive_fsspec.utils import merge_fields


@pytest.mark.parametrize(
    "base, extra, expected",
    [
        # No extra fields: base is returned unchanged.
        ("name,id", None, "name,id"),
        ("name,id", "", "name,id"),
        # Extra fields are appended.
        ("name,id", "driveId", "name,id,driveId"),
        # Duplicate tokens across base and extra are dropped, order preserved.
        ("name,id,size", "id,driveId", "name,id,size,driveId"),
        # Surrounding whitespace and empty tokens are stripped.
        ("name, id ", " id ,driveId", "name,id,driveId"),
        ("name,id", "driveId,", "name,id,driveId"),
        # Nested-field selectors are treated as opaque tokens.
        (
            "name,id",
            "capabilities/canDelete",
            "name,id,capabilities/canDelete",
        ),
    ],
)
def test_merge_fields(base: str, extra: str | None, expected: str) -> None:
    assert merge_fields(base, extra) == expected


@pytest.mark.parametrize(
    "prefix, name, expected",
    [
        ("/a/b/", "c", "/a/b/c"),
        ("a/b", "c", "/a/b/c"),
    ],
)
def test_normalize_path(prefix: str, name: str, expected: str) -> None:
    assert _normalize_path(prefix, name) == expected


@pytest.mark.parametrize(
    "mime_type, expected_type",
    [
        ("text/plain", "file"),
        (DIR_MIME_TYPE, "directory"),
    ],
)
def test_finfo_from_response_type(mime_type: str, expected_type: str) -> None:
    info = _finfo_from_response(
        {"name": "child", "mimeType": mime_type}, path_prefix="parent"
    )
    assert info["type"] == expected_type
    assert info["name"] == "parent/child"


def test_finfo_from_response_casts_size() -> None:
    assert _finfo_from_response({"name": "x", "size": "12"})["size"] == 12


def test_finfo_from_response_defaults_missing_size() -> None:
    assert _finfo_from_response({"name": "x"})["size"] == 0


def test_finfo_from_response_strips_leading_slash() -> None:
    info = _finfo_from_response({"name": "f"}, path_prefix="/top")
    assert info["name"] == "top/f"
