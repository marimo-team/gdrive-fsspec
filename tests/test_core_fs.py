"""Unit tests for GoogleDriveFileSystem directory and path operations."""

import pathlib
from typing import Any
from unittest import mock

import pytest
from conftest import MockedDriveFS, empty_files_list_response, empty_listing
from googleapiclient.errors import HttpError

from gdrive_fsspec._constants import (
    _CHANGES_FIELDS,
    _CHANGES_PAGE_SIZE,
    _NUM_RETRIES,
    DIR_MIME_TYPE,
    INFO_FIELDS,
    ROOT_ID,
    MultipleFilesError,
)
from gdrive_fsspec._file import GoogleDriveFile
from gdrive_fsspec.core import GoogleDriveFileSystem
from gdrive_fsspec.types import FileInfo
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
    """A file-info dict for a file the caller may permanently delete."""
    return {"id": "file-id", "capabilities": {"canDelete": True}, **extra}


def _trashable_info(**extra: Any) -> dict[str, Any]:
    """A file-info dict for a file the caller may move to trash."""
    return {"id": "file-id", "capabilities": {"canTrash": True}, **extra}


def test_rm_file_trashes_and_updates_dircache(mocked_fs: MockedDriveFS) -> None:
    # rm defaults to trash (files.update trashed=True), matching the Drive UI.
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value=_trashable_info())
    fs.dircache["parent"] = [
        {"name": "parent/file", "id": "file-id", "size": 0, "type": "file"}
    ]
    fs.dircache["parent/file"] = empty_listing()

    fs.rm_file("parent/file")

    mocked_fs.files.update.assert_called_once_with(
        fileId="file-id", body={"trashed": True}, supportsAllDrives=True
    )
    mocked_fs.files.delete.assert_not_called()
    assert fs.dircache["parent"] == []
    assert "parent/file" not in fs.dircache


def test_rm_permanent_deletes_and_updates_dircache(mocked_fs: MockedDriveFS) -> None:
    # permanent=True hard-deletes via files.delete instead of trashing.
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value=_deletable_info())
    fs.dircache["parent"] = [
        {"name": "parent/file", "id": "file-id", "size": 0, "type": "file"}
    ]
    fs.dircache["parent/file"] = empty_listing()

    fs.rm("parent/file", permanent=True)

    mocked_fs.files.delete.assert_called_once_with(
        fileId="file-id", supportsAllDrives=True
    )
    mocked_fs.files.update.assert_not_called()
    assert fs.dircache["parent"] == []
    assert "parent/file" not in fs.dircache


def test_rm_requests_capabilities_and_drive_id(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value=_trashable_info())

    fs._rm("parent/file")

    # Both capability signals must be resolved in the same info() call, not a
    # separate follow-up request.
    fs.info.assert_called_once_with(
        "parent/file",
        fields="driveId,capabilities/canDelete,capabilities/canTrash",
    )


def test_rm_no_trash_permission_raises(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(
        return_value={"id": "file-id", "capabilities": {"canTrash": False}}
    )

    with pytest.raises(PermissionError, match="Trash"):
        fs._rm("parent/file")

    # Nothing is trashed when the capability check fails.
    mocked_fs.files.update.assert_not_called()


def test_rm_permanent_no_delete_permission_on_shared_drive_raises_with_role_hint(
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
        fs._rm("parent/file", permanent=True)

    # Nothing is deleted when the capability check fails.
    mocked_fs.files.delete.assert_not_called()


def test_rm_permanent_no_delete_permission_on_my_drive_omits_shared_drive_advice(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    # No driveId → a My Drive file; the shared-drive role hint would mislead.
    fs.info = mock.Mock(
        return_value={"id": "file-id", "capabilities": {"canDelete": False}}
    )

    with pytest.raises(PermissionError) as excinfo:
        fs._rm("parent/file", permanent=True)

    assert "shared drives" not in str(excinfo.value)
    mocked_fs.files.delete.assert_not_called()


def test_rm_permanent_missing_capabilities_treated_as_not_deletable(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    # Absent capabilities block deletion rather than risk a masked-404 failure.
    fs.info = mock.Mock(return_value={"id": "file-id"})

    with pytest.raises(PermissionError):
        fs._rm("parent/file", permanent=True)

    mocked_fs.files.delete.assert_not_called()


def test_rm_missing_file_raises_file_not_found(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    # info() surfaces a genuinely missing file before the capability check.
    fs.info = mock.Mock(side_effect=FileNotFoundError("parent/file"))

    with pytest.raises(FileNotFoundError):
        fs._rm("parent/file")

    mocked_fs.files.delete.assert_not_called()
    mocked_fs.files.update.assert_not_called()


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
    mocked_fs.files.update.assert_not_called()


def test_rm_root_raises_value_error(mocked_fs: MockedDriveFS) -> None:
    # Deleting the root must fail clearly, not with a misleading PermissionError.
    fs = mocked_fs.fs

    with pytest.raises(ValueError, match="root"):
        fs._rm("")

    mocked_fs.files.delete.assert_not_called()
    mocked_fs.files.update.assert_not_called()


def test_rm_trashes_file(mocked_fs: MockedDriveFS) -> None:
    # rm -> _rm (our override) -> files.update (trash by default).
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=False)
    fs.info = mock.Mock(return_value=_trashable_info())

    fs.rm("file.txt")

    mocked_fs.files.update.assert_called_once_with(
        fileId="file-id", body={"trashed": True}, supportsAllDrives=True
    )


def test_rmdir_requires_directory(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=False)

    with pytest.raises(ValueError, match="not a directory"):
        fs.rmdir("file.txt")


def test_rmdir_trashes_empty_directory(mocked_fs: MockedDriveFS) -> None:
    # rmdir -> rm -> _rm (our override) -> files.update (trash by default).
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=True)
    fs.ls = mock.Mock(return_value=[])
    fs.info = mock.Mock(return_value=_trashable_info(id="dir-id"))

    fs.rmdir("empty")

    mocked_fs.files.update.assert_called_once_with(
        fileId="dir-id", body={"trashed": True}, supportsAllDrives=True
    )


def test_rmdir_permanent_forwards_flag(mocked_fs: MockedDriveFS) -> None:
    # permanent=True must thread through rmdir -> rm -> _rm to files.delete.
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=True)
    fs.ls = mock.Mock(return_value=[])
    fs.info = mock.Mock(return_value=_deletable_info(id="dir-id"))

    fs.rmdir("empty", permanent=True)

    mocked_fs.files.delete.assert_called_once_with(
        fileId="dir-id", supportsAllDrives=True
    )
    mocked_fs.files.update.assert_not_called()


def test_ls_from_dircache_returns_sorted_names(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs.dircache["parent"] = [
        {"name": "parent/b", "size": 0, "type": "file"},
        {"name": "parent/a", "size": 0, "type": "file"},
    ]

    assert anon_fs.ls("parent") == ["parent/a", "parent/b"]


def test_ls_from_dircache_detail(anon_fs: GoogleDriveFileSystem) -> None:
    listing: list[FileInfo] = [{"name": "parent/a", "size": 0, "type": "file"}]
    anon_fs.dircache["parent"] = listing

    assert anon_fs.ls("parent", detail=True) == listing


def test_ls_nested_directory(anon_fs: GoogleDriveFileSystem) -> None:
    # The directory is resolved with a targeted query; only its own contents
    # are listed (and cached) — the parent is never listed.
    anon_fs.root_file_id = "root"
    anon_fs._find_child_by_name = mock.Mock(
        return_value={
            "name": "parent",
            "id": "parent-id",
            "type": "directory",
            "size": 0,
            "mimeType": DIR_MIME_TYPE,
        }
    )
    anon_fs._list_children = mock.Mock(
        return_value=[
            {
                "name": "parent/child.txt",
                "id": "child-id",
                "type": "file",
                "size": 0,
                "mimeType": "text/plain",
            }
        ]
    )

    assert anon_fs.ls("parent") == ["parent/child.txt"]
    anon_fs._find_child_by_name.assert_called_once_with(
        "root", "parent", trashed=False, path_prefix=""
    )
    anon_fs._list_children.assert_called_once_with(
        "parent-id", trashed=False, path_prefix="parent", fields=None
    )
    assert anon_fs.dircache == {
        "parent": [
            {
                "name": "parent/child.txt",
                "id": "child-id",
                "type": "file",
                "size": 0,
                "mimeType": "text/plain",
            }
        ]
    }


def test_ls_nested_file_resolves_via_parent_listing(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs.dircache["parent"] = [
        {
            "name": "parent/child.txt",
            "id": "child-id",
            "size": 0,
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


def test_info_empty_fields_uses_listing_fast_path(mocked_fs: MockedDriveFS) -> None:
    # A blank mask is equivalent to no extra fields: reuse the listing entry.
    fs = mocked_fs.fs
    fs.dircache[""] = [
        {
            "name": "file.txt",
            "id": "file-id",
            "size": 0,
            "type": "file",
            "mimeType": "text/plain",
        }
    ]

    info = fs.info("file.txt", fields="")

    mocked_fs.files.get.assert_not_called()
    assert info["id"] == "file-id"


def test_info_ignores_reserved_kwargs_for_files_get(
    mocked_fs: MockedDriveFS,
) -> None:
    # Forwarded **kwargs must not collide with the explicit files.get args.
    fs = mocked_fs.fs
    fs.dircache[""] = [
        {
            "name": "file.txt",
            "id": "file-id",
            "size": 0,
            "type": "file",
            "mimeType": "text/plain",
        }
    ]
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "file-id",
        "name": "file.txt",
        "mimeType": "text/plain",
        "driveId": "drive-1",
    }

    info = fs.info(
        "file.txt", fields="driveId", supportsAllDrives=False, fileId="bogus"
    )

    _, kwargs = mocked_fs.files.get.call_args
    assert kwargs["fileId"] == "file-id"
    assert kwargs["supportsAllDrives"] is True
    assert info["driveId"] == "drive-1"


def test_ls_file_with_fields_enriches_via_files_get(
    mocked_fs: MockedDriveFS,
) -> None:
    # Extra fields on a file path come from one files.get on that file, not
    # from a fields-enriched listing of the whole parent.
    fs = mocked_fs.fs
    fs._resolve_entry = mock.Mock(
        return_value={"name": "file.txt", "id": "file-id", "type": "file"}
    )
    fs._list_children = mock.Mock()
    mocked_fs.files.get.return_value.execute.return_value = {
        "id": "file-id",
        "name": "file.txt",
        "mimeType": "text/plain",
        "driveId": "drive-1",
    }

    result = fs.ls("file.txt", detail=True, fields="driveId")

    mocked_fs.files.get.assert_called_once_with(
        fileId="file-id",
        fields=merge_fields(INFO_FIELDS, "driveId"),
        supportsAllDrives=True,
    )
    fs._list_children.assert_not_called()
    assert result[0]["driveId"] == "drive-1"


def test_info_cold_path_uses_targeted_resolution(
    mocked_fs: MockedDriveFS,
) -> None:
    # A cold info() resolves with targeted queries only: no directory
    # listings, no files.get, and no dircache writes.
    fs = mocked_fs.fs
    fs._find_child_by_name = mock.Mock(
        side_effect=[
            {"name": "a", "id": "a-id", "type": "directory"},
            {"name": "a/b", "id": "b-id", "type": "file"},
        ]
    )
    fs._list_children = mock.Mock()

    info = fs.info("a/b")

    assert info["id"] == "b-id"
    assert fs._find_child_by_name.call_count == 2
    fs._list_children.assert_not_called()
    mocked_fs.files.get.assert_not_called()
    assert fs.dircache == {}


def test_ls_non_canonical_bypasses_cache_read_and_write(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    fetched = [{"name": "a", "id": "1", "type": "file", "driveId": "drive-1"}]
    anon_fs.dircache[""] = [{"name": "a", "id": "1", "size": 0, "type": "file"}]
    anon_fs._list_children = mock.Mock(return_value=fetched)

    result = anon_fs.ls("", detail=True, fields="driveId")

    anon_fs._list_children.assert_called_once()
    assert result[0]["driveId"] == "drive-1"
    assert "driveId" not in anon_fs.dircache[""][0]

    anon_fs.dircache.clear()
    anon_fs._list_children.reset_mock()

    anon_fs.ls("", detail=True, fields="driveId")

    anon_fs._list_children.assert_called_once()
    assert "" not in anon_fs.dircache


def test_ls_fields_without_detail_raises(anon_fs: GoogleDriveFileSystem) -> None:
    # Requesting fields with detail=False would silently discard them, since the
    # names-only result carries no metadata. Reject the misuse instead.
    anon_fs._list_children = mock.Mock()

    with pytest.raises(ValueError, match="detail=True"):
        anon_fs.ls("", fields="driveId")

    anon_fs._list_children.assert_not_called()


def test_ls_empty_fields_uses_cache(anon_fs: GoogleDriveFileSystem) -> None:
    # A blank mask must not bypass the cache; it means no extra fields.
    cached: list[FileInfo] = [{"name": "a", "id": "1", "size": 0, "type": "file"}]
    anon_fs.dircache[""] = cached
    anon_fs._list_children = mock.Mock()

    result = anon_fs.ls("", detail=True, fields="")

    assert result == cached
    anon_fs._list_children.assert_not_called()


def test_ls_skips_cache_when_trashed_requested(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    canonical: list[FileInfo] = [{"name": "a", "id": "1", "size": 0, "type": "file"}]
    anon_fs.dircache[""] = list(canonical)
    anon_fs._list_children = mock.Mock(
        return_value=[{"name": "trashed-a", "id": "2", "type": "file", "trashed": True}]
    )

    result = anon_fs.ls("", detail=True, trashed=True)

    anon_fs._list_children.assert_called_once()
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
            "size": 0,
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
        {"name": "parent/other", "id": "other-id", "size": 0, "type": "file"}
    ]
    with pytest.raises(FileNotFoundError):
        anon_fs._path_to_id("parent/child")


def test_path_to_id_duplicate_raises_multiple_files_error(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    # Ambiguous resolution must fail, not silently pick one, so callers like the
    # write path can refuse to overwrite an arbitrary duplicate.
    anon_fs.dircache["parent"] = [
        {"name": "parent/dup", "id": "1", "size": 0, "type": "file"},
        {"name": "parent/dup", "id": "2", "size": 0, "type": "file"},
    ]
    with pytest.raises(MultipleFilesError):
        anon_fs._path_to_id("parent/dup")


def test_resolve_entry_rejects_root(anon_fs: GoogleDriveFileSystem) -> None:
    # Root has no parent to list; callers must handle it before resolving.
    with pytest.raises(ValueError, match="root"):
        anon_fs._resolve_entry("")


def _dir_entry(path: str, file_id: str) -> FileInfo:
    return {
        "name": path,
        "id": file_id,
        "size": 0,
        "type": "directory",
        "mimeType": DIR_MIME_TYPE,
    }


def _file_entry(path: str, file_id: str) -> FileInfo:
    return {
        "name": path,
        "id": file_id,
        "size": 0,
        "type": "file",
        "mimeType": "text/plain",
    }


def test_resolve_entry_cold_deep_path_uses_targeted_queries(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    # One targeted query per component, never a directory listing.
    anon_fs.root_file_id = "root-id"
    by_parent = {
        ("root-id", "a"): _dir_entry("a", "a-id"),
        ("a-id", "b"): _dir_entry("a/b", "b-id"),
        ("b-id", "f"): _file_entry("a/b/f", "f-id"),
    }
    anon_fs._find_child_by_name = mock.Mock(
        side_effect=lambda parent_id, name, **kw: by_parent[(parent_id, name)]
    )
    anon_fs._list_children = mock.Mock()

    entry = anon_fs._resolve_entry("a/b/f")

    assert entry["id"] == "f-id"
    assert anon_fs._find_child_by_name.call_count == 3
    anon_fs._list_children.assert_not_called()
    assert anon_fs.dircache == {}


def test_resolve_entry_anchors_at_deepest_cached_ancestor(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs.dircache["a/b"] = [_dir_entry("a/b/c", "c-id")]
    anon_fs._find_child_by_name = mock.Mock(return_value=_file_entry("a/b/c/d", "d-id"))

    entry = anon_fs._resolve_entry("a/b/c/d")

    assert entry["id"] == "d-id"
    anon_fs._find_child_by_name.assert_called_once_with(
        "c-id", "d", trashed=False, path_prefix="a/b/c"
    )


def test_resolve_entry_trashed_bypasses_cache(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    # Cached listings exclude trashed files, so they cannot answer trashed
    # lookups.
    anon_fs.root_file_id = "root-id"
    anon_fs.dircache[""] = empty_listing()
    anon_fs._find_child_by_name = mock.Mock(return_value=_file_entry("gone", "gone-id"))

    entry = anon_fs._resolve_entry("gone", trashed=True)

    assert entry["id"] == "gone-id"
    anon_fs._find_child_by_name.assert_called_once_with(
        "root-id", "gone", trashed=True, path_prefix=""
    )


def test_resolve_entry_intermediate_file_raises(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    # "a" is a file, so nothing can exist below it; no API call needed.
    anon_fs.dircache[""] = [_file_entry("a", "a-id")]
    anon_fs._find_child_by_name = mock.Mock()

    with pytest.raises(FileNotFoundError):
        anon_fs._resolve_entry("a/b")

    anon_fs._find_child_by_name.assert_not_called()


def test_google_drive_file_normalizes_pathlike(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs._path_to_id = mock.Mock(return_value="file-id")
    fs.info = mock.Mock(
        return_value={"id": "file-id", "size": 0, "type": "file", "name": "file.txt"}
    )

    opened = GoogleDriveFile(fs, pathlib.PurePosixPath("file.txt"), mode="rb")

    assert opened.path == "file.txt"
    fs._path_to_id.assert_called_once_with("file.txt")


def test_ls_file_resolves_directly(anon_fs: GoogleDriveFileSystem) -> None:
    # ls on a file returns the file's own entry without listing its parent.
    anon_fs._find_child_by_name = mock.Mock(
        return_value={
            "name": "file.txt",
            "id": "file-id",
            "type": "file",
            "mimeType": "text/plain",
        }
    )
    anon_fs._list_children = mock.Mock()

    assert anon_fs.ls("file.txt") == ["file.txt"]
    anon_fs._list_children.assert_not_called()
    assert anon_fs.dircache == {}


def test_ls_raises_multiple_files_error(mocked_fs: MockedDriveFS) -> None:
    # The duplicate is detected by the real _find_child_by_name from the raw
    # files.list response.
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = {
        "files": [
            {"name": "dup", "id": "1", "mimeType": "text/plain"},
            {"name": "dup", "id": "2", "mimeType": "text/plain"},
        ]
    }
    mocked_fs.files.list.return_value = list_request

    with pytest.raises(MultipleFilesError):
        fs.ls("dup")


def test_list_children_paginates(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.side_effect = [
        {"files": [{"name": "a", "id": "1"}], "nextPageToken": "page-2"},
        {"files": [{"name": "b", "id": "2"}]},
    ]
    mocked_fs.files.list.return_value = list_request

    result = fs._list_children("folder-id")

    assert len(result) == 2
    assert mocked_fs.files.list.call_count == 2
    assert mocked_fs.files.list.call_args_list[1].kwargs["pageToken"] == "page-2"


def test_list_children_passes_num_retries(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_children("folder-id")

    list_request.execute.assert_called_once_with(num_retries=_NUM_RETRIES)


def test_list_children_shared_drive_root_query(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    fs.drive = "drive-123"
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_children(ROOT_ID)

    query = mocked_fs.files.list.call_args.kwargs["q"]
    assert "'drive-123' in parents" in query
    assert "trashed = false" in query


def test_list_children_includes_trashed_when_requested(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_children("folder-id", trashed=True)

    query = mocked_fs.files.list.call_args.kwargs["q"]
    assert "trashed = false" not in query


def test_list_children_passes_drive_kwargs(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.drive = "drive-123"
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_children("folder-id")

    kwargs = mocked_fs.files.list.call_args.kwargs
    assert kwargs["driveId"] == "drive-123"
    assert kwargs["supportsAllDrives"] is True


def _files_list_response(
    *names_and_ids: tuple[str, str], token: str | None = None
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "files": [
            {"name": name, "id": file_id, "mimeType": "text/plain"}
            for name, file_id in names_and_ids
        ]
    }
    if token is not None:
        response["nextPageToken"] = token
    return response


def test_find_child_by_name_builds_targeted_query(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._find_child_by_name("folder-id", "it's here")

    kwargs = mocked_fs.files.list.call_args.kwargs
    assert kwargs["q"] == (
        "name = 'it\\'s here' and 'folder-id' in parents and trashed = false"
    )
    assert kwargs["fields"] == f"nextPageToken, files({INFO_FIELDS})"
    assert kwargs["spaces"] == fs.spaces
    assert "orderBy" not in kwargs
    list_request.execute.assert_called_once_with(num_retries=_NUM_RETRIES)


def test_find_child_by_name_includes_trashed_when_requested(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._find_child_by_name("folder-id", "file.txt", trashed=True)

    assert "trashed = false" not in mocked_fs.files.list.call_args.kwargs["q"]


def test_find_child_by_name_shared_drive(mocked_fs: MockedDriveFS) -> None:
    # The legacy "root" alias is replaced with the shared-drive id in the
    # query, and drive-scoping kwargs are forwarded.
    fs = mocked_fs.fs
    fs.drive = "drive-123"
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._find_child_by_name(ROOT_ID, "file.txt")

    kwargs = mocked_fs.files.list.call_args.kwargs
    assert "'drive-123' in parents" in kwargs["q"]
    assert kwargs["driveId"] == "drive-123"
    assert kwargs["supportsAllDrives"] is True


def test_find_child_by_name_no_match_returns_none(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    assert fs._find_child_by_name("folder-id", "missing.txt") is None


def test_find_child_by_name_returns_normalized_entry(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = {
        "files": [
            {"name": "file.txt", "id": "file-id", "mimeType": "text/plain", "size": "3"}
        ]
    }
    mocked_fs.files.list.return_value = list_request

    entry = fs._find_child_by_name("folder-id", "file.txt", path_prefix="parent")

    assert entry == {
        "name": "parent/file.txt",
        "id": "file-id",
        "mimeType": "text/plain",
        "size": 3,
        "type": "file",
    }


def test_find_child_by_name_duplicate_raises(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = _files_list_response(
        ("dup.txt", "1"), ("dup.txt", "2")
    )
    mocked_fs.files.list.return_value = list_request

    with pytest.raises(MultipleFilesError, match="parent/dup.txt"):
        fs._find_child_by_name("folder-id", "dup.txt", path_prefix="parent")


def test_find_child_by_name_ignores_case_variants(mocked_fs: MockedDriveFS) -> None:
    # Drive matches name = '...' case-insensitively; only exact matches count.
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = _files_list_response(
        ("README.txt", "1"), ("Readme.txt", "2")
    )
    mocked_fs.files.list.return_value = list_request

    assert fs._find_child_by_name("folder-id", "readme.txt") is None


def test_find_child_by_name_paginates_past_case_variants(
    mocked_fs: MockedDriveFS,
) -> None:
    # The exact match may sit on a later page when case-variants fill the first;
    # raw result counts and page tokens say nothing about true duplicates.
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.side_effect = [
        _files_list_response(("README.txt", "1"), token="page-2"),
        _files_list_response(("readme.txt", "2")),
    ]
    mocked_fs.files.list.return_value = list_request

    entry = fs._find_child_by_name("folder-id", "readme.txt")

    assert entry is not None and entry["id"] == "2"
    assert mocked_fs.files.list.call_count == 2
    assert mocked_fs.files.list.call_args.kwargs["pageToken"] == "page-2"


def test_find_child_by_name_duplicate_across_pages_raises(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.side_effect = [
        _files_list_response(("dup.txt", "1"), token="page-2"),
        _files_list_response(("dup.txt", "2")),
    ]
    mocked_fs.files.list.return_value = list_request

    with pytest.raises(MultipleFilesError):
        fs._find_child_by_name("folder-id", "dup.txt")


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

        def next_chunk(**kwargs: Any) -> tuple[mock.Mock, bool]:
            assert kwargs.get("num_retries") == _NUM_RETRIES
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


# ---------------------------------------------------------------------------
# Changes-API cache synchronization
# ---------------------------------------------------------------------------


def _changes_http_error(status: int) -> HttpError:
    resp = mock.Mock(status=status, reason="Error")
    return HttpError(resp, b'{"error": {"message": "token"}}')


def _mock_changes(
    service: mock.Mock,
    pages: list[dict[str, Any]],
    start_token: str = "TOK0",
) -> tuple[mock.Mock, mock.Mock]:
    """Stub service.changes().list() (via side_effect) and getStartPageToken()."""
    list_request = mock.Mock()
    list_request.execute.side_effect = pages
    service.changes.return_value.list.return_value = list_request

    start_request = mock.Mock()
    start_request.execute.return_value = {"startPageToken": start_token}
    service.changes.return_value.getStartPageToken.return_value = start_request
    return list_request, start_request


def _dir(path: str, file_id: str) -> FileInfo:
    return {
        "name": path,
        "id": file_id,
        "size": 0,
        "type": "directory",
        "mimeType": DIR_MIME_TYPE,
    }


def _file(path: str, file_id: str) -> FileInfo:
    return {
        "name": path,
        "id": file_id,
        "size": 0,
        "type": "file",
        "mimeType": "text/plain",
    }


def _new(name: str, *parent_ids: str) -> dict[str, Any]:
    """A change's ``file`` resource: current name + parent folder ids."""
    return {"id": f"{name}-id", "name": name, "parents": list(parent_ids)}


def _enable_sync(fs: GoogleDriveFileSystem, interval: float = 60) -> None:
    fs._changes_sync_interval = interval
    fs._changes_page_token = "T"
    fs._last_sync_monotonic = None
    # These tests simulate an authenticated sync-enabled instance; the anon_fs
    # fixture is anonymous, which the read hook (correctly) treats as dormant.
    fs._is_anonymous = False
    # Seed the cached real root id so _build_dir_id_to_path does not issue a
    # files.get during sync. Tests that exercise root-level changes override it.
    fs.__dict__.setdefault("_resolved_root_id", fs.root_file_id)


def test_first_read_baselines_only(mocked_fs: MockedDriveFS) -> None:
    # With no page token yet, the first read establishes the baseline and
    # reconciles nothing (the change feed is not polled).
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs._changes_page_token = None
    list_request, _ = _mock_changes(mocked_fs.service, [])
    fs.dircache["d"] = [_file("d/x", "x-id")]

    fs._maybe_sync_cache()

    assert fs._changes_page_token == "TOK0"
    list_request.execute.assert_not_called()
    assert fs.dircache["d"] == [_file("d/x", "x-id")]


def test_resolved_root_id_resolves_alias(mocked_fs: MockedDriveFS) -> None:
    # The "root" alias must resolve to the concrete folder id, since the change
    # feed reports real ids (a create at root would otherwise never map).
    fs = mocked_fs.fs
    fs.root_file_id = ROOT_ID
    mocked_fs.files.get.return_value.execute.return_value = {"id": "real-root-id"}

    assert fs._resolved_root_id == "real-root-id"
    mocked_fs.files.get.assert_called_once_with(fileId=ROOT_ID, fields="id")


def test_resolved_root_id_passes_through_real_id(mocked_fs: MockedDriveFS) -> None:
    # A concrete root id (shared drive / explicit root_file_id) needs no lookup.
    fs = mocked_fs.fs
    fs.root_file_id = "folder-123"

    assert fs._resolved_root_id == "folder-123"
    mocked_fs.files.get.assert_not_called()


def test_sync_invalidates_root_via_resolved_id(mocked_fs: MockedDriveFS) -> None:
    # Regression: a change under the My Drive root ("root" alias) must invalidate
    # the cached root listing. The change reports the REAL parent id, so mapping
    # by the alias would silently miss it.
    fs = mocked_fs.fs
    fs.root_file_id = ROOT_ID
    fs.__dict__["_resolved_root_id"] = "real-root-id"  # seed the cached_property
    _enable_sync(fs)
    fs.dircache[""] = [_file("at_root.txt", "child-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [
                    {
                        "removed": False,
                        "fileId": "new-id",
                        "file": _new("new", "real-root-id"),
                    }
                ],
            }
        ],
    )

    fs._sync_cache()

    assert "" not in fs.dircache  # root listing dropped despite the alias


def test_sync_surgical_invalidate_on_create_in_cached_dir(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("d", "d-id")]
    fs.dircache["d"] = [_file("d/old", "old-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [
                    {"removed": False, "fileId": "new-id", "file": _new("new", "d-id")}
                ],
            }
        ],
    )

    fs._sync_cache()

    assert "d" not in fs.dircache  # the listing that gained a child was dropped
    assert "" in fs.dircache  # unaffected sibling listing kept
    assert fs._changes_page_token == "T2"


def test_sync_move_invalidates_old_and_new_dirs(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("a", "a-id"), _dir("b", "b-id")]
    fs.dircache["a"] = [_file("a/f", "f-id")]  # f currently lives in a
    fs.dircache["b"] = empty_listing()
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                # f moved to b: change reports the NEW parent only.
                "changes": [
                    {"removed": False, "fileId": "f-id", "file": _new("f", "b-id")}
                ],
            }
        ],
    )

    fs._sync_cache()

    assert "a" not in fs.dircache  # old location, via id_to_paths
    assert "b" not in fs.dircache  # new location, via parents
    assert "" in fs.dircache


def test_sync_renamed_cached_directory_drops_its_own_listing(
    mocked_fs: MockedDriveFS,
) -> None:
    # Regression: renaming a cached directory must drop the listing keyed by its
    # OWN (now-stale) path, not just the parent listing that contained it.
    # Otherwise ls("foo") keeps serving contents for a path that no longer exists.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("foo", "foo-id")]
    fs.dircache["foo"] = [_file("foo/child.txt", "child-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                # foo renamed to bar, still under root.
                "changes": [
                    {
                        "removed": False,
                        "fileId": "foo-id",
                        "file": _new("bar", fs.root_file_id),
                    }
                ],
            }
        ],
    )

    fs._sync_cache()

    assert "" not in fs.dircache  # parent listing (name changed) dropped
    assert "foo" not in fs.dircache  # the dir's own stale listing dropped


def test_sync_moved_cached_directory_drops_subtree(
    mocked_fs: MockedDriveFS,
) -> None:
    # Moving a cached directory must drop its own listing AND all descendant
    # keys, whose paths are now stale.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("src", "src-id"), _dir("dst", "dst-id")]
    fs.dircache["src"] = [_dir("src/sub", "sub-id")]
    fs.dircache["src/sub"] = [_file("src/sub/leaf.txt", "leaf-id")]
    fs.dircache["dst"] = empty_listing()
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                # src moved under dst: change reports only the NEW parent dst-id.
                "changes": [
                    {
                        "removed": False,
                        "fileId": "src-id",
                        "file": _new("src", "dst-id"),
                    }
                ],
            }
        ],
    )

    fs._sync_cache()

    assert "src" not in fs.dircache  # own listing dropped
    assert "src/sub" not in fs.dircache  # descendant subtree dropped
    assert "dst" not in fs.dircache  # new location, via parents
    assert "" not in fs.dircache  # old location, via id_to_paths


def test_sync_removed_cached_directory_drops_subtree(
    mocked_fs: MockedDriveFS,
) -> None:
    # A hard-deleted cached directory (removed=True, no file) must still drop its
    # own listing and descendants, reached via id_to_paths + the subtree purge.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("gone", "gone-id")]
    fs.dircache["gone"] = [_file("gone/leaf.txt", "leaf-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [{"removed": True, "fileId": "gone-id"}],
            }
        ],
    )

    fs._sync_cache()

    assert "gone" not in fs.dircache
    assert "" not in fs.dircache


def test_sync_batch_accumulates_surgical_invalidations(
    mocked_fs: MockedDriveFS,
) -> None:
    # A single drain returns many changes; the reducer must accumulate a pop for
    # each affected dir. Here creates land in two different cached dirs and a
    # third change removes a cached file from a third dir.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("a", "a-id"), _dir("b", "b-id"), _dir("c", "c-id")]
    fs.dircache["a"] = empty_listing()
    fs.dircache["b"] = empty_listing()
    fs.dircache["c"] = [_file("c/old", "old-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [
                    {"removed": False, "fileId": "n1", "file": _new("n1", "a-id")},
                    {"removed": False, "fileId": "n2", "file": _new("n2", "b-id")},
                    {"removed": True, "fileId": "old-id"},
                ],
            }
        ],
    )

    fs._sync_cache()

    # Every affected dir dropped; the untouched root listing survives.
    assert "a" not in fs.dircache
    assert "b" not in fs.dircache
    assert "c" not in fs.dircache
    assert "" in fs.dircache


def test_sync_batch_full_clear_supersedes_surgical(mocked_fs: MockedDriveFS) -> None:
    # In a mixed batch, a single unmappable change forces a full clear that
    # discards any surgical work from other changes in the same batch — a
    # cleared cache is a superset of any partial invalidation, so this is safe.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("d", "d-id")]
    fs.dircache["d"] = empty_listing()
    # An unmapped cached dir makes an unresolved parent unsafe -> full clear.
    fs.dircache["orphan"] = [_file("orphan/x", "x-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [
                    # Surgical: maps to cached dir "d"...
                    {"removed": False, "fileId": "n1", "file": _new("n1", "d-id")},
                    # ...then an unresolvable parent forces a full clear.
                    {"removed": False, "fileId": "n2", "file": _new("n2", "UNKNOWN")},
                ],
            }
        ],
    )

    fs._sync_cache()

    assert fs.dircache == {}  # everything dropped, not just "d"


def test_sync_removed_cached_file_invalidates_its_dir(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache["a"] = [_file("a/f", "f-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [{"removed": True, "fileId": "f-id"}],
            }
        ],
    )

    fs._sync_cache()

    assert "a" not in fs.dircache


def test_sync_removed_uncached_file_is_noop(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache["a"] = [_file("a/f", "f-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [{"removed": True, "fileId": "unknown-id"}],
            }
        ],
    )

    fs._sync_cache()

    assert fs.dircache["a"] == [_file("a/f", "f-id")]  # untouched, no full clear


def test_sync_unresolved_parent_with_unmapped_dir_full_clears(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    # "orphan" is cached but its id is not derivable (no parent listing lists
    # it), so an unresolvable parent could be it -> must full-clear.
    fs.dircache["orphan"] = [_file("orphan/f", "f-id")]
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [
                    {
                        "removed": False,
                        "fileId": "new-id",
                        "file": _new("new", "UNKNOWN-PARENT"),
                    }
                ],
            }
        ],
    )

    fs._sync_cache()

    assert fs.dircache == {}


def test_sync_unresolved_parent_ignored_when_cache_fully_mapped(
    mocked_fs: MockedDriveFS,
) -> None:
    # Every cached dir's id is derivable (top-down cache), so an unresolvable
    # parent must target an uncached dir -> no clear, no invalidation.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("d", "d-id")]
    fs.dircache["d"] = empty_listing()
    _mock_changes(
        mocked_fs.service,
        [
            {
                "newStartPageToken": "T2",
                "changes": [
                    {
                        "removed": False,
                        "fileId": "x",
                        "file": _new("x", "UNKNOWN-PARENT"),
                    }
                ],
            }
        ],
    )

    fs._sync_cache()

    assert set(fs.dircache) == {"", "d"}


def test_sync_token_expiry_rebaselines_and_clears(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs._changes_page_token = "OLD"
    fs.dircache["d"] = [_file("d/x", "x-id")]
    list_request, _ = _mock_changes(mocked_fs.service, [], start_token="TOK1")
    list_request.execute.side_effect = _changes_http_error(410)

    fs._sync_cache()

    assert fs.dircache == {}
    assert fs._changes_page_token == "TOK1"


@pytest.mark.parametrize("status", [400, 404, 410])
def test_sync_token_expiry_statuses(mocked_fs: MockedDriveFS, status: int) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache["d"] = [_file("d/x", "x-id")]
    list_request, _ = _mock_changes(mocked_fs.service, [], start_token="TOK1")
    list_request.execute.side_effect = _changes_http_error(status)

    fs._sync_cache()

    assert fs.dircache == {}
    assert fs._changes_page_token == "TOK1"


def test_sync_non_token_error_propagates(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    list_request, _ = _mock_changes(mocked_fs.service, [])
    list_request.execute.side_effect = _changes_http_error(500)

    with pytest.raises(HttpError):
        fs._sync_cache()


def test_maybe_sync_swallows_sync_failure(mocked_fs: MockedDriveFS) -> None:
    # A sync failure on the cache-read hook must never break the read: the
    # cached listing is served and no exception escapes.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache["d"] = [_file("d/x", "x-id")]
    list_request, _ = _mock_changes(mocked_fs.service, [])
    list_request.execute.side_effect = _changes_http_error(500)

    # ls goes through _maybe_sync_cache; it must not raise.
    assert fs.ls("d") == ["d/x"]


def test_ls_survives_sync_failure(mocked_fs: MockedDriveFS) -> None:
    # End-to-end: even a non-recoverable sync error leaves ls serving the cache.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_file("cached.txt", "c-id")]
    mocked_fs.service.changes.return_value.list.side_effect = RuntimeError("boom")

    assert fs.ls("", detail=True) == [_file("cached.txt", "c-id")]


def test_sync_pagination_persists_new_start_token(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache[""] = [_dir("d", "d-id")]
    fs.dircache["d"] = empty_listing()
    _mock_changes(
        mocked_fs.service,
        [
            {
                "nextPageToken": "P2",
                "changes": [
                    {"removed": False, "fileId": "n1", "file": _new("n1", "d-id")}
                ],
            },
            {
                "newStartPageToken": "TOK9",
                "changes": [{"removed": True, "fileId": "d-id"}],
            },
        ],
    )

    fs._sync_cache()

    # Page 2 continuation used P2, and the final token is persisted.
    list_calls = mocked_fs.service.changes.return_value.list.call_args_list
    assert list_calls[1].kwargs["pageToken"] == "P2"
    assert fs._changes_page_token == "TOK9"
    assert "d" not in fs.dircache  # create-in-d from page 1 applied
    assert "" not in fs.dircache  # d-id removed on page 2 invalidated root


def test_sync_shared_drive_scope(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.drive = "DRV"
    _enable_sync(fs)
    _mock_changes(mocked_fs.service, [{"newStartPageToken": "T2", "changes": []}])

    fs._sync_cache()

    list_kwargs = mocked_fs.service.changes.return_value.list.call_args.kwargs
    assert list_kwargs["driveId"] == "DRV"
    assert list_kwargs["includeItemsFromAllDrives"] is True
    assert list_kwargs["supportsAllDrives"] is True
    assert list_kwargs["includeRemoved"] is True
    assert list_kwargs["fields"] == _CHANGES_FIELDS
    assert list_kwargs["pageSize"] == _CHANGES_PAGE_SIZE


def test_sync_my_drive_scope_omits_drive_id(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.drive = None
    _enable_sync(fs)
    fs._changes_page_token = None
    _mock_changes(mocked_fs.service, [])

    fs._maybe_sync_cache()  # baselines

    start_kwargs = (
        mocked_fs.service.changes.return_value.getStartPageToken.call_args.kwargs
    )
    assert "driveId" not in start_kwargs


def test_maybe_sync_dormant_when_interval_none(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs._changes_sync_interval = None
    fs.dircache["d"] = [_file("d/x", "x-id")]

    fs.ls("d")
    fs.info("d/x")

    mocked_fs.service.changes.assert_not_called()


def test_maybe_sync_dormant_when_anonymous(mocked_fs: MockedDriveFS) -> None:
    # If an instance is reconnected to anonymous credentials after sync was
    # enabled, the read hook must stay dormant (the Changes API needs auth).
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs._is_anonymous = True
    fs.dircache["d"] = [_file("d/x", "x-id")]

    fs.ls("d")
    fs.info("d/x")

    mocked_fs.service.changes.assert_not_called()


def test_maybe_sync_failed_baseline_gates_next_read(mocked_fs: MockedDriveFS) -> None:
    # A failing lazy baseline must still consume the TTL window, so repeated
    # reads do not hot-loop retrying (and log-spamming) the baseline call.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs._changes_page_token = None  # force the lazy-baseline path
    fs.dircache["d"] = [_file("d/x", "x-id")]
    _, start_request = _mock_changes(mocked_fs.service, [])
    start_request.execute.side_effect = _changes_http_error(500)

    with mock.patch(
        "gdrive_fsspec.core.time.monotonic", side_effect=[100.0, 100.0, 130.0, 130.0]
    ):
        fs.ls("d")  # baseline attempt fails, but the TTL is stamped
        fs.ls("d")  # within the window -> must NOT retry

    # Only the first read attempted the baseline; the second was gated.
    start_request.execute.assert_called_once()
    assert fs.ls("d") == ["d/x"]  # cache still served throughout


def test_maybe_sync_baseline_gates_like_any_attempt(
    mocked_fs: MockedDriveFS,
) -> None:
    # The baseline is a sync attempt like any other: it stamps the interval
    # clock, so a read within the window does not immediately re-sync. The first
    # reconciliation happens on the first read past the interval.
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs._changes_page_token = None  # first read baselines
    fs.dircache["d"] = [_file("d/x", "x-id")]
    list_request, _ = _mock_changes(
        mocked_fs.service, [{"newStartPageToken": "T2", "changes": []}]
    )

    with mock.patch(
        "gdrive_fsspec.core.time.monotonic", side_effect=[100.0, 101.0, 200.0]
    ):
        fs.ls("d")  # baselines (TOK0) and stamps the clock at t=100
        assert fs._changes_page_token == "TOK0"
        fs.ls("d")  # t=101, within the 60s interval -> gated, no reconcile
        list_request.execute.assert_not_called()
        fs.ls("d")  # t=200, past the interval -> first reconciliation runs

    list_request.execute.assert_called_once()


def test_maybe_sync_ttl_gates_second_call(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs, interval=60)
    fs.dircache["d"] = [_file("d/x", "x-id")]
    _mock_changes(mocked_fs.service, [{"newStartPageToken": "T2", "changes": []}])

    with mock.patch(
        "gdrive_fsspec.core.time.monotonic", side_effect=[100.0, 100.0, 130.0, 130.0]
    ):
        fs.ls("d")  # syncs (first call)
        fs.ls("d")  # within TTL window -> no sync

    list_request = mocked_fs.service.changes.return_value.list.return_value
    list_request.execute.assert_called_once()


def test_ls_fields_or_trashed_skips_sync(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs.dircache["d"] = [_file("d/x", "x-id")]
    fs._resolve_entry = mock.Mock(return_value=_dir("d", "d-id"))
    mocked_fs.files.list.return_value.execute.return_value = empty_files_list_response()

    fs.ls("d", detail=True, fields="driveId")
    fs.ls("d", detail=True, trashed=True)

    mocked_fs.service.changes.assert_not_called()


def test_info_trashed_skips_sync(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    _enable_sync(fs)
    fs._resolve_entry = mock.Mock(return_value=_file("d/x", "x-id"))

    fs.info("d/x", trashed=True)

    mocked_fs.service.changes.assert_not_called()
