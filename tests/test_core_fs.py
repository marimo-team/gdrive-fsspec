"""Unit tests for GoogleDriveFileSystem directory and path operations."""

import pathlib
from typing import Any
from unittest import mock

import pytest
from conftest import MockedDriveFS, empty_files_list_response, empty_listing

from gdrive_fsspec.core import (
    DIR_MIME_TYPE,
    INFO_FIELDS,
    ROOT_ID,
    GoogleDriveFile,
    GoogleDriveFileSystem,
    MultipleFilesError,
)
from gdrive_fsspec.utils import merge_fields


def test_mkdir_creates_folder_and_updates_dircache(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.exists = mock.Mock(return_value=False)
    fs._path_to_id = mock.Mock(return_value="parent-id")
    mocked_fs.files.create.return_value.execute.return_value = {
        "id": "new-id",
        "name": "newfolder",
        "mimeType": DIR_MIME_TYPE,
    }
    fs.dircache["parent"] = empty_listing()

    result = fs.mkdir("parent/newfolder", create_parents=False)

    mocked_fs.files.create.assert_called_once_with(
        body={
            "name": "newfolder",
            "mimeType": DIR_MIME_TYPE,
            "parents": ["parent-id"],
        },
        supportsAllDrives=True,
    )
    assert result == {
        "id": "new-id",
        "name": "newfolder",
        "mimeType": DIR_MIME_TYPE,
    }
    assert fs.dircache["parent/newfolder"] == []
    assert fs.dircache["parent"] == [
        {
            "id": "new-id",
            "name": "parent/newfolder",
            "mimeType": DIR_MIME_TYPE,
            "size": 0,
            "type": "directory",
        }
    ]


def test_mkdir_raises_when_path_exists(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.exists = mock.Mock(return_value=True)
    fs._path_to_id = mock.Mock(return_value="parent-id")

    with pytest.raises(FileExistsError):
        fs.mkdir("parent/existing", create_parents=False)

    # The existence check short-circuits before the parent id is resolved.
    fs._path_to_id.assert_not_called()
    mocked_fs.files.create.assert_not_called()


def test_mkdir_create_parents_calls_makedirs(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.makedirs = mock.Mock()
    fs.exists = mock.Mock(return_value=False)
    fs._path_to_id = mock.Mock(return_value="parent-id")
    mocked_fs.files.create.return_value.execute.return_value = {
        "id": "new-id",
        "name": "child",
        "mimeType": DIR_MIME_TYPE,
    }

    fs.mkdir("parent/child", create_parents=True)

    fs.makedirs.assert_called_once_with("parent", exist_ok=True)


def test_mkdir_skips_parent_creation_at_root(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.makedirs = mock.Mock()
    fs.exists = mock.Mock(return_value=False)
    fs.root_file_id = "root-id"
    mocked_fs.files.create.return_value.execute.return_value = {
        "id": "new-id",
        "name": "top",
        "mimeType": DIR_MIME_TYPE,
    }

    fs.mkdir("top", create_parents=True)

    fs.makedirs.assert_not_called()


def test_makedirs_creates_missing_segments(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.exists = mock.Mock(side_effect=[False, False, True])
    fs.mkdir = mock.Mock()

    fs.makedirs("a/b/c", exist_ok=True)

    assert fs.mkdir.call_count == 2
    fs.mkdir.assert_any_call("a", create_parents=False)
    fs.mkdir.assert_any_call("a/b", create_parents=False)


def test_makedirs_exist_ok_false_raises(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.exists = mock.Mock(side_effect=[True, True])

    with pytest.raises(FileExistsError):
        fs.makedirs("a/b", exist_ok=False)


def _deletable_info(**extra: Any) -> dict[str, Any]:
    """A file-info dict for a file the caller is allowed to delete."""
    return {"id": "file-id", "capabilities": {"canDelete": True}, **extra}


def test_rm_file_deletes_and_updates_dircache(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value=_deletable_info())
    fs.dircache["parent"] = [{"name": "parent/file", "id": "file-id"}]
    fs.dircache["parent/file"] = empty_listing()

    fs.rm_file("parent/file")

    mocked_fs.files.delete.assert_called_once_with(
        fileId="file-id", supportsAllDrives=True
    )
    assert fs.dircache["parent"] == []
    assert "parent/file" not in fs.dircache


def test_rm_requests_capabilities_and_drive_id(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value=_deletable_info())

    fs._rm("parent/file")

    # The capability check must be resolved in the same info() call, not a
    # separate follow-up request.
    fs.info.assert_called_once_with(
        "parent/file", fields="driveId,capabilities/canDelete"
    )


def test_rm_no_delete_permission_on_shared_drive_raises_with_role_hint(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(
        return_value={
            "id": "file-id",
            "driveId": "drive-1",
            "capabilities": {"canDelete": False},
        }
    )

    with pytest.raises(PermissionError, match="Manager access"):
        fs._rm("parent/file")

    # Nothing is deleted when the capability check fails.
    mocked_fs.files.delete.assert_not_called()


def test_rm_no_delete_permission_on_my_drive_omits_shared_drive_advice(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    # No driveId → a My Drive file; the shared-drive role hint would mislead.
    fs.info = mock.Mock(
        return_value={"id": "file-id", "capabilities": {"canDelete": False}}
    )

    with pytest.raises(PermissionError) as excinfo:
        fs._rm("parent/file")

    assert "shared drives" not in str(excinfo.value)
    mocked_fs.files.delete.assert_not_called()


def test_rm_missing_capabilities_treated_as_not_deletable(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    # Absent capabilities block deletion rather than risk a masked-404 failure.
    fs.info = mock.Mock(return_value={"id": "file-id"})

    with pytest.raises(PermissionError):
        fs._rm("parent/file")

    mocked_fs.files.delete.assert_not_called()


def test_rm_missing_file_raises_file_not_found(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    # info() surfaces a genuinely missing file before the capability check.
    fs.info = mock.Mock(side_effect=FileNotFoundError("parent/file"))

    with pytest.raises(FileNotFoundError):
        fs._rm("parent/file")

    mocked_fs.files.delete.assert_not_called()


def test_rm_non_empty_folder_without_recursive_raises(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=True)
    fs.ls = mock.Mock(return_value=["child"])

    with pytest.raises(ValueError, match="non-empty"):
        fs.rm("folder", recursive=False)

    # The guard must fire before any Drive delete is issued.
    mocked_fs.files.delete.assert_not_called()


def test_rm_deletes_file(mocked_fs: MockedDriveFS) -> None:
    # rm -> rm_file (fsspec base) -> _rm (our override) -> files.delete.
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=False)
    fs.info = mock.Mock(return_value=_deletable_info())

    fs.rm("file.txt")

    mocked_fs.files.delete.assert_called_once_with(
        fileId="file-id", supportsAllDrives=True
    )


def test_rmdir_requires_directory(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=False)

    with pytest.raises(ValueError, match="not a directory"):
        fs.rmdir("file.txt")


def test_rmdir_deletes_empty_directory(mocked_fs: MockedDriveFS) -> None:
    # rmdir -> rm -> rm_file (fsspec base) -> _rm (our override) -> files.delete.
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=True)
    fs.ls = mock.Mock(return_value=[])
    fs.info = mock.Mock(return_value=_deletable_info(id="dir-id"))

    fs.rmdir("empty")

    mocked_fs.files.delete.assert_called_once_with(
        fileId="dir-id", supportsAllDrives=True
    )


def test_ls_from_dircache_returns_sorted_names(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs.dircache["parent"] = [
        {"name": "parent/b", "type": "file"},
        {"name": "parent/a", "type": "file"},
    ]

    assert anon_fs.ls("parent") == ["parent/a", "parent/b"]


def test_ls_from_dircache_detail(anon_fs: GoogleDriveFileSystem) -> None:
    listing = [{"name": "parent/a", "type": "file"}]
    anon_fs.dircache["parent"] = listing

    assert anon_fs.ls("parent", detail=True) == listing


def test_ls_nested_directory(anon_fs: GoogleDriveFileSystem) -> None:
    def list_by_id(
        file_id: str,
        trashed: bool = False,
        path_prefix: str | None = None,
        fields: str | None = None,
    ) -> list[dict[str, Any]]:
        if file_id == "root":
            return [
                {
                    "name": "parent",
                    "id": "parent-id",
                    "type": "directory",
                    "mimeType": DIR_MIME_TYPE,
                }
            ]
        if file_id == "parent-id":
            return [
                {
                    "name": "parent/child.txt",
                    "id": "child-id",
                    "type": "file",
                    "mimeType": "text/plain",
                }
            ]
        return []

    anon_fs.root_file_id = "root"
    anon_fs._list_directory_by_id = mock.Mock(side_effect=list_by_id)

    assert anon_fs.ls("parent") == ["parent/child.txt"]
    assert anon_fs.dircache["parent"] == [
        {
            "name": "parent/child.txt",
            "id": "child-id",
            "type": "file",
            "mimeType": "text/plain",
        }
    ]


def test_ls_nested_file_resolves_via_parent_listing(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs.dircache["parent"] = [
        {
            "name": "parent/child.txt",
            "id": "child-id",
            "type": "file",
            "mimeType": "text/plain",
        }
    ]

    assert anon_fs.ls("parent/child.txt") == ["parent/child.txt"]


def test_info_honors_fields_after_listing_cache_warmed_without_them(
    mocked_fs: MockedDriveFS,
) -> None:
    """Regression: a warm dircache entry must not satisfy ``info(..., fields=...)``.

    Resolves the file id from the listing cache, then fetches authoritative
    metadata via ``files.get``. Before the ``info()`` rewrite, a listing cached
    without ``capabilities`` caused ``info(..., fields="capabilities/canDelete")``
    to return stale metadata and ``_rm`` to raise a false ``PermissionError``.
    """
    fs = mocked_fs.fs
    fs.dircache[""] = [
        {
            "name": "file.txt",
            "id": "file-id",
            "type": "file",
            "size": 3,
            "mimeType": "text/plain",
        }
    ]
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "file-id",
        "name": "file.txt",
        "mimeType": "text/plain",
        "size": "3",
        "capabilities": {"canDelete": True},
    }

    info = fs.info("file.txt", fields="capabilities/canDelete")

    mocked_fs.files.get.assert_called_once_with(
        fileId="file-id",
        fields=merge_fields(INFO_FIELDS, "capabilities/canDelete"),
        supportsAllDrives=True,
    )
    assert info == {
        "name": "file.txt",
        "id": "file-id",
        "type": "file",
        "size": 3,
        "mimeType": "text/plain",
        "capabilities": {"canDelete": True},
    }


def test_info_without_fields_reuses_listing_entry_without_get(
    mocked_fs: MockedDriveFS,
) -> None:
    # With no extra fields, the parent listing already carries the full entry,
    # so info() must not make a per-file files.get request.
    fs = mocked_fs.fs
    fs.dircache[""] = [
        {
            "name": "file.txt",
            "id": "file-id",
            "type": "file",
            "size": 3,
            "mimeType": "text/plain",
        }
    ]

    info = fs.info("file.txt")

    mocked_fs.files.get.assert_not_called()
    assert info["id"] == "file-id"
    assert info["type"] == "file"


def test_ls_non_canonical_bypasses_cache_read_and_write(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    fetched = [{"name": "a", "id": "1", "type": "file", "driveId": "drive-1"}]
    anon_fs.dircache[""] = [{"name": "a", "id": "1", "type": "file"}]
    anon_fs._list_directory_by_id = mock.Mock(return_value=fetched)

    result = anon_fs.ls("", detail=True, fields="driveId")

    anon_fs._list_directory_by_id.assert_called_once()
    assert result[0]["driveId"] == "drive-1"
    assert "driveId" not in anon_fs.dircache[""][0]

    anon_fs.dircache.clear()
    anon_fs._list_directory_by_id.reset_mock()

    anon_fs.ls("", detail=True, fields="driveId")

    anon_fs._list_directory_by_id.assert_called_once()
    assert "" not in anon_fs.dircache


def test_ls_fields_without_detail_raises(anon_fs: GoogleDriveFileSystem) -> None:
    # Requesting fields with detail=False would silently discard them, since the
    # names-only result carries no metadata. Reject the misuse instead.
    anon_fs._list_directory_by_id = mock.Mock()

    with pytest.raises(ValueError, match="detail=True"):
        anon_fs.ls("", fields="driveId")

    anon_fs._list_directory_by_id.assert_not_called()


def test_ls_skips_cache_when_trashed_requested(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    canonical = [{"name": "a", "id": "1", "type": "file"}]
    anon_fs.dircache[""] = [dict(entry) for entry in canonical]
    anon_fs._list_directory_by_id = mock.Mock(
        return_value=[{"name": "trashed-a", "id": "2", "type": "file", "trashed": True}]
    )

    result = anon_fs.ls("", detail=True, trashed=True)

    anon_fs._list_directory_by_id.assert_called_once()
    assert result[0]["name"] == "trashed-a"
    assert anon_fs.dircache[""] == canonical


def test_path_to_id_resolves_from_parent_dircache(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs.root_file_id = "root-id"
    anon_fs.dircache["parent"] = [
        {
            "name": "parent/child.txt",
            "id": "child-id",
            "type": "file",
            "mimeType": "text/plain",
        }
    ]

    assert anon_fs._path_to_id("") == "root-id"
    assert anon_fs._path_to_id("parent/child.txt") == "child-id"


def test_path_to_id_missing_raises_file_not_found(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs.dircache["parent"] = [
        {"name": "parent/other", "id": "other-id", "type": "file"}
    ]
    with pytest.raises(FileNotFoundError):
        anon_fs._path_to_id("parent/child")


def test_path_to_id_duplicate_raises_multiple_files_error(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    # Ambiguous resolution must fail, not silently pick one, so callers like the
    # write path can refuse to overwrite an arbitrary duplicate.
    anon_fs.dircache["parent"] = [
        {"name": "parent/dup", "id": "1", "type": "file"},
        {"name": "parent/dup", "id": "2", "type": "file"},
    ]
    with pytest.raises(MultipleFilesError):
        anon_fs._path_to_id("parent/dup")


def test_resolve_entry_rejects_root(anon_fs: GoogleDriveFileSystem) -> None:
    # Root has no parent to list; callers must handle it before resolving.
    with pytest.raises(ValueError, match="root"):
        anon_fs._resolve_entry("")


def test_google_drive_file_normalizes_pathlike(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs._path_to_id = mock.Mock(return_value="file-id")
    fs.info = mock.Mock(
        return_value={"id": "file-id", "size": 0, "type": "file", "name": "file.txt"}
    )

    opened = GoogleDriveFile(fs, pathlib.PurePosixPath("file.txt"), mode="rb")

    assert opened.path == "file.txt"
    fs._path_to_id.assert_called_once_with("file.txt")


def test_ls_file_at_root_returns_parent_listing(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs._list_directory_by_id = mock.Mock(
        return_value=[
            {
                "name": "file.txt",
                "id": "file-id",
                "type": "file",
                "mimeType": "text/plain",
            }
        ]
    )

    assert anon_fs.ls("file.txt") == ["file.txt"]


def test_ls_raises_multiple_files_error(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs._list_directory_by_id = mock.Mock(
        return_value=[
            {"name": "dup", "id": "1", "type": "file"},
            {"name": "dup", "id": "2", "type": "file"},
        ]
    )

    with pytest.raises(MultipleFilesError):
        anon_fs.ls("dup")


def test_list_directory_by_id_paginates(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.side_effect = [
        {"files": [{"name": "a", "id": "1"}], "nextPageToken": "page-2"},
        {"files": [{"name": "b", "id": "2"}]},
    ]
    mocked_fs.files.list.return_value = list_request

    result = fs._list_directory_by_id("folder-id")

    assert len(result) == 2
    assert mocked_fs.files.list.call_count == 2
    assert mocked_fs.files.list.call_args_list[1].kwargs["pageToken"] == "page-2"


def test_list_directory_by_id_shared_drive_root_query(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    fs.drive = "drive-123"
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_directory_by_id(ROOT_ID)

    query = mocked_fs.files.list.call_args.kwargs["q"]
    assert "'drive-123' in parents" in query
    assert "trashed = false" in query


def test_list_directory_by_id_includes_trashed_when_requested(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_directory_by_id("folder-id", trashed=True)

    query = mocked_fs.files.list.call_args.kwargs["q"]
    assert "trashed = false" not in query


def test_list_directory_by_id_passes_drive_kwargs(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.drive = "drive-123"
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_directory_by_id("folder-id")

    kwargs = mocked_fs.files.list.call_args.kwargs
    assert kwargs["driveId"] == "drive-123"
    assert kwargs["supportsAllDrives"] is True


def test_drives_paginates(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.side_effect = [
        {"drives": [{"id": "1", "name": "a"}], "nextPageToken": "t"},
        {"drives": [{"id": "2", "name": "b"}]},
    ]
    mocked_fs.service.drives.return_value.list.return_value = list_request

    assert fs.drives == [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]


DOC_MIME = "application/vnd.google-apps.document"


def _set_export_formats(fs: GoogleDriveFileSystem, formats: dict[str, Any]) -> None:
    # export_formats is a cached_property; seed its cache so service.about() is
    # not hit during the test.
    fs.__dict__["export_formats"] = formats


def test_export_streams_via_export_media(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value={"id": "doc-id", "mimeType": DOC_MIME})
    _set_export_formats(fs, {DOC_MIME: ["text/plain", "application/pdf"]})
    request = mock.Mock()
    mocked_fs.files.export_media.return_value = request

    def fake_downloader(buffer: Any, req: Any) -> mock.Mock:
        assert req is request
        downloader = mock.Mock()
        # Two chunks, then done; second next_chunk reports completion.
        chunks = iter([b"expo", b"rted"])

        def next_chunk() -> tuple[mock.Mock, bool]:
            buffer.write(next(chunks))
            done = buffer.getvalue() == b"exported"
            return mock.Mock(), done

        downloader.next_chunk.side_effect = next_chunk
        return downloader

    with mock.patch("gdrive_fsspec.core.MediaIoBaseDownload", fake_downloader):
        result = fs.export("doc.gdoc", "text/plain")

    mocked_fs.files.export_media.assert_called_once_with(
        fileId="doc-id", mimeType="text/plain"
    )
    assert result == b"exported"


def test_export_rejects_unsupported_mime_type(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value={"id": "doc-id", "mimeType": DOC_MIME})
    _set_export_formats(fs, {DOC_MIME: ["text/plain", "application/pdf"]})

    with pytest.raises(ValueError, match="text/plain, application/pdf"):
        fs.export("doc.gdoc", "text/doc")

    mocked_fs.files.export_media.assert_not_called()


def test_export_formats_queries_about_resource(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    expected = {DOC_MIME: ["text/plain"]}
    mocked_fs.service.about().get().execute.return_value = {"exportFormats": expected}

    assert fs.export_formats == expected
    # cached_property: second access does not re-query
    mocked_fs.service.about.reset_mock()
    assert fs.export_formats == expected
    mocked_fs.service.about.assert_not_called()
