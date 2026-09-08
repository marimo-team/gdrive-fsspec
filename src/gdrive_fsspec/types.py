"""Static types for Google Drive fsspec integration."""

from __future__ import annotations

from typing import Any, Literal

from typing_extensions import TypedDict


class _FsspecRequired(TypedDict):
    """Fields fsspec requires on every file-info dict."""

    name: str
    size: int
    type: Literal["file", "directory"]


class _Capabilities(TypedDict, total=False):
    """Subset of Drive v3 ``File.capabilities`` requested for permission checks."""

    canDelete: bool
    canTrash: bool


class _DrivePartialFields(TypedDict, total=False):
    """Subset of Drive v3 ``File`` fields requested via ``FIELDS``."""

    id: str
    mimeType: str
    trashed: bool
    version: str
    createdTime: str
    modifiedTime: str
    driveId: str
    capabilities: _Capabilities


class FileInfo(_FsspecRequired, _DrivePartialFields, extra_items=Any):
    """fsspec file-info dict: normalized path fields plus Drive API metadata."""


__all__ = ["FileInfo"]
