import json
from typing import Any, Literal, cast
from unittest import mock

import pytest
from conftest import MockedDriveFS
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest

from gdrive_fsspec.core import (
    _NUM_RETRIES,
    DIR_MIME_TYPE,
    GoogleDriveFile,
    GoogleDriveFileSystem,
)


def test_create_anon(anon_fs: GoogleDriveFileSystem) -> None:
    assert anon_fs.service is not None


def test_srv_is_deprecated_alias(anon_fs: GoogleDriveFileSystem) -> None:
    with pytest.warns(DeprecationWarning):
        assert anon_fs.srv is anon_fs.service


def test_auth_kwargs() -> None:
    fs = GoogleDriveFileSystem(
        token="anon",
        auth_kwargs={"user_email": "test@example.com"},
        skip_instance_cache=True,
    )
    assert fs.service is not None
    assert fs.auth_kwargs == {"user_email": "test@example.com"}


def test_connect_invalid_method() -> None:
    with pytest.raises(ValueError):
        GoogleDriveFileSystem(token=cast(Any, "bogus"), skip_instance_cache=True)


def test_invalid_access_raises() -> None:
    with pytest.raises(KeyError):
        GoogleDriveFileSystem(
            token="anon", access=cast(Any, "nope"), skip_instance_cache=True
        )


@pytest.mark.parametrize(
    "access, expected_scopes",
    [
        ("full_control", ["https://www.googleapis.com/auth/drive"]),
        ("read_only", ["https://www.googleapis.com/auth/drive.readonly"]),
    ],
)
def test_access_scopes_mapping(
    access: Literal["full_control", "read_only"],
    expected_scopes: list[str],
) -> None:
    fs = GoogleDriveFileSystem(token="anon", access=access, skip_instance_cache=True)
    assert fs.scopes == expected_scopes


def test_upload_chunk_without_parent_dircache() -> None:
    fs = GoogleDriveFileSystem(
        token="anon", skip_instance_cache=True, use_listings_cache=False
    )
    fs.files = mock.Mock()
    fs.authed_http = mock.Mock()
    # New file: the path does not exist yet.
    fs.info = mock.Mock(side_effect=FileNotFoundError("parent/file.txt"))
    fs.authed_http.request.return_value = (
        {"status": "200"},
        json.dumps({"id": "file-id", "name": "file.txt", "size": "4"}).encode(),
    )
    file = GoogleDriveFile(fs, "parent/file.txt", mode="wb")
    file.location = "https://example.invalid/upload?upload_id=1"
    file.write(b"data")
    file.offset = 0

    try:
        # A fully-accepted final chunk (HTTP 200) reports completion as True.
        assert file._upload_chunk(final=True) is True
    finally:
        file.closed = True

    assert "parent" not in fs.dircache
    assert file.file_id == "file-id"


def test_drive_kw_without_drive(anon_fs: GoogleDriveFileSystem) -> None:
    assert anon_fs._drive_kw() == {}


def test_drive_kw_with_drive(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drive = "drive-123"
    kw = anon_fs._drive_kw()
    assert kw["driveId"] == "drive-123"
    assert kw["supportsAllDrives"] is True


def test_root_info(anon_fs: GoogleDriveFileSystem) -> None:
    info = anon_fs.info("")
    assert info == {
        "name": "",
        "mimeType": DIR_MIME_TYPE,
        "type": "directory",
        "size": 0,
        "id": anon_fs.root_file_id,
    }


def _http_error(status: int) -> HttpError:
    resp = mock.Mock(status=status, reason="Error")
    return HttpError(resp, b'{"error": {"message": "x"}}')


def test_validate_root_file_id_accepts_folder(mocked_fs: MockedDriveFS) -> None:
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "folder-id",
        "trashed": False,
        "mimeType": DIR_MIME_TYPE,
    }

    mocked_fs.fs._validate_root_file_id("folder-id")

    mocked_fs.files.get.assert_called_once_with(
        fileId="folder-id",
        fields="id,trashed,mimeType,driveId",
        supportsAllDrives=True,
    )


def test_validate_root_file_id_accepts_folder_in_scoped_drive(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.fs.drive = "drive-id"
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "folder-id",
        "trashed": False,
        "mimeType": DIR_MIME_TYPE,
        "driveId": "drive-id",
    }

    mocked_fs.fs._validate_root_file_id("folder-id")


def test_validate_root_file_id_rejects_folder_in_other_drive(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.fs.drive = "drive-id"
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "folder-id",
        "trashed": False,
        "mimeType": DIR_MIME_TYPE,
        "driveId": "other-drive",
    }

    with pytest.raises(ValueError, match="not in drive"):
        mocked_fs.fs._validate_root_file_id("folder-id")


def test_validate_root_file_id_rejects_my_drive_folder_when_scoped(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.fs.drive = "drive-id"
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "folder-id",
        "trashed": False,
        "mimeType": DIR_MIME_TYPE,
    }

    with pytest.raises(ValueError, match="not in drive"):
        mocked_fs.fs._validate_root_file_id("folder-id")


def test_validate_root_file_id_rejects_non_folder(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "file-id",
        "trashed": False,
        "mimeType": "text/plain",
    }

    with pytest.raises(NotADirectoryError, match="not a folder"):
        mocked_fs.fs._validate_root_file_id("file-id")


def test_validate_root_file_id_rejects_trashed_folder(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "folder-id",
        "trashed": True,
        "mimeType": DIR_MIME_TYPE,
    }

    with pytest.raises(FileNotFoundError, match="trashed"):
        mocked_fs.fs._validate_root_file_id("folder-id")


def test_validate_root_file_id_accepts_drive_id_and_sets_drive(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.fs.drive = None
    mocked_fs.files.get.return_value.execute.side_effect = _http_error(404)
    mocked_fs.service.drives.return_value.get.return_value.execute.return_value = {
        "id": "drive-id",
    }

    mocked_fs.fs._validate_root_file_id("drive-id")

    assert mocked_fs.fs.drive == "drive-id"
    mocked_fs.service.drives.return_value.get.assert_called_once_with(
        driveId="drive-id"
    )


def test_validate_root_file_id_accepts_matching_drive_id(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.fs.drive = "drive-id"
    mocked_fs.files.get.return_value.execute.side_effect = _http_error(404)
    mocked_fs.service.drives.return_value.get.return_value.execute.return_value = {
        "id": "drive-id",
    }

    mocked_fs.fs._validate_root_file_id("drive-id")

    assert mocked_fs.fs.drive == "drive-id"


def test_validate_root_file_id_conflicting_drive_id_raises(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.fs.drive = "existing-drive"
    mocked_fs.files.get.return_value.execute.side_effect = _http_error(404)
    mocked_fs.service.drives.return_value.get.return_value.execute.return_value = {
        "id": "drive-id",
    }

    with pytest.raises(ValueError, match="conflicts with drive"):
        mocked_fs.fs._validate_root_file_id("drive-id")


def test_validate_root_file_id_missing(mocked_fs: MockedDriveFS) -> None:
    mocked_fs.files.get.return_value.execute.side_effect = _http_error(404)
    mocked_fs.service.drives.return_value.get.return_value.execute.side_effect = (
        _http_error(404)
    )

    with pytest.raises(FileNotFoundError, match="not found"):
        mocked_fs.fs._validate_root_file_id("missing-id")


def test_validate_root_file_id_propagates_drive_permission_error(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.files.get.return_value.execute.side_effect = _http_error(404)
    mocked_fs.service.drives.return_value.get.return_value.execute.side_effect = (
        _http_error(403)
    )

    with pytest.raises(HttpError) as exc_info:
        mocked_fs.fs._validate_root_file_id("drive-id")
    assert exc_info.value.status_code == 403


def test_validate_root_file_id_propagates_non_404_from_files_get(
    mocked_fs: MockedDriveFS,
) -> None:
    mocked_fs.files.get.return_value.execute.side_effect = _http_error(403)

    with pytest.raises(HttpError) as exc_info:
        mocked_fs.fs._validate_root_file_id("folder-id")
    assert exc_info.value.status_code == 403


def test_ls_empty_root_returns_empty(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs._list_directory_by_id = mock.Mock(return_value=[])

    assert anon_fs.ls("") == []
    assert anon_fs.dircache[""] == []


def test_ls_missing_child_in_populated_parent_raises(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs._list_directory_by_id = mock.Mock(
        return_value=[
            {
                "name": "other",
                "id": "other-id",
                "type": "file",
                "mimeType": "text/plain",
            }
        ]
    )

    with pytest.raises(FileNotFoundError):
        anon_fs.ls("missing")


def test_ls_missing_path_on_empty_root_raises(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs._list_directory_by_id = mock.Mock(return_value=[])

    with pytest.raises(FileNotFoundError):
        anon_fs.ls("missing")


def test_invalidate_cache_path(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.dircache["parent"] = [{"name": "parent/file"}]
    anon_fs.dircache["other"] = [{"name": "other/file"}]

    anon_fs.invalidate_cache("parent")

    assert "parent" not in anon_fs.dircache
    assert anon_fs.dircache["other"] == [{"name": "other/file"}]


def test_invalidate_cache_all(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.dircache["parent"] = [{"name": "parent/file"}]
    anon_fs.dircache["other"] = [{"name": "other/file"}]

    anon_fs.invalidate_cache()

    assert anon_fs.dircache == {}


def test_resolve_drive_id_by_name(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drives = [{"id": "1", "name": "foo"}, {"id": "2", "name": "bar"}]
    assert anon_fs._resolve_drive_id("foo") == "1"


def test_resolve_drive_id_by_id(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drives = [{"id": "1", "name": "foo"}, {"id": "2", "name": "bar"}]
    assert anon_fs._resolve_drive_id("2") == "2"


def test_resolve_drive_id_missing(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drives = [{"id": "1", "name": "foo"}]
    with pytest.raises(ValueError, match="not found by id or name"):
        anon_fs._resolve_drive_id("missing")


def test_resolve_drive_id_duplicate_name(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drives = [{"id": "1", "name": "dup"}, {"id": "2", "name": "dup"}]
    with pytest.raises(ValueError, match="multiple shared drives"):
        anon_fs._resolve_drive_id("dup")


@pytest.mark.parametrize(
    "creds",
    [
        {"type": "service_account"},
        '{"type": "service_account"}',
    ],
)
def test_service_account_creds_parsing(creds: dict[str, Any] | str) -> None:
    target = "gdrive_fsspec.core.service_account.Credentials.from_service_account_info"
    with mock.patch(target) as from_info:
        GoogleDriveFileSystem(
            token="service_account",
            creds=creds,
            skip_instance_cache=True,
        )
    from_info.assert_called_once()
    assert from_info.call_args.kwargs["info"] == {"type": "service_account"}


@pytest.mark.parametrize("creds", ["", "   ", "\t\n"])
def test_service_account_empty_creds_raises(creds: str) -> None:
    with pytest.raises(ValueError, match="Empty credentials"):
        GoogleDriveFileSystem(
            token="service_account",
            creds=creds,
            skip_instance_cache=True,
        )


def test_files_get_execute_passes_num_retries(mocked_fs: MockedDriveFS) -> None:
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "folder-id",
        "trashed": False,
        "mimeType": DIR_MIME_TYPE,
    }

    mocked_fs.fs._validate_root_file_id("folder-id")

    mocked_fs.files.get.return_value.execute.assert_called_once_with(
        num_retries=_NUM_RETRIES
    )


def test_about_get_execute_passes_num_retries(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    expected = {DIR_MIME_TYPE: ["text/plain"]}
    mocked_fs.service.about().get().execute.return_value = {"exportFormats": expected}

    assert fs.export_formats == expected

    mocked_fs.service.about().get().execute.assert_called_once_with(
        num_retries=_NUM_RETRIES
    )


def _drive_http_request(http: mock.Mock) -> HttpRequest:
    return HttpRequest(
        http,
        postproc=lambda _resp, content: content,
        uri="https://www.googleapis.com/drive/v3/files/test",
        method="GET",
    )


def test_execute_retries_transient_503_then_succeeds() -> None:
    http = mock.Mock()
    error_resp = mock.Mock(status=503)
    ok_resp = mock.Mock(status=200)
    http.request.side_effect = [
        (error_resp, b'{"error": {"message": "unavailable"}}'),
        (error_resp, b'{"error": {"message": "unavailable"}}'),
        (ok_resp, b'{"id": "ok"}'),
    ]
    request = _drive_http_request(http)

    with (
        mock.patch.object(request, "_sleep"),
        mock.patch.object(request, "_rand", return_value=0),
    ):
        result = request.execute(num_retries=_NUM_RETRIES)

    assert result == b'{"id": "ok"}'
    assert http.request.call_count == 3


def test_execute_retries_rate_limit_403() -> None:
    http = mock.Mock()
    rate_limit_body = json.dumps(
        {
            "error": {
                "errors": [
                    {
                        "domain": "usageLimits",
                        "reason": "userRateLimitExceeded",
                        "message": "User Rate Limit Exceeded",
                    }
                ],
                "code": 403,
                "message": "User Rate Limit Exceeded",
            }
        }
    ).encode()
    http.request.side_effect = [
        (mock.Mock(status=403), rate_limit_body),
        (mock.Mock(status=200), b'{"id": "ok"}'),
    ]
    request = _drive_http_request(http)

    with (
        mock.patch.object(request, "_sleep"),
        mock.patch.object(request, "_rand", return_value=0),
    ):
        result = request.execute(num_retries=_NUM_RETRIES)

    assert result == b'{"id": "ok"}'
    assert http.request.call_count == 2


def test_execute_does_not_retry_permission_403() -> None:
    http = mock.Mock()
    permission_body = json.dumps(
        {
            "error": {
                "errors": [
                    {
                        "domain": "global",
                        "reason": "insufficientFilePermissions",
                        "message": "Insufficient permissions",
                    }
                ],
                "code": 403,
                "message": "Insufficient permissions",
            }
        }
    ).encode()
    http.request.return_value = (mock.Mock(status=403), permission_body)
    request = _drive_http_request(http)

    with pytest.raises(HttpError) as exc_info:
        request.execute(num_retries=_NUM_RETRIES)

    assert exc_info.value.status_code == 403
    http.request.assert_called_once()
