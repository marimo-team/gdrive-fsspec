"""Opt-in public Drive tests; no authenticated credentials are loaded."""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest

from gdrive_fsspec import GoogleDriveFileSystem

pytestmark = pytest.mark.integration


@pytest.fixture()
def public_drive() -> Generator[GoogleDriveFileSystem]:
    root = os.environ.get("GDRIVE_FSSPEC_PUBLIC_FOLDER")
    if not root:
        pytest.skip("GDRIVE_FSSPEC_PUBLIC_FOLDER is not configured")
    fs = GoogleDriveFileSystem(
        token="anon", root_file_id=root, skip_instance_cache=True
    )
    try:
        yield fs
    finally:
        fs.close()


def test_public_folder(public_drive: GoogleDriveFileSystem) -> None:
    entries = public_drive.ls("", detail=True)
    assert public_drive.isdir("")
    for entry in entries:
        assert public_drive.info(entry["name"])["id"] == entry["id"]
        if entry["type"] == "directory":
            public_drive.ls(entry["name"])


def test_public_binary_seek(public_drive: GoogleDriveFileSystem) -> None:
    path = os.environ.get("GDRIVE_FSSPEC_PUBLIC_BINARY")
    if not path:
        pytest.skip("GDRIVE_FSSPEC_PUBLIC_BINARY is not configured")
    with public_drive.open(path, "rb", block_size=64, cache_type="none") as opened:
        prefix = opened.read(64)
        assert opened.size >= len(prefix)
        opened.seek(0)
        assert opened.read(64) == prefix
        opened.seek(0, 2)
        assert opened.read(1) == b""


def test_public_native_snapshot(public_drive: GoogleDriveFileSystem) -> None:
    path = os.environ.get("GDRIVE_FSSPEC_PUBLIC_NATIVE")
    if not path:
        pytest.skip("GDRIVE_FSSPEC_PUBLIC_NATIVE is not configured")
    with public_drive.open(path, "rb") as opened:
        data = opened.read()
        assert data
        opened.seek(0)
        assert opened.read() == data
