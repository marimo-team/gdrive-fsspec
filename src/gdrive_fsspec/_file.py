"""Buffered file object for Google Drive reads and hand-rolled resumable uploads."""

from __future__ import annotations

import io
import json
import random
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httplib2
from fsspec.spec import AbstractBufferedFile
from googleapiclient.errors import HttpError

from ._constants import (
    _NUM_RETRIES,
    _RETRYABLE_403_REASONS,
    _RETRYABLE_TRANSPORT_ERRORS,
    DEFAULT_BLOCK_SIZE,
    LOGGER,
    UPLOAD_URL,
    MultipleFilesError,
    PathLike,
    _finfo_from_response,
)
from .types import FileInfo
from .typing_utils import override

if TYPE_CHECKING:
    from .core import GoogleDriveFileSystem


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

        # Always define _media_object so it is not a branch-conditional attribute;
        # it is only ever populated (lazily) on the read path in _fetch_range.
        self._media_object: Any | None = None
        if mode == "wb":
            self.location = None
            self.file_id: str | None = existing_id
        else:
            self.file_id = fs._path_to_id(path)

    @override
    def _fetch_range(self, start: int | None = None, end: int | None = None) -> bytes:
        """Fetch bytes from Google Drive for the open file.

        Args:
            start: Start byte offset, or None to fetch from the beginning.
            end: End byte offset (exclusive), or None to fetch through the end.

        Returns:
            Requested byte range, or empty bytes if the range is not satisfiable.
        """

        if self.file_id is None:
            # _fetch_range only runs on read-mode files, whose id is resolved in
            # __init__; guard the invariant rather than send fileId=None.
            raise RuntimeError("cannot fetch range before the file id is resolved")
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
        # fsspec drives the write path after setting ``offset`` (to an int) and
        # calling ``_initiate_upload`` (which sets ``location``).
        if self.offset is None:
            raise RuntimeError("upload chunk before offset was initialized")
        if self.location is None:
            raise RuntimeError("upload chunk before _initiate_upload set the location")
        if final and self.autocommit:
            if length:
                part = "%i-%i" % (self.offset, self.offset + length - 1)
                head["Content-Range"] = "bytes %s/%i" % (part, self.offset + length)
            else:
                # closing when buffer is empty
                head["Content-Range"] = "bytes */%i" % self.offset
                data = None
        else:
            head["Content-Range"] = "bytes %i-%i/*" % (
                self.offset,
                self.offset + length - 1,
            )
        head.update(
            {"Content-Type": "application/octet-stream", "Content-Length": str(length)}
        )
        response, body = self._authed_request(
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
