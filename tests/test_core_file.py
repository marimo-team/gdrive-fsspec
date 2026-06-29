"""Unit tests for GoogleDriveFile read/write behavior."""

import io
import json
from typing import Any
from unittest import mock

import pytest
from conftest import MockedDriveFS, empty_headers, empty_listing
from googleapiclient.errors import HttpError

from gdrive_fsspec.core import (
    GoogleDriveFile,
    GoogleDriveFileSystem,
    MultipleFilesError,
    _parse_range_end,
    _with_supports_all_drives,
)


def _http_error(message: str) -> HttpError:
    response = mock.Mock(status=416, reason="Range Not Satisfiable")
    return HttpError(response, message.encode())


def _write_file(
    fs: GoogleDriveFileSystem,
    path: str = "parent/file.txt",
    existing_id: str | None = None,
    parent_id: str = "parent-id",
) -> GoogleDriveFile:
    """Build a ``wb`` file, stubbing ``fs.info`` for the path and its parent.

    ``existing_id`` is the id returned for ``path`` itself (``None`` means the
    path does not exist yet, so the upload should create a new file).
    """
    parent = fs._parent(path)

    def _info(p: str, *args: object, **kwargs: object) -> dict[str, Any]:
        if fs._strip_protocol(p) == parent:
            return {"id": parent_id, "type": "directory"}
        if existing_id is not None:
            return {"id": existing_id, "type": "file"}
        raise FileNotFoundError(p)

    fs.info = mock.Mock(side_effect=_info)
    file = GoogleDriveFile(fs, path, mode="wb")
    file.location = "https://example.invalid/upload?upload_id=abc"
    return file


def _read_file(
    fs: GoogleDriveFileSystem, path: str = "path/file.txt"
) -> GoogleDriveFile:
    fs.info = mock.Mock(
        return_value={"id": "file-id", "size": 5, "type": "file", "name": path}
    )
    return GoogleDriveFile(fs, path, mode="rb")


def test_fetch_range_full_read(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    media = mock.Mock()
    media.headers = empty_headers()
    media.execute.return_value = b"hello"
    mocked_fs.files.get_media.return_value = media

    file = _read_file(fs)
    data = file._fetch_range()

    assert data == b"hello"
    mocked_fs.files.get_media.assert_called_once_with(
        fileId="file-id", supportsAllDrives=True
    )
    assert "Range" not in media.headers


def test_fetch_range_with_byte_range(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    media = mock.Mock()
    media.headers = empty_headers()
    media.execute.return_value = b"hel"  # codespell:ignore hel
    mocked_fs.files.get_media.return_value = media

    file = _read_file(fs)
    file._fetch_range(0, 3)

    assert media.headers["Range"] == "bytes=0-2"


def test_fetch_range_not_satisfiable_returns_empty(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    media = mock.Mock()
    media.headers = empty_headers()
    media.execute.side_effect = _http_error("not satisfiable")
    mocked_fs.files.get_media.return_value = media

    file = _read_file(fs)

    assert file._fetch_range(100, 200) == b""


def test_fetch_range_propagates_other_http_errors(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    media = mock.Mock()
    media.headers = empty_headers()
    media.execute.side_effect = HttpError(
        mock.Mock(status=403, reason="Forbidden"), b"denied"
    )
    mocked_fs.files.get_media.return_value = media

    file = _read_file(fs)

    with pytest.raises(HttpError):
        file._fetch_range()


def test_initiate_upload_new_file(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = (
        {"status": "200", "location": "https://upload.example/resume?upload_id=xyz"},
        b"",
    )

    file = _write_file(fs)
    assert file.file_id is None
    try:
        file._initiate_upload()
    finally:
        file.closed = True

    assert file.location == "https://upload.example/resume?upload_id=xyz"
    args, kwargs = mocked_fs.files._http.request.call_args
    assert kwargs["method"] == "POST"
    assert args[0].endswith("/files?uploadType=resumable&supportsAllDrives=true")
    assert json.loads(kwargs["body"].decode()) == {
        "name": "file.txt",
        "parents": ["parent-id"],
    }


def test_initiate_upload_existing_file_patches(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = (
        {"status": "200", "location": "https://upload.example/resume?upload_id=xyz"},
        b"",
    )

    file = _write_file(fs, existing_id="existing-id")
    assert file.file_id == "existing-id"
    try:
        file._initiate_upload()
    finally:
        file.closed = True

    assert file.location == "https://upload.example/resume?upload_id=xyz"
    args, kwargs = mocked_fs.files._http.request.call_args
    assert kwargs["method"] == "PATCH"
    assert args[0].startswith(
        "https://www.googleapis.com/upload/drive/v3/files/existing-id?"
    )
    # No new resource is created, so name/parents are not re-sent.
    assert json.loads(kwargs["body"].decode()) == {}


def test_open_wb_propagates_multiple_files_error(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    # A path that already resolves to duplicates must not be overwritten by
    # creating a third copy; surface the ambiguity instead.
    fs.info = mock.Mock(side_effect=MultipleFilesError("parent/file.txt"))

    with pytest.raises(MultipleFilesError):
        GoogleDriveFile(fs, "parent/file.txt", mode="wb")


def test_open_wb_on_directory_raises(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    # Opening an existing directory for writing must not resolve its id as an
    # overwrite target; bytes cannot be PATCHed onto a folder.
    fs.info = mock.Mock(
        return_value={"id": "dir-id", "type": "directory", "name": "parent/sub"}
    )

    with pytest.raises(IsADirectoryError):
        GoogleDriveFile(fs, "parent/sub", mode="wb")


def test_upload_chunk_partial(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = (
        {"status": "308", "range": "0-999"},
        b"",
    )
    file = _write_file(fs)
    file.write(b"x" * 1000)
    file.offset = 0

    try:
        file._upload_chunk(final=False)
    finally:
        file.closed = True

    headers = mocked_fs.files._http.request.call_args.kwargs["headers"]
    assert headers["Content-Range"] == "bytes 0-999/*"


def test_upload_chunk_partial_accept_keeps_unsent_tail(
    mocked_fs: MockedDriveFS,
) -> None:
    """Google accepts intermediate data only up to a 256 KiB boundary.

    When the server stores fewer bytes than were sent (308 with a ``Range``
    short of the buffer end), the unaccepted tail must stay buffered and
    ``offset`` must track the server, otherwise the upload silently never
    completes. The method returns False so fsspec does not advance past the
    tail itself.
    """
    fs = mocked_fs.fs
    # Sent 1000 bytes from offset 0; server stored only 0-599 (600 bytes).
    mocked_fs.authed_http.request.return_value = (
        {"status": "308", "range": "bytes=0-599"},
        b"",
    )
    file = _write_file(fs)
    file.write(b"y" * 1000)
    file.offset = 0

    try:
        result = file._upload_chunk(final=False)
    finally:
        file.closed = True

    # _upload_chunk parsed the 308, delegated to _consume_accepted, and
    # propagated its result: the tail stays buffered and offset tracks the
    # server. (Branch-level cases are covered directly below.)
    assert result is False
    assert file.offset == 600
    assert file.buffer.tell() == 400
    file.buffer.seek(0)
    assert file.buffer.read() == b"y" * 400


# ---------------------------------------------------------------------------
# Granular, transport-free tests for the 308 reconciliation helper. These call
# _consume_accepted directly so each branch is exercised in isolation, without
# the _upload_chunk / mocked-transport scaffolding above.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("bytes=0-499", 499),  # canonical resumable form
        ("0-499", 499),  # bare form (some responses omit the unit)
        ("bytes=0-0", 0),  # a single stored byte
        (None, None),  # no header
        ("", None),  # empty header
        ("bytes=*", None),  # no dash
        ("bytes=0-notanint", None),  # garbled end
        ("bytes=10-20", None),  # non-zero start: must not be read as end=20
        ("10-20", None),  # same, bare form
        ("garbage-20", None),  # not a range at all
        ("bytes=0-", None),  # missing end
    ],
)
def test_parse_range_end(header: str | None, expected: int | None) -> None:
    assert _parse_range_end(header) == expected


@pytest.mark.parametrize(
    "url,expected_query",
    [
        # No existing param: it is added.
        ("https://up/resume?upload_id=x", "upload_id=x&supportsAllDrives=true"),
        # Already true: stays true, not duplicated.
        (
            "https://up/resume?upload_id=x&supportsAllDrives=true",
            "upload_id=x&supportsAllDrives=true",
        ),
        # Explicitly false: forced to true (the substring check missed this).
        (
            "https://up/resume?supportsAllDrives=false&upload_id=x",
            "supportsAllDrives=true&upload_id=x",
        ),
        # No query string at all.
        ("https://up/resume", "supportsAllDrives=true"),
    ],
)
def test_with_supports_all_drives(url: str, expected_query: str) -> None:
    from urllib.parse import urlsplit

    assert urlsplit(_with_supports_all_drives(url)).query == expected_query


def _consume(
    fs: GoogleDriveFileSystem, data: bytes | None, header: str | None, offset: int
) -> tuple[bool, GoogleDriveFile]:
    """Run _consume_accepted on a fresh file at ``offset`` and return its result."""
    file = _write_file(fs)
    file.offset = offset
    try:
        result = file._consume_accepted(data, header)
    finally:
        file.closed = True
    return result, file


def test_consume_accepted_none_data_returns_true(mocked_fs: MockedDriveFS) -> None:
    """An empty finalizing PUT (data=None) has nothing to reconcile."""
    result, _ = _consume(mocked_fs.fs, None, "bytes=0-9", offset=10)
    assert result is True


def test_consume_accepted_full_accept_returns_true(mocked_fs: MockedDriveFS) -> None:
    result, file = _consume(mocked_fs.fs, b"x" * 100, "bytes=0-99", offset=0)
    assert result is True


def test_consume_accepted_partial_keeps_tail(mocked_fs: MockedDriveFS) -> None:
    result, file = _consume(mocked_fs.fs, b"x" * 100, "bytes=0-59", offset=0)
    assert result is False
    assert file.offset == 60
    file.buffer.seek(0)
    assert file.buffer.read() == b"x" * 40


def test_consume_accepted_partial_at_nonzero_offset(
    mocked_fs: MockedDriveFS,
) -> None:
    """Accounting is relative to the chunk's starting offset, not absolute 0."""
    # Chunk covers bytes 1000..1099; server stored through 1049 → 50 accepted.
    result, file = _consume(mocked_fs.fs, b"y" * 100, "bytes=0-1049", offset=1000)
    assert result is False
    assert file.offset == 1050
    file.buffer.seek(0)
    assert file.buffer.read() == b"y" * 50


def test_consume_accepted_no_range_rebuffers_all(mocked_fs: MockedDriveFS) -> None:
    """A 308 without a Range header means nothing was stored; re-send all."""
    result, file = _consume(mocked_fs.fs, b"z" * 100, None, offset=0)
    assert result is False
    assert file.offset == 0
    file.buffer.seek(0)
    assert file.buffer.read() == b"z" * 100


def test_consume_accepted_end_behind_offset_raises(
    mocked_fs: MockedDriveFS,
) -> None:
    """Server reporting a stored end behind the chunk start → negative accepted."""
    with pytest.raises(IOError, match="accepted"):
        _consume(mocked_fs.fs, b"z" * 100, "bytes=0-499", offset=600)


def test_consume_accepted_over_report_raises(mocked_fs: MockedDriveFS) -> None:
    """Server claiming more stored than we sent in this chunk is a protocol error."""
    # offset 0, chunk is 100 bytes, but server says it stored through byte 999.
    with pytest.raises(IOError, match="accepted"):
        _consume(mocked_fs.fs, b"z" * 100, "bytes=0-999", offset=0)


def test_upload_chunk_final_updates_dircache(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.dircache["parent"] = empty_listing()
    mocked_fs.files._http.request.return_value = (
        {"status": "200"},
        json.dumps(
            {"id": "file-id", "name": "file.txt", "mimeType": "text/plain"}
        ).encode(),
    )
    file = _write_file(fs)
    file.write(b"data")
    file.offset = 0

    try:
        file._upload_chunk(final=True)
    finally:
        file.closed = True

    assert file.file_id == "file-id"
    assert fs.dircache["parent"] == [
        {
            "id": "file-id",
            "name": "parent/file.txt",
            "mimeType": "text/plain",
            "size": 4,
            "type": "file",
        }
    ]


def test_upload_chunk_final_overwrites_dircache_entry(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    # Existing listing already has the file; overwriting must not duplicate it.
    fs.dircache["parent"] = [
        {"name": "parent/file.txt", "id": "old-id", "size": 1, "type": "file"}
    ]
    mocked_fs.files._http.request.return_value = (
        {"status": "200"},
        json.dumps(
            {"id": "new-id", "name": "file.txt", "mimeType": "text/plain"}
        ).encode(),
    )
    file = _write_file(fs, existing_id="old-id")
    file.write(b"hello")
    file.offset = 0

    try:
        file._upload_chunk(final=True)
    finally:
        file.closed = True

    # Whole-listing compare: the single entry is fully replaced, with no stale
    # fields (old id/size) leaking through from the pre-existing entry.
    assert fs.dircache["parent"] == [
        {
            "id": "new-id",
            "name": "parent/file.txt",
            "mimeType": "text/plain",
            "size": 5,
            "type": "file",
        }
    ]
    assert file.file_id == "new-id"


def test_upload_chunk_final_empty_buffer(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = (
        {"status": "200"},
        json.dumps({"id": "file-id", "name": "file.txt"}).encode(),
    )
    file = _write_file(fs)
    file.buffer = io.BytesIO()
    file.offset = 10
    file.autocommit = True

    try:
        file._upload_chunk(final=True)
    finally:
        file.closed = True

    headers = mocked_fs.files._http.request.call_args.kwargs["headers"]
    assert headers["Content-Range"] == "bytes */10"
    assert mocked_fs.files._http.request.call_args.kwargs["body"] is None


def test_upload_chunk_unexpected_status_raises(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = ({"status": "500"}, b"error")
    file = _write_file(fs)
    file.write(b"data")
    file.offset = 0

    with pytest.raises(IOError):
        try:
            file._upload_chunk(final=False)
        finally:
            file.closed = True


def test_upload_chunk_unexpected_response_raises_ioerror(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = ({"status": "204"}, b"")
    file = _write_file(fs)
    file.write(b"data")
    file.offset = 0

    with pytest.raises(IOError):
        try:
            file._upload_chunk(final=False)
        finally:
            file.closed = True


def test_commit_finalizes_upload(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    file = _write_file(fs)
    file.autocommit = False

    with mock.patch.object(file, "_upload_chunk") as upload:
        try:
            file.commit()
        finally:
            file.closed = True

    assert file.autocommit
    upload.assert_called_once_with(final=True)


def test_discard_noop_without_location(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    file = _write_file(fs)
    file.location = None

    try:
        file.discard()
    finally:
        file.closed = True

    mocked_fs.files._http.request.assert_not_called()


def test_discard_cancels_resumable_upload(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = ({"status": "204"}, b"")
    file = _write_file(fs)
    file.location = "https://upload.example/resume?upload_id=abc123"

    try:
        file.discard()
    finally:
        file.closed = True

    args, kwargs = mocked_fs.files._http.request.call_args
    assert args[0] == (
        "https://upload.example/resume?upload_id=abc123&supportsAllDrives=true"
    )
    assert kwargs["method"] == "DELETE"
    # Session URI is cleared so a later close() does not re-cancel.
    assert file.location is None


def test_discard_keeps_existing_supports_all_drives(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = ({"status": "204"}, b"")
    file = _write_file(fs)
    file.location = (
        "https://upload.example/resume?upload_id=abc123&supportsAllDrives=true"
    )

    try:
        file.discard()
    finally:
        file.closed = True

    # The flag is already present; do not append a duplicate.
    args, _ = mocked_fs.files._http.request.call_args
    assert args[0].count("supportsAllDrives") == 1


def test_discard_accepts_http_499(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    # Google replies 499 to a successful resumable-upload cancellation.
    mocked_fs.files._http.request.return_value = ({"status": "499"}, b"")
    file = _write_file(fs)
    file.location = "https://upload.example/resume?upload_id=abc123"

    try:
        file.discard()
    finally:
        file.closed = True

    assert file.location is None


def test_discard_raises_on_failure(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = ({"status": "500"}, b"error")
    file = _write_file(fs)
    file.location = "https://upload.example/resume?upload_id=abc123"

    with pytest.raises(IOError):
        try:
            file.discard()
        finally:
            file.closed = True


def test_transaction_rollback_discards_once(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    file = _write_file(fs)
    tx = fs.transaction
    tx.start()
    tx.files.append(file)

    with mock.patch.object(file, "discard") as discard:
        # A failed transaction rolls back via complete(commit=False).
        tx.complete(commit=False)

    discard.assert_called_once_with()
    file.closed = True
