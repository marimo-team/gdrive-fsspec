"""Shared constants, aliases, and pure helpers for :mod:`gdrive_fsspec`.

This module is the foundation both :mod:`gdrive_fsspec.core` (the filesystem)
and :mod:`gdrive_fsspec._file` (the buffered file) build on. It imports nothing
from the rest of the package except :mod:`.types`, so it never participates in
an import cycle.
"""

from __future__ import annotations

import logging
import os
import pathlib
import ssl
from typing import TYPE_CHECKING, Any, Literal, Mapping, TypeAlias, cast

import httplib2

from .types import FileInfo

if TYPE_CHECKING:
    from googleapiclient._apis.drive.v3.schemas import File

DEFAULT_BLOCK_SIZE = 5 * 2**20
LOGGER = logging.getLogger("gdrive_fsspec")

# https://developers.google.com/workspace/drive/api/guides/api-specific-auth
SCOPE_DICT = {
    "full_control": "https://www.googleapis.com/auth/drive",
    "read_only": "https://www.googleapis.com/auth/drive.readonly",
}

# https://developers.google.com/workspace/drive/api/guides/mime-types
DIR_MIME_TYPE = "application/vnd.google-apps.folder"

# Base URL for resumable uploads.
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

# File resource fields; partial-response mask for files.list / files.get:
# https://developers.google.com/workspace/drive/api/reference/rest/v3/files#resource
# https://developers.google.com/workspace/drive/api/guides/performance#partial
_BASE_FIELDS = [
    "name",
    "id",
    "size",
    "trashed",
    "mimeType",
    "version",
    "createdTime",
    "modifiedTime",
]
INFO_FIELDS = ",".join(_BASE_FIELDS)

# Shared-drive role docs, surfaced in permission errors on delete/trash.
_SHARED_DRIVE_ROLES_URL = "https://support.google.com/a/answer/7337554"
# https://developers.google.com/workspace/drive/api/guides/delete#permissions for more info.

_TRASH_PERMISSION_MSG = "Insufficient permissions to move the file into Trash."
_TRASH_PERMISSION_SHARED_DRIVE_MSG = (
    "Insufficient permissions to move the file into Trash. Shared drives require "
    f"Content manager or Manager access. See {_SHARED_DRIVE_ROLES_URL}"
)
_DELETE_PERMISSION_SHARED_DRIVE_MSG = (
    "Insufficient permissions to permanently delete the file. Shared drives require "
    f"Manager access. See {_SHARED_DRIVE_ROLES_URL}"
)
_DELETE_PERMISSION_MSG = "Insufficient permissions to permanently delete the file."

_NUM_RETRIES = 5

# Changes.list partial-response mask. ``file`` is absent on removed changes; the
# ``parents`` sub-field carries the file's CURRENT parent folder ids, used to map
# a change to the cached directory listing(s) it invalidates.
# https://developers.google.com/workspace/drive/api/reference/rest/v3/changes/list
_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(changeType,removed,fileId,"
    "file(id,name,mimeType,trashed,parents,driveId))"
)
_CHANGES_PAGE_SIZE = 1000

# Page size for the single-name lookup in _find_child_by_name. One exact match
# is the norm; the pages exist only to settle rare case-variant collisions
# (Drive's name filter is case-insensitive), so a modest size bounds round-trips.
_FIND_CHILD_PAGE_SIZE = 100

# HTTP statuses returned for an expired/invalid Changes page token; recovery is
# to re-baseline the token and drop the whole cache (the gap can't be replayed).
_CHANGES_TOKEN_EXPIRED_STATUSES = frozenset({400, 404, 410})

# Transport-level failures worth retrying on the hand-rolled resumable-upload path
_RETRYABLE_TRANSPORT_ERRORS = (
    ConnectionError,
    TimeoutError,
    ssl.SSLError,
    httplib2.ServerNotFoundError,
)

# 403 ``reason``s that mean throttling rather than a real authorization failure
# See https://developers.google.com/workspace/drive/api/guides/handle-errors#rate-limit
_RETRYABLE_403_REASONS = frozenset({"userRateLimitExceeded", "rateLimitExceeded"})

AuthMethod = Literal["anon", "browser", "cache", "service_account"]
ROOT_ID = "root"
ROOT_DIR = ""

# One path element — matches fsspec.stringify_path
PathLike: TypeAlias = str | os.PathLike[str] | pathlib.Path


class MultipleFilesError(FileNotFoundError):
    pass


def _normalize_path(prefix: str, name: str) -> str:
    raw_prefix = prefix.strip("/")
    return "/" + "/".join([raw_prefix, name])


def _finfo_from_response(
    file: File | Mapping[str, Any], path_prefix: str | None = None
) -> FileInfo:
    # strictly speaking, other types might be capable of having children,
    # such as packages
    # TODO: check specifically for links
    file_type = "directory" if file.get("mimeType") == DIR_MIME_TYPE else "file"
    if path_prefix:
        name = _normalize_path(path_prefix, file["name"])
    else:
        name = file["name"]
    info: FileInfo = {
        "name": name.lstrip("/"),
        "size": int(file.get("size", 0)),
        "type": file_type,
    }
    return cast(FileInfo, {**file, **info})
