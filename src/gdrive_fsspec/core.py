from __future__ import annotations

import io
import json
import logging
import os
import pathlib
import random
import ssl
import time
import warnings
from functools import cached_property
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Mapping,
    MutableMapping,
    TypeAlias,
    cast,
    overload,
)
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httplib2
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem
from google.auth.credentials import AnonymousCredentials, Credentials
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, build_http
from typing_extensions import TypedDict

from .types import FileInfo
from .typing_utils import override
from .utils import escape_query_str, merge_fields

if TYPE_CHECKING:
    from googleapiclient._apis.drive.v3.resources import DriveResource
    from googleapiclient._apis.drive.v3.schemas import Change, Drive, File

    from .types import FilesResource

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


def _should_retry_status(status: int, content: bytes) -> bool:
    """Return whether a raw response is a transient failure worth retrying.

    Mirrors the Drive-aware policy the discovery client applies to ``.execute()``:
    https://github.com/googleapis/google-api-python-client/blob/main/googleapiclient/http.py#L80

    Also see https://developers.google.com/workspace/drive/api/guides/handle-errors
    """
    if status >= 500 or status == 429:
        return True
    if status != 403 or not content:
        return False

    try:
        error = json.loads(content.decode("utf-8")).get("error", {})
        reasons = {entry.get("reason") for entry in error.get("errors", [])}
    except (UnicodeDecodeError, ValueError, AttributeError):
        return False
    return bool(reasons & _RETRYABLE_403_REASONS)


def _normalize_path(prefix: str, name: str) -> str:
    raw_prefix = prefix.strip("/")
    return "/" + "/".join([raw_prefix, name])


def _with_supports_all_drives(url: str) -> str:
    """Return ``url`` with ``supportsAllDrives=true`` set, overriding any value.

    The resumable session URI is opaque and may already carry query parameters
    (``upload_id``, ``session_crd``). Setting the parameter via the query parser
    forces ``true`` even if the URL somehow already had ``supportsAllDrives=false``.
    """
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["supportsAllDrives"] = "true"
    return urlunsplit(parts._replace(query=urlencode(query)))


def _parse_range_end(range_header: str | None) -> int | None:
    """Last stored byte index from a resumable ``Range`` header.

    Resumable uploads report stored bytes as ``bytes=0-<end>`` (the optional
    ``bytes=`` unit is tolerated). Only ranges starting at ``0`` are accepted —
    anything else (a non-zero start, missing dash, or non-integer end) returns
    ``None``, since ``_consume_accepted`` assumes the range covers from the
    start of the object and a malformed value would miscount accepted bytes.
    """
    if not range_header:
        return None
    spec = range_header.strip().removeprefix("bytes=")
    start, sep, end = spec.partition("-")
    if not sep or start != "0":
        return None
    try:
        return int(end)
    except ValueError:
        return None


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


class MultipleFilesError(FileNotFoundError):
    pass


AuthMethod = Literal["anon", "browser", "cache", "service_account"]
ROOT_ID = "root"
ROOT_DIR = ""

# One path element — matches fsspec.stringify_path
PathLike: TypeAlias = str | os.PathLike[str] | pathlib.Path


class _PageListKwargs(TypedDict, total=False):
    """The ``list`` method doesn't enforce good type params, so we use a TypedDict to help."""

    pageToken: str


class GoogleDriveFileSystem(AbstractFileSystem):
    """Access Google Drive as a file system.

    In the Google Drive API, everything is a file resource. Folders are files
    with a special MIME type.

    Note:
        We assume that each path identifies a unique file. In Google Drive, it
        is possible to have multiple identically named files, and this will
        result in errors in this implementation.
    """

    protocol = "gdrive"
    root_marker = ""

    if TYPE_CHECKING:
        service: DriveResource
        files: FilesResource
        authed_http: AuthorizedHttp
        # pyrefly: ignore [bad-override-mutable-attribute]  # fsspec DirCache is untyped
        dircache: MutableMapping[str, list[FileInfo]]
        _auth_method: AuthMethod | None
        _is_anonymous: bool

    def __init__(
        self,
        root_file_id: str | None = None,
        token: AuthMethod = "cache",
        access: Literal["full_control", "read_only"] = "full_control",
        spaces: str = "drive",
        creds: dict[str, Any] | str | None = None,
        drive: str | None = None,
        auth_kwargs: dict[str, Any] | None = None,
        changes_sync_interval: float | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a Google Drive file system.

        Args:
            root_file_id: Folder file ID to use as the filesystem root (the empty
                path ``""``). Obtain it from a folder URL such as
                ``https://drive.google.com/drive/folders/<id>``. If omitted,
                defaults to the shared-drive root when ``drive`` is set, otherwise
                ``"root"`` (the authenticated user's My Drive). A shared-drive ID
                is also accepted here for backwards compatibility (when it cannot
                be resolved as a file, it is treated as a shared drive and
                ``drive`` is set from it), but this is a legacy path; prefer
                passing ``drive`` to target a shared drive.
            token: One of ``"anon"``, ``"browser"``, ``"cache"``,
                ``"service_account"``. Using ``"browser"`` will prompt a URL to
                be opened in a browser and cache the response for future use with
                ``token="cache"``. ``"browser"`` removes any previously cached
                token file, if it exists.
            access: One of ``"full_control"``, ``"read_only"``.
            spaces: Category of files to search; can be ``"drive"``,
                ``"appDataFolder"``, and ``"photos"``. Of these, only the first
                is general.
            creds: Required for ``"service_account"`` token. A dict with the
                service account credentials from the GCP console (same content
                as the downloaded JSON). See
                https://cloud.google.com/iam/docs/service-account-creds#key-types.
                Files must be shared with the service account email from that JSON.
            drive: A shared drive to scope API calls to, given as either its ID
                or its name. Resolved against the drives accessible to the current
                credentials (via ``drives.list``). Required for service-account
                uploads. If omitted, operations use the user's My Drive (or
                anonymous public files when ``token="anon"``). Combine with
                ``root_file_id`` to start below the shared-drive root, e.g. a
                subfolder ID inside that drive.
            auth_kwargs: Additional keyword arguments passed to the authentication
                backend (``pydata_google_auth.get_user_credentials`` for user
                OAuth, or
                ``service_account.Credentials.from_service_account_info`` for
                service accounts). For headless or remote environments where a
                local callback server is unavailable, pass
                ``use_local_webserver=False`` to request a token via the console.
            changes_sync_interval: If set (in seconds), opt into reconciling
                out-of-band Drive changes (files created, modified, moved,
                trashed, or deleted by other processes or the Drive web UI) into
                the directory-listing cache. Before a cached listing is served by
                ``ls``/``info``, changes since the last sync are polled via the
                Drive Changes API, at most once per interval. Affected listings
                are dropped so they refresh on demand. ``None`` (default)
                disables the feature entirely — no extra API calls and zero cost
                on the read path. Requires authentication. Reconciliation is
                best-effort: a sync failure is logged and the cached listings are
                served unchanged rather than raising. Note: the filesystem's own
                writes also appear in the change feed, so a directory just
                written may be re-listed once on the next read after a sync. If a
                sync-enabled instance is shared across threads, a sync may
                reconcile against a slightly stale snapshot of the cache.
            **kwargs: Passed to the parent class.
        """
        # Ideally, these should be keyword-arguments, but to maintain backwards compatibility, we keep the existing API.

        super().__init__(**kwargs)
        self.access = access
        self.scopes = [SCOPE_DICT[access]]
        self.spaces = spaces
        self.creds = creds
        self.drive = drive
        self.auth_kwargs = auth_kwargs or {}
        self.connect(method=token)
        if token == "anon":
            self.drive = None
        elif drive:
            self.drive = self._resolve_drive_id(drive)

        if root_file_id and root_file_id != ROOT_ID:
            self._validate_root_file_id(root_file_id)

        self.root_file_id = root_file_id or self.drive or ROOT_ID

        if self._is_anonymous and changes_sync_interval is not None:
            raise ValueError("Changes sync is not supported for anonymous access")

        self._changes_sync_interval = changes_sync_interval
        self._changes_page_token: str | None = None
        self._last_sync_monotonic: float | None = None
        if self._changes_sync_interval is not None:
            # Baseline the changes token eagerly so the first cached read can already reconcile.
            try:
                self._changes_page_token = self._get_start_page_token()
            except HttpError:
                LOGGER.warning(
                    "Could not baseline the changes token at construction; "
                    "will retry on first sync",
                    exc_info=True,
                )

    def _validate_root_file_id(self, root_file_id: str) -> None:
        try:
            meta = self.files.get(
                fileId=root_file_id,
                fields="id,trashed,mimeType,driveId",
                supportsAllDrives=True,
            ).execute(num_retries=_NUM_RETRIES)
        except HttpError as err:
            if err.status_code != 404:
                raise
            self._confirm_shared_drive_root(root_file_id)
            return

        if meta.get("trashed"):
            raise FileNotFoundError(f"root_file_id {root_file_id!r} is trashed")
        if meta.get("mimeType") != DIR_MIME_TYPE:
            raise NotADirectoryError(f"root_file_id {root_file_id!r} is not a folder")
        # Check if the root file is in the correct drive.
        if self.drive is not None and meta.get("driveId") != self.drive:
            raise ValueError(
                f"root_file_id {root_file_id!r} is not in drive {self.drive!r}"
            )

    def _confirm_shared_drive_root(self, drive_id: str) -> None:
        """Accept a shared-drive ID passed as ``root_file_id`` (legacy).

        Older versions documented ``root_file_id`` as accepting a "share, drive or
        folder ID", so a shared-drive ID may be passed here. When the ID does not
        resolve as a file, fall back to treating it as a shared drive and set
        ``self.drive`` from it so directory listings are scoped correctly.

        Args:
            drive_id: Shared-drive ID to validate.

        Note:
            Prefer passing ``drive`` instead of using this legacy path.
        """
        try:
            self.service.drives().get(driveId=drive_id).execute(
                num_retries=_NUM_RETRIES
            )
        except HttpError as err:
            if err.status_code == 404:
                raise FileNotFoundError(f"root_file_id {drive_id!r} not found") from err
            raise
        if self.drive is None:
            self.drive = drive_id
        elif self.drive != drive_id:
            raise ValueError(
                f"root_file_id {drive_id!r} conflicts with drive {self.drive!r}"
            )

    def connect(self, method: AuthMethod | None = None) -> None:
        if method == "browser":
            cred = self._connect_browser()
        elif method == "cache":
            cred = self._connect_cache()
        elif method == "anon":
            cred = AnonymousCredentials()
        elif method == "service_account":
            cred = self._connect_service_account()
        else:
            raise ValueError(f"Invalid connection method `{method}`.")

        self._auth_method = method
        self._is_anonymous = method == "anon"

        # Own the authenticated transport explicitly. Sharing the transport
        # keeps credential refresh and connection state in one place.
        self.authed_http = AuthorizedHttp(
            cred, http=build_http()
        )  # TODO: should we make this private?

        # AuthorizedHttp duck-types as httplib2.Http (this is exactly what
        # googleapiclient passes internally), but the stubs only accept Http.
        # pyrefly: ignore [no-matching-overload]
        self.service = build("drive", "v3", http=self.authed_http)
        self.files = self.service.files()

    @property
    def srv(self) -> DriveResource:
        """Deprecated alias for :attr:`service`."""
        warnings.warn(
            "`srv` is deprecated; use `service` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.service

    @property
    def _user_credentials_cache_path(self) -> str:
        import pydata_google_auth.cache

        return pydata_google_auth.cache.READ_WRITE._path

    def _connect_browser(self) -> Credentials:
        try:
            os.remove(self._user_credentials_cache_path)
        except OSError:
            pass
        return self._connect_cache()

    def _connect_cache(self) -> Credentials:
        import pydata_google_auth

        kwargs = {"use_local_webserver": True, **self.auth_kwargs}
        return pydata_google_auth.get_user_credentials(self.scopes, **kwargs)

    def _connect_service_account(self) -> Credentials:
        if isinstance(self.creds, str):
            if not self.creds.strip():
                raise ValueError("Empty credentials are not allowed")
            if self.creds[0] != "{":
                creds = json.load(open(self.creds))
            else:
                creds = json.loads(self.creds)
        else:
            creds = self.creds
        return service_account.Credentials.from_service_account_info(
            info=creds, scopes=self.scopes, **self.auth_kwargs
        )

    @cached_property
    def drives(self) -> list[Drive]:
        """Shared drives accessible to the current user.

        drives.list only returns shared drives
        https://developers.google.com/workspace/drive/api/reference/rest/v3/drives/list

        Returns:
            List of drive resource dicts from the Drive API.
        """
        drives: list[Drive] = []
        page_token: str | None = None
        while True:
            page_kwargs: _PageListKwargs = {}
            if page_token:
                page_kwargs["pageToken"] = page_token

            response = (
                self.service.drives()
                .list(**page_kwargs)
                .execute(num_retries=_NUM_RETRIES)
            )
            drives.extend(response["drives"])
            page_token = response.get("nextPageToken")
            if page_token is None:
                break
        return drives

    @cached_property
    def export_formats(self) -> dict[str, list[str]]:
        """Supported export conversions, keyed by source MIME type.

        Maps each Google-native MIME type (e.g.
        ``application/vnd.google-apps.document``) to the list of target MIME
        types it can be exported to. Fetched once from the Drive ``about``
        resource and cached for the lifetime of the filesystem.

        Returns:
            Mapping of source MIME type to its list of valid export targets.
        """
        about = (
            self.service.about()
            .get(fields="exportFormats")
            .execute(num_retries=_NUM_RETRIES)
        )
        return about.get("exportFormats", {})

    def _path_str(self, path: PathLike) -> str:
        """Strip the protocol and normalize a path-like input to a single string.

        Args:
            path: A ``str``, ``os.PathLike``, or ``pathlib.Path``.

        Returns:
            The protocol-stripped path as a string.

        Raises:
            TypeError: If ``path`` resolves to a sequence of paths.
        """
        stripped = self._strip_protocol(path)
        if isinstance(stripped, list):
            raise TypeError("expected a single path, not a sequence of paths")
        return stripped

    # Return type is Any (not drive.v3 File) to match fsspec AbstractFileSystem.mkdir.
    @override
    def mkdir(self, path: PathLike, create_parents: bool = True, **kwargs: Any) -> Any:
        """Create a directory at the given path.

        Args:
            path: Directory path to create.
            create_parents: If True, create any missing parent directories first.
            **kwargs: Ignored; accepted for fsspec compatibility.

        Returns:
            The created folder's file resource dict from the Drive API (drive.v3
            ``File`` at runtime).

        Raises:
            FileExistsError: If a file or folder already exists at ``path``.
        """
        parent = self._parent(path)
        if create_parents and parent:
            self.makedirs(parent, exist_ok=True)

        stripped_path = self._path_str(path)
        if self.exists(stripped_path):
            raise FileExistsError(stripped_path)

        parent_id = self._path_to_id(parent)
        meta = {
            "name": stripped_path.rstrip("/").rsplit("/", 1)[-1],
            "mimeType": DIR_MIME_TYPE,
            "parents": [parent_id],
        }
        LOGGER.debug(f"Creating {stripped_path}, child of {parent_id}")
        out: File = self.files.create(body=meta, supportsAllDrives=True).execute(
            num_retries=_NUM_RETRIES
        )
        if parent in self.dircache:
            self.dircache[parent].append(_finfo_from_response(out, path_prefix=parent))
        self.dircache[stripped_path] = []
        return out

    @override
    def makedirs(self, path: PathLike, exist_ok: bool = True) -> None:
        """Create a directory and any missing parent directories.

        Args:
            path: Directory path to create (may include nested components).
            exist_ok: If False, raise when the final path component already exists.

        Raises:
            FileExistsError: If ``exist_ok`` is False and the directory already exists.
        """
        parts = self._path_str(path).split("/")
        path = ""
        for i, part in enumerate(parts):
            path = path + "/" + part if path else part
            if not self.exists(path):
                self.mkdir(path, create_parents=False)
            elif i == len(parts) - 1 and not exist_ok:
                raise FileExistsError(path)

    # fsspec's base rm_file delegates to _rm; we implement delete here so both
    # rm_file and rm share the same Drive trash/permanent behavior.
    @override
    def _rm(self, path: PathLike, permanent: bool = False) -> None:
        """Delete a single file or directory by path.

        Args:
            path: Path of the file or folder to delete.
            permanent: If True, permanently delete the file instead of moving to trash.
        """
        stripped_path = self._path_str(path)
        if stripped_path == "":
            raise ValueError("Cannot delete the filesystem root")
        file_info = self.info(
            stripped_path, fields="driveId,capabilities/canDelete,capabilities/canTrash"
        )
        file_id = file_info["id"]
        LOGGER.debug(f"Removing {stripped_path}, file_id={file_id}")

        on_shared_drive = bool(file_info.get("driveId"))

        # Sometimes, the file exists but delete reports a 404 error.
        # This is due to a permission issue rather than a file not found error.
        # https://github.com/marimo-team/gdrive-fsspec/issues/19
        # So we check whether we can delete it first.
        if permanent:
            can_delete = file_info.get("capabilities", {}).get("canDelete", False)
            if not can_delete and on_shared_drive:
                raise PermissionError(_DELETE_PERMISSION_SHARED_DRIVE_MSG)
            elif not can_delete:
                raise PermissionError(_DELETE_PERMISSION_MSG)
            self.files.delete(fileId=file_id, supportsAllDrives=True).execute(
                num_retries=_NUM_RETRIES
            )
        else:
            can_trash = file_info.get("capabilities", {}).get("canTrash", False)
            if not can_trash and on_shared_drive:
                raise PermissionError(_TRASH_PERMISSION_SHARED_DRIVE_MSG)
            elif not can_trash:
                raise PermissionError(_TRASH_PERMISSION_MSG)
            self._trash_file(file_id)

        parent = self._parent(stripped_path)
        if parent in self.dircache:
            listing = self.dircache[parent]
            self.dircache[parent] = [
                li for li in listing if li["name"] != stripped_path
            ]

        # Drop any cached listing rooted at the deleted path (it was a directory).
        self.dircache.pop(stripped_path, None)

    @override
    def rm(
        self,
        path: PathLike,
        recursive: bool = True,
        maxdepth: int | None = None,
        permanent: bool = False,
    ) -> None:
        """Delete a file or directory.

        By default the file is moved to the trash, matching the Google Drive UI
        (and recoverable from there). Pass ``permanent=True`` to hard-delete.

        Args:
            path: Path of the file or folder to delete.
            recursive: If False, refuse to delete a non-empty directory.
            maxdepth: Ignored; accepted for fsspec compatibility.
            permanent: If True, permanently delete instead of moving to trash.
                This is irreversible, refer to https://developers.google.com/workspace/drive/api/guides/delete#permissions for permissions.

        Raises:
            ValueError: If ``recursive`` is False and the directory is not empty.
        """
        if recursive is False and self.isdir(path) and self.ls(path):
            raise ValueError("Attempt to delete non-empty folder")
        self._rm(path, permanent=permanent)

    @override
    def rmdir(self, path: PathLike, permanent: bool = False) -> None:
        """Remove an empty directory.

        Args:
            path: Path of the directory to remove.
            permanent: If True, permanently delete instead of moving to trash.

        Raises:
            ValueError: If ``path`` is not a directory or is not empty.
        """
        if not self.isdir(path):
            raise ValueError("Path is not a directory")
        self.rm(path, recursive=False, permanent=permanent)

    @override
    def invalidate_cache(self, path: PathLike | None = None) -> None:
        if path is None:
            self.dircache.clear()
        else:
            self.dircache.pop(self._path_str(path), None)
        super().invalidate_cache(path)

    # ----------------------------------
    # Changes-API cache synchronization
    # ----------------------------------

    def _maybe_sync_cache(self) -> None:
        """Cache-read hook: reconcile out-of-band changes, at most once per interval.

        Best-effort: any failure is logged and swallowed so a cached ``ls``/
        ``info`` is never turned into an error.
        """
        if self._changes_sync_interval is None or self._is_anonymous:
            return
        last = self._last_sync_monotonic
        now = time.monotonic()
        if last is not None and now - last < self._changes_sync_interval:
            return
        self._last_sync_monotonic = now
        try:
            self._sync_cache()
        except Exception:
            LOGGER.warning("Cache sync failed; serving cached listings", exc_info=True)

    def _sync_cache(self) -> None:
        """Reconcile out-of-band Drive changes into the dircache.

        Drops cached listings that a change since the last sync could have made
        stale (see :meth:`_plan_invalidations`), falling back to clearing the
        whole cache when a change cannot be mapped or the page token expired. The
        very first call has no page token yet and only establishes the baseline.
        """
        if self._changes_page_token is None:
            self._changes_page_token = self._get_start_page_token()
            return
        try:
            changes, new_token = self._iter_changes(self._changes_page_token)
        except HttpError as err:
            if err.status_code in _CHANGES_TOKEN_EXPIRED_STATUSES:
                # The change gap can't be replayed; re-baseline and drop the cache.
                LOGGER.debug(
                    "Changes token expired (%s), error: %s, clearing cache",
                    err.status_code,
                    err.error_details,
                )
                self._changes_page_token = self._get_start_page_token()
                self.dircache.clear()
                return
            raise

        self._changes_page_token = new_token
        if not changes:
            return

        dir_id_to_path = self._build_dir_id_to_path()
        id_to_paths = self._build_id_to_paths()
        to_invalidate, full_clear = self._plan_invalidations(
            changes, dir_id_to_path, id_to_paths
        )
        if full_clear:
            LOGGER.debug("Unmappable change; clearing dircache")
            self.dircache.clear()
            return
        for path in to_invalidate:
            self.dircache.pop(path, None)

    def _get_start_page_token(self) -> str:
        """Baseline Changes page token for this instance's corpus."""
        kwargs: dict[str, Any] = {}
        if self.drive is not None:
            kwargs = dict(driveId=self.drive, supportsAllDrives=True)
        response = (
            self.service.changes()
            .getStartPageToken(**kwargs)
            .execute(num_retries=_NUM_RETRIES)
        )
        return response["startPageToken"]

    def _iter_changes(self, page_token: str) -> tuple[list[Change], str]:
        """Drain all changes from ``page_token``.

        Args:
            page_token: The saved page token to resume from.

        Returns:
            A ``(changes, new_start_token)`` pair, where ``new_start_token`` is
            the token to persist for the next sync.

        Raises:
            HttpError: If the token is invalid or expired (caller re-baselines).
        """
        changes: list[Change] = []
        new_start_token: str | None = None
        scope = self._changes_scope_kw()
        token: str | None = page_token
        while token is not None:
            response = (
                self.service.changes()
                .list(
                    pageToken=token,
                    spaces=self.spaces,
                    includeRemoved=True,
                    fields=_CHANGES_FIELDS,
                    pageSize=_CHANGES_PAGE_SIZE,
                    **scope,
                )
                .execute(num_retries=_NUM_RETRIES)
            )
            changes.extend(response.get("changes", []))
            new_start_token = response.get("newStartPageToken", new_start_token)
            token = response.get("nextPageToken")
        if new_start_token is None:
            # The final page always carries newStartPageToken; treat its absence
            # as a token to re-baseline rather than silently losing progress.
            raise RuntimeError("changes.list returned no newStartPageToken")
        return changes, new_start_token

    def _changes_scope_kw(self) -> dict[str, Any]:
        """Drive-scoping kwargs for ``changes.list`` (empty for My Drive).

        Distinct from :meth:`_drive_kw`, which emits ``corpora`` — a
        ``files.list`` parameter that ``changes.list`` does not accept.
        """
        if self.drive is not None:
            return dict(
                driveId=self.drive,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
        return {}

    @cached_property
    def _resolved_root_id(self) -> str:
        """The real folder ID of the filesystem root.

        ``root_file_id`` may be the ``"root"`` alias (My Drive), but the Drive
        API — notably the change feed — reports the concrete folder ID. Resolve
        the alias once via ``files.get``; a real ID (shared drive or an explicit
        ``root_file_id``) is returned unchanged with no API call.
        """
        if self.root_file_id != ROOT_ID:
            return self.root_file_id
        meta = self.files.get(fileId=ROOT_ID, fields="id").execute(
            num_retries=_NUM_RETRIES
        )
        return meta["id"]

    def _build_dir_id_to_path(self) -> dict[str, str]:
        """Map each cached directory's folder ID to its cached path.

        A directory's ID is learned from the entry representing it inside its
        parent's cached listing; the root maps to :attr:`_resolved_root_id` (the
        concrete id, since the change feed never uses the ``"root"`` alias). A
        cached directory whose parent is not cached has no derivable ID and is
        absent here — see :meth:`_plan_invalidations` for how that is handled.
        """
        dir_id_to_path: dict[str, str] = {self._resolved_root_id: ROOT_DIR}
        # Snapshot the cache: another thread's ls/rm/sync may mutate it, and
        # iterating a live dict would raise "changed size during iteration".
        for listing in list(self.dircache.values()):
            for entry in listing:
                if entry.get("type") == "directory" and "id" in entry:
                    dir_id_to_path[entry["id"]] = entry["name"]
        return dir_id_to_path

    def _build_id_to_paths(self) -> dict[str, list[str]]:
        """Map each cached file ID to the directory path(s) that list it."""
        id_to_paths: dict[str, list[str]] = {}
        # Snapshot the cache (see _build_dir_id_to_path).
        for dir_path, listing in list(self.dircache.items()):
            for entry in listing:
                file_id = entry.get("id")
                if file_id is not None:
                    id_to_paths.setdefault(file_id, []).append(dir_path)
        return id_to_paths

    def _plan_invalidations(
        self,
        changes: list[Change],
        dir_id_to_path: dict[str, str],
        id_to_paths: dict[str, list[str]],
    ) -> tuple[set[str], bool]:
        """Decide which cached directories a batch of changes invalidates.

        Returns ``(paths_to_drop, full_clear)``. A cached listing goes stale only if a
        change touches one of its direct children, which is caught either via the
        change's current parents (new location) or via ``id_to_paths`` (old
        location). An unresolved parent can only endanger a cached directory
        whose own ID is underivable, so it forces a full clear only when such an
        unmapped directory is actually cached.
        """
        mapped_paths = set(dir_id_to_path.values())
        # Snapshot the keys: a concurrent mutation must not raise mid-iteration.
        has_unmapped_dir = any(path not in mapped_paths for path in list(self.dircache))

        to_invalidate: set[str] = set()
        for change in changes:
            file_id = change.get("fileId")
            # Old location: any cached dir currently listing this id may now be
            # stale (moved out, renamed, trashed, or deleted). Fires even for a
            # removed change, since the index is built from the pre-sync cache.
            if file_id is not None and file_id in id_to_paths:
                to_invalidate.update(id_to_paths[file_id])

            # The changed item may itself be a cached directory that was moved,
            # renamed, or removed. Its own listing (and everything under it) is
            # then keyed by a now-stale path, so drop that whole subtree.
            if file_id is not None and file_id in dir_id_to_path:
                to_invalidate.update(self._cached_subtree(dir_id_to_path[file_id]))

            file = change.get("file")
            if change.get("removed") or file is None:
                # No parents to inspect; the old-location step above is all we can
                # (and need to) do for a removed or file-less change.
                continue

            # New location: each current parent that maps to a cached directory
            # may now be stale (child added, renamed, or moved in).
            for parent_id in file.get("parents") or []:
                if parent_id in dir_id_to_path:
                    to_invalidate.add(dir_id_to_path[parent_id])
                elif has_unmapped_dir:
                    # The parent could be a cached-but-unmapped directory we
                    # can't identify; the only safe response is to drop everything.
                    return set(), True
        return to_invalidate, False

    def _cached_subtree(self, path: str) -> set[str]:
        """Cached dircache keys at ``path`` and everything nested beneath it."""
        prefix = path + "/"
        # Snapshot the keys so a concurrent mutation can't raise mid-iteration.
        return {
            key for key in list(self.dircache) if key == path or key.startswith(prefix)
        }

    # ------------------------------------------------------------------
    # fsspec surface: listing & metadata
    # ------------------------------------------------------------------

    @overload
    # pyrefly: ignore [bad-override]  # overloads diverge from base ls signature
    def ls(
        self,
        path: PathLike,
        detail: Literal[False] = False,
        trashed: bool = False,
        **kwargs: Any,
    ) -> list[str]: ...

    @overload
    def ls(
        self,
        path: PathLike,
        detail: Literal[True],
        trashed: bool = False,
        fields: str | None = None,
        **kwargs: Any,
    ) -> list[FileInfo]: ...

    @override
    def ls(
        self,
        path: PathLike,
        detail: bool = False,
        trashed: bool = False,
        fields: str | None = None,
        **kwargs: Any,
    ) -> list[str] | list[FileInfo]:
        """List files and directories under ``path``.

        Args:
            path: Directory path to list. Use ``""`` for the filesystem root.
            detail: If True, return full file-info dicts; otherwise return paths only.
            trashed: If True, include trashed items in the listing.
            fields: Extra Drive fields to request on top of the defaults, as a
                comma-separated string (e.g. ``"driveId,capabilities/canDelete"``).
                See https://developers.google.com/workspace/drive/api/reference/rest/v3/files#resource.
            kwargs: Not used; accepted for fsspec compatibility.

        Returns:
            Sorted list of child paths, or list of file-info dicts when ``detail``
            is True.

        Raises:
            ValueError: If ``fields`` is given while ``detail`` is False, since the
                names-only result would discard the requested fields.
            FileNotFoundError: If ``path`` does not exist.
            MultipleFilesError: If multiple files share the same path name.
        """
        # A blank mask means "no extra fields".
        fields = (fields or "").strip() or None
        if fields is not None and not detail:
            raise ValueError(
                "fields requires detail=True; names-only output discards the requested fields"
            )

        stripped_path: str = self._path_str(path)

        # We only check the cache for typical API calls, we avoid caching if user passes extra fields or trashed files.
        use_cache = fields is None and not trashed

        # Reconcile out-of-band changes before serving anything from the cache;
        # skipped on the non-caching paths (fields/trashed), which hit the API.
        if use_cache:
            self._maybe_sync_cache()

        entry: FileInfo | None = None
        if stripped_path != ROOT_DIR and not (
            use_cache and stripped_path in self.dircache
        ):
            entry = self._resolve_entry(stripped_path, trashed=trashed)

        if use_cache and (cached := self.dircache.get(stripped_path)) is not None:
            files = cached
        elif entry is not None and entry["type"] != "directory":
            # `ls` on a file returns the file's own info; never cached under
            # the file path.
            if fields is not None:
                meta = self._get_file(entry["id"], fields=fields)
                entry = _finfo_from_response(
                    meta, path_prefix=self._parent(stripped_path)
                )
            files = [entry]
        else:
            dir_id = entry["id"] if entry is not None else self.root_file_id
            files = self._list_children(
                dir_id, trashed=trashed, path_prefix=stripped_path, fields=fields
            )
            if use_cache:
                self.dircache[stripped_path] = files

        if detail:
            return files
        else:
            return sorted([file["name"] for file in files])

    @override
    def info(
        self,
        path: PathLike,
        trashed: bool = False,
        fields: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return metadata for a file or directory.

        Args:
            path: Path to inspect. Use ``""`` for the filesystem root.
            trashed: If True, allow resolving trashed files.
            fields: Extra Drive fields to request on top of the defaults, as a
                comma-separated string (e.g. ``"driveId,capabilities/canDelete"``).
                See https://developers.google.com/workspace/drive/api/reference/rest/v3/files#resource.
            kwargs: Additional arguments to pass to the ``files.get`` request.

        Returns:
            File-info dict including ``name``, ``type``, ``size``, and Drive API fields.
        """
        stripped_path = self._path_str(path)
        if stripped_path == "":
            info: FileInfo = {
                "name": stripped_path,
                "mimeType": DIR_MIME_TYPE,
                "type": "directory",
                "size": 0,
                "id": self.root_file_id,
            }
            return cast(dict[str, Any], info)

        # A blank mask means "no extra fields".
        fields = (fields or "").strip() or None

        # Resolution reads cached ancestor listings whenever trashed is False
        # (see _resolve_entry); reconcile out-of-band changes first.
        if not trashed:
            self._maybe_sync_cache()

        file_info = self._resolve_entry(stripped_path, trashed=trashed)
        if fields is not None:
            # The resolved entry only carries the default fields, so fetch the
            # requested extras fresh via files.get — O(1) in directory size.
            meta = self._get_file(file_info["id"], fields=fields, **kwargs)
            file_info = _finfo_from_response(
                meta, path_prefix=self._parent(stripped_path)
            )
        return cast(dict[str, Any], file_info)

    def export(self, path: PathLike, mime_type: str) -> bytes:
        """Export a Google-native file to another format and download it.

        Use this for Docs, Sheets, Slides, and other Google Workspace files that
        cannot be downloaded directly with ``open(..., "rb")``.

        Args:
            path: Path of the Google-native file to export.
            mime_type: Target MIME type for the export (e.g. ``"text/plain"``).
                Must be one of the conversions Drive supports for this file's
                type; see :attr:`export_formats`.

        Returns:
            Exported file content as bytes.

        Raises:
            ValueError: If ``mime_type`` is not a supported export target for
                this file's source type.
        """
        info = self.info(path)
        file_id = info["id"]
        source_mime = info.get("mimeType", "")
        targets = self.export_formats.get(source_mime, [])
        if mime_type not in targets:
            valid = ", ".join(targets) if targets else "none"
            raise ValueError(
                f"Cannot export {path!r} (type {source_mime!r}) to {mime_type!r}. Supported export types: {valid}."
            )

        request = self.files.export_media(fileId=file_id, mimeType=mime_type)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=_NUM_RETRIES)
        return buffer.getvalue()

    # ------------------------------------------------------------------
    # Path resolution: path -> FileInfo / file ID
    # ------------------------------------------------------------------

    def _resolve_entry(self, path: PathLike, *, trashed: bool = False) -> FileInfo:
        """Resolve a path to its FileInfo, walking down from a cached ancestor.

        Starts at the deepest ancestor whose listing is cached and resolves
        each remaining component with a targeted single-name query. Cached listings are
        authoritative: a name missing from one raises without an API call.
        When ``trashed`` is True the cache is skipped entirely, since cached
        listings exclude trashed files.

        Args:
            path: Path to resolve.
            trashed: If True, allow resolving trashed files.

        Returns:
            The FileInfo for the resolved path.

        Raises:
            ValueError: If called with the root path, which has no entry of
                its own — callers must handle the root themselves.
            FileNotFoundError: If ``path`` or any of its components does not exist.
            MultipleFilesError: If multiple files share the same path name.
        """
        stripped_path = self._path_str(path)
        if stripped_path == "":
            raise ValueError("_resolve_entry is not defined for the root path")
        parts = stripped_path.split("/")

        start = 0
        if not trashed:
            for i in range(len(parts) - 1, -1, -1):
                if "/".join(parts[:i]) in self.dircache:
                    start = i
                    break

        entry: FileInfo | None = None
        parent_id = self.root_file_id
        for depth in range(start, len(parts)):
            parent_path = "/".join(parts[:depth])
            child_path = "/".join(parts[: depth + 1])
            listing = self.dircache.get(parent_path) if not trashed else None
            if listing is not None:
                matches = [f for f in listing if f["name"] == child_path]
                if len(matches) > 1:
                    raise MultipleFilesError(child_path)
                entry = matches[0] if matches else None
            else:
                entry = self._find_child_by_name(
                    parent_id, parts[depth], trashed=trashed, path_prefix=parent_path
                )
            if entry is None:
                raise FileNotFoundError(child_path)
            if depth < len(parts) - 1 and entry["type"] != "directory":
                # An intermediate component is not a folder, so nothing can exist below it.
                raise FileNotFoundError("/".join(parts[: depth + 2]))
            parent_id = entry["id"]
        if entry is None:
            raise FileNotFoundError(stripped_path)
        return entry

    def _path_to_id(self, path: PathLike, trashed: bool = False) -> str:
        if self._path_str(path) == "":
            return self.root_file_id
        return self._resolve_entry(path, trashed=trashed)["id"]

    def _resolve_drive_id(self, drive: str) -> str:
        """Resolve a shared-drive ID or name to its drive ID.

        Args:
            drive: Shared-drive ID or human-readable name.

        Returns:
            The matching shared-drive ID.

        Raises:
            ValueError: If no drive matches, or the name matches multiple drives.
        """
        if any(d["id"] == drive for d in self.drives):
            return drive
        matches = [d["id"] for d in self.drives if d["name"] == drive]
        if len(matches) == 1:
            return matches[0]
        if len(matches) == 0:
            raise ValueError(f"Drive {drive!r} not found by id or name")
        raise ValueError(f"Drive name {drive!r} refers to multiple shared drives")

    # ------------------------------------------------------------------
    # ID-space Drive operations: speak in file IDs, no path knowledge
    # (``path_prefix`` is only a naming hint for returned entries)
    # ------------------------------------------------------------------

    def _drive_kw(self) -> dict[str, Any]:
        if self.drive is not None:
            return dict(
                includeItemsFromAllDrives=True,
                corpora="drive",
                supportsAllDrives=True,
                driveId=self.drive,
            )
        else:
            empty: dict[str, Any] = {}
            return empty

    def _parent_query_id(self, file_id: str) -> str:
        """Id to use in a ``'<id>' in parents`` filter.

        Substitutes the shared-drive id for the ``"root"`` alias, since a
        shared drive's top-level folder is queried by its drive id.
        """
        if file_id == ROOT_ID and self.drive is not None:
            return self.drive
        return file_id

    def _get_file(
        self, file_id: str, *, fields: str | None = None, **kwargs: Any
    ) -> File:
        """Fetch a file resource by ID via ``files.get``.

        Args:
            file_id: File ID to fetch.
            fields: Extra Drive fields to request on top of ``INFO_FIELDS``.
            **kwargs: Additional arguments for the ``files.get`` request;
                reserved argument names are dropped.

        Returns:
            The raw Drive file resource dict.
        """
        for reserved in ("fileId", "fields", "supportsAllDrives"):
            kwargs.pop(reserved, None)
        return self.files.get(
            fileId=file_id,
            fields=merge_fields(INFO_FIELDS, fields),
            supportsAllDrives=True,
            **kwargs,
        ).execute(num_retries=_NUM_RETRIES)

    def _list_children(
        self,
        file_id: str,
        trashed: bool = False,
        path_prefix: str | None = None,
        fields: str | None = None,
    ) -> list[FileInfo]:
        """List every child of a folder by ID, paginating ``files.list``.

        Args:
            file_id: File ID of the folder to list.
            trashed: If True, include trashed items in the listing.
            path_prefix: Parent path used to build the entries' names.
            fields: Extra Drive fields to request on top of the defaults.

        Returns:
            FileInfo entries for all children, in name order.
        """
        all_files: list[FileInfo] = []
        page_token: str | None = None

        file_fields = merge_fields(INFO_FIELDS, fields)
        all_fields = f"nextPageToken, files({file_fields})"

        query = f"'{self._parent_query_id(file_id)}' in parents "
        if not trashed:
            query += "and trashed = false "
        kwargs = self._drive_kw()
        while True:
            LOGGER.debug("%s ; prefix %s", query, path_prefix)
            page_kwargs: _PageListKwargs = {}
            if page_token:
                page_kwargs["pageToken"] = page_token

            response = self.files.list(
                q=query,
                spaces=self.spaces,
                fields=all_fields,
                orderBy="name",
                pageSize=1000,
                **page_kwargs,
                **kwargs,
            ).execute(num_retries=_NUM_RETRIES)
            for file in response.get("files", []):
                all_files.append(_finfo_from_response(file, path_prefix))
            page_token = response.get("nextPageToken", None)
            if page_token is None:
                break
        return all_files

    def _find_child_by_name(
        self,
        parent_id: str,
        name: str,
        *,
        trashed: bool = False,
        path_prefix: str | None = None,
    ) -> FileInfo | None:
        """Look up one child of a folder by name with a targeted query.

        The Drive query language matches ``name = '...'`` case-insensitively, so results are
        re-filtered client-side with an exact comparison; pages are consumed
        until the match count is settled, since case-variant siblings make the
        raw result count meaningless.

        Args:
            parent_id: File ID of the folder to search in.
            name: Exact (case-sensitive) child name to find.
            trashed: If True, include trashed files in the search.
            path_prefix: Parent path used to build the returned entry's name.

        Returns:
            The child's FileInfo, or None if no child has that name.

        Raises:
            MultipleFilesError: If several children share the name.
        """
        query_parent = self._parent_query_id(parent_id)
        query = f"name = '{escape_query_str(name)}' and '{query_parent}' in parents"
        if not trashed:
            query += " and trashed = false"

        matches: list[File] = []
        page_token: str | None = None
        while True:
            page_kwargs: _PageListKwargs = {}
            if page_token:
                page_kwargs["pageToken"] = page_token

            response = self.files.list(
                q=query,
                spaces=self.spaces,
                fields=f"nextPageToken, files({INFO_FIELDS})",
                pageSize=_FIND_CHILD_PAGE_SIZE,
                **page_kwargs,
                **self._drive_kw(),
            ).execute(num_retries=_NUM_RETRIES)
            matches.extend(
                file for file in response.get("files", []) if file["name"] == name
            )
            page_token = response.get("nextPageToken")
            if page_token is None or len(matches) > 1:
                break

        if not matches:
            return None
        if len(matches) > 1:
            raise MultipleFilesError(
                _normalize_path(path_prefix, name).lstrip("/") if path_prefix else name
            )
        return _finfo_from_response(matches[0], path_prefix)

    def _trash_file(self, file_id: str, untrash: bool = False) -> File:
        """Trash or untrash a file.

        Args:
            file_id: The ID of the file to trash or untrash.
            untrash: If True, untrash the file.
        """
        response = self.files.update(
            fileId=file_id,
            body={"trashed": not untrash},
            supportsAllDrives=True,
        ).execute(num_retries=_NUM_RETRIES)
        return response

    @override
    def _open(
        self,
        path: PathLike,
        mode: str = "rb",
        block_size: int | None = None,
        autocommit: bool = True,
        cache_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AbstractBufferedFile:
        """Open a file on Google Drive, returning a buffered file object.

        Args:
            path: Path of the file to open.
            mode: File mode; only ``"rb"`` and ``"wb"`` are supported.
            block_size: Buffer size in bytes; defaults to ``DEFAULT_BLOCK_SIZE``.
            autocommit: If True, commit the upload when the file is closed.
            cache_options: Options forwarded to the read-ahead cache.
            **kwargs: Passed to :class:`GoogleDriveFile`.

        Returns:
            A :class:`GoogleDriveFile` open in the requested mode.
        """
        return GoogleDriveFile(
            self,
            path,
            mode=mode,
            block_size=block_size if block_size is not None else DEFAULT_BLOCK_SIZE,
            autocommit=autocommit,
            cache_options=cache_options,
            **kwargs,
        )


class GoogleDriveFile(AbstractBufferedFile):
    def __init__(
        self,
        fs: GoogleDriveFileSystem,
        path: PathLike,
        mode: str = "rb",
        block_size: int = DEFAULT_BLOCK_SIZE,
        autocommit: bool = True,
        **kwargs: Any,
    ) -> None:
        """Open a file on Google Drive for reading or writing.

        Args:
            fs: GoogleDriveFileSystem instance.
            path: File path to open.
            mode: File mode; currently only ``"rb"`` and ``"wb"`` are supported.
            block_size: Buffer size for reading or writing (default 5 MiB).
            autocommit: If True, commit the upload when the file is closed.
            **kwargs: Passed to :class:`AbstractBufferedFile`.

        Raises:
            IsADirectoryError: If ``mode`` is ``"wb"`` and ``path`` is an
                existing directory.
            MultipleFilesError: If ``path`` already resolves to multiple files.
        """
        path = fs._path_str(path)

        existing_id: str | None = None
        if mode == "wb":
            # If the path already exists, remember its id so the upload PATCHes
            # the existing file instead of creating an identically-named
            # duplicate.
            try:
                existing: FileInfo = cast(FileInfo, fs.info(path))
            except MultipleFilesError:
                raise
            except FileNotFoundError:
                pass
            else:
                if existing["type"] == "directory":
                    raise IsADirectoryError(path)
                existing_id = existing["id"]

        super().__init__(fs, path, mode, block_size, autocommit=autocommit, **kwargs)

        if mode == "wb":
            self.location = None
            self.file_id: str | None = existing_id
        else:
            self.file_id = fs._path_to_id(path)
            self._media_object: Any | None = None

    @override
    def _fetch_range(self, start: int | None = None, end: int | None = None) -> bytes:
        """Fetch bytes from Google Drive for the open file.

        Args:
            start: Start byte offset, or None to fetch from the beginning.
            end: End byte offset (exclusive), or None to fetch through the end.

        Returns:
            Requested byte range, or empty bytes if the range is not satisfiable.
        """

        if self._media_object is None:
            self._media_object = self.fs.files.get_media(
                fileId=self.file_id, supportsAllDrives=True
            )
        if start is not None or end is not None:
            start = start or 0
            end = end or 0
            self._media_object.headers["Range"] = "bytes=%i-%i" % (start, end - 1)
        else:
            self._media_object.headers.pop("Range", None)
        try:
            data = self._media_object.execute(num_retries=_NUM_RETRIES)
            return data
        except HttpError as e:
            # TODO : doc says server might send everything if range is outside
            if "not satisfiable" in str(e):
                return b""
            raise

    def _authed_request(
        self,
        uri: str,
        method: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[httplib2.Response, bytes]:
        """Make an authenticated raw request via the owned transport.

        Wraps ``fs.authed_http.request`` with typed return values and retries
        transient failures with exponential backoff. Resumable-upload endpoints
        are driven by hand (they bypass the discovery client's ``num_retries``),
        so this mirrors ``googleapiclient.http._should_retry_response``: retry
        ``5xx``, ``429``, rate-limit ``403``s, and transport errors.
        """
        response: httplib2.Response | None = None
        content: bytes | None = None
        for attempt in range(_NUM_RETRIES + 1):
            if attempt > 0:
                sleep_time = random.random() * 2**attempt
                LOGGER.warning(
                    "Retrying resumable upload %s %s (attempt %d/%d) after %.2fs",
                    method,
                    uri,
                    attempt,
                    _NUM_RETRIES,
                    sleep_time,
                )
                time.sleep(sleep_time)
            try:
                response, content = self.fs.authed_http.request(
                    uri, method=method, body=body, headers=headers
                )
            except _RETRYABLE_TRANSPORT_ERRORS:
                if attempt == _NUM_RETRIES:
                    raise
                continue
            # Stop once the status is terminal, or on the last attempt where a
            # still-retryable status is the best (final) result the caller gets.
            if attempt == _NUM_RETRIES or not _should_retry_status(
                int(response["status"]), content
            ):
                break
        if response is None or content is None:
            raise RuntimeError("Resumable upload retry loop exited without a result")
        return response, content

    @override
    # pyrefly: ignore [bad-override]  # fsspec leaves the base method unannotated
    def _upload_chunk(self, final: bool = False) -> bool:
        """Upload one chunk of a resumable multi-part upload.

        Returns ``False`` (fsspec's "buffer not fully consumed" signal) when the
        server accepted only part of the buffer; ``True`` once it accepted all of
        it. See :meth:`_consume_accepted` for the partial-acceptance handling.

        Args:
            final: If True, finalize and commit the upload.

        Raises:
            IOError: If the upload server returns an unexpected response.
        """
        self.buffer.seek(0)
        data = self.buffer.getvalue()
        head = {}
        length = len(data)
        if final and self.autocommit:
            if length:
                # pyrefly: ignore [unsupported-operation]
                part = "%i-%i" % (self.offset, self.offset + length - 1)
                # pyrefly: ignore [unsupported-operation]
                head["Content-Range"] = "bytes %s/%i" % (part, self.offset + length)
            else:
                # closing when buffer is empty
                head["Content-Range"] = "bytes */%i" % self.offset
                data = None
        else:
            head["Content-Range"] = "bytes %i-%i/*" % (
                self.offset,
                # pyrefly: ignore [unsupported-operation]
                self.offset + length - 1,
            )
        head.update(
            {"Content-Type": "application/octet-stream", "Content-Length": str(length)}
        )
        response, body = self._authed_request(
            # pyrefly: ignore [unsupported-operation]
            self.location + "&supportsAllDrives=true",
            "PUT",
            body=data,
            headers=head,
        )
        status = int(response["status"])
        if status >= 400:
            error_message = body.decode("utf-8", errors="replace")
            raise IOError(f"Chunk upload failed (HTTP {status}): {error_message}")
        if status in [200, 201]:
            # server thinks we are finished - this should happen
            # only when closing
            blob = json.loads(body.decode())
            self.file_id = blob["id"]
            parent = self.fs._parent(self.path)
            info = _finfo_from_response(blob, path_prefix=parent)
            info["size"] = self.tell()
            if parent in self.fs.dircache:
                listing = self.fs.dircache[parent]
                # Update the existing entry in place when overwriting, so the
                # parent listing keeps exactly one entry per path.
                for i, existing in enumerate(listing):
                    if existing["name"] == info["name"]:
                        listing[i] = info
                        break
                else:
                    listing.append(info)
            return True
        if status != 308:
            raise IOError(f"Unexpected resumable status {status}")
        # A 308 on a finalizing PUT means the server did not commit the object.
        # This path sends a concrete total and expects 200/201; treating the
        # 308 as a partial-consumption signal would silently leave the upload
        # unfinalized, since commit()/close() flush only once and ignore the
        # re-buffer. Fail loudly instead.
        if final and self.autocommit:
            raise IOError(
                f"Resumable upload not finalized: server returned 308 "
                f"(range {response.get('range')!r}) on the final chunk"
            )
        return self._consume_accepted(data, response.get("range"))

    def _consume_accepted(self, data: bytes | None, range_header: str | None) -> bool:
        """Reconcile a 308 response with what the server actually stored.

        Google accepts intermediate data only up to a 256 KiB-aligned boundary
        and reports the last stored byte in ``Range: bytes=0-<end>``. Any bytes
        past that boundary were dropped, so re-buffer them for the next chunk.

        Returns True if the whole buffer was accepted (fsspec then advances
        ``offset`` and clears the buffer), or False if a tail was re-buffered
        here (so fsspec must not advance ``offset`` past it).
        """
        if data is None:
            # Empty finalizing PUT; nothing to reconcile.
            return True
        offset = self.offset or 0
        stored_end = _parse_range_end(range_header)
        # A 308 with no/garbled Range means the server persisted nothing yet, so
        # the whole buffer must be re-sent. ``accepted`` is bytes stored from
        # this buffer; the server should never report an end behind our offset.
        accepted = 0 if stored_end is None else stored_end + 1 - offset
        if accepted < 0 or accepted > len(data):
            raise IOError(
                f"Server reported {accepted} accepted bytes outside the {len(data)}-byte chunk at offset {offset}"
            )
        if accepted == len(data):
            return True
        self.buffer = io.BytesIO(data[accepted:])
        self.buffer.seek(0, 2)  # position at end for further writes
        self.offset = offset + accepted
        return False

    @override
    def commit(self) -> None:
        """Finalize the upload when ``autocommit`` is False."""
        self.autocommit = True
        self._upload_chunk(final=True)

    @override
    def _initiate_upload(self) -> None:
        """Start a resumable upload session.

        If the path already exists, the existing file is updated in place via
        PATCH. Otherwise, a new file is created via POST.

        The discovery client's files.create/update
        can only drive resumable uploads from a fully seekable source (MediaFileUpload);
        fsspec streams blocks of unknown total size, so we manage the resumable session
        manually instead. https://developers.google.com/workspace/drive/api/guides/manage-uploads#resumable
        """
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        query = "?uploadType=resumable&supportsAllDrives=true"
        # also allows description, MIME type, version, thumbnail...
        if self.file_id is not None:
            # Update the existing file in place. ``name``/``parents`` are
            # already set on the resource, so an empty body suffices.
            response, _ = self._authed_request(
                f"{UPLOAD_URL}/{self.file_id}{query}",
                "PATCH",
                headers=headers,
                body=json.dumps({}).encode(),
            )
        else:
            parent_id = self.fs._path_to_id(self.fs._parent(self.path))
            response, _ = self._authed_request(
                f"{UPLOAD_URL}{query}",
                "POST",
                headers=headers,
                body=json.dumps(
                    {"name": self.path.rsplit("/", 1)[-1], "parents": [parent_id]}
                ).encode(),
            )
        status = int(response["status"])
        if status >= 400:
            raise IOError(f"Init upload failed with status {status}")
        self.location = response["location"]

    @override
    def discard(self) -> None:
        """Cancel an in-progress resumable upload.

        Issues a ``DELETE`` against the session URI returned by
        :meth:`_initiate_upload`, mirroring the other resumable-upload calls
        rather than reconstructing the endpoint. Google replies ``499`` to a
        successful cancellation, so that status is accepted alongside ``<400``.
        See https://developers.google.com/workspace/drive/api/guides/manage-uploads#cancel-upload
        """
        if self.location is None:
            LOGGER.debug("Abort file creation %s", self.path)
            return
        LOGGER.debug("Cancel file creation %s", self.path)
        response, _ = self._authed_request(
            _with_supports_all_drives(self.location),
            "DELETE",
            headers={"Content-Length": "0"},
        )
        status = int(response["status"])
        if not (status < 400 or status == 499):
            raise IOError(f"Cancel upload failed with status {status}")
        self.location = None
