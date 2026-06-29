# ---------------------------------------------------------------------------
# Integration tests (require live Google Drive credentials)
#
# Run: uv run pytest -v -m integration
#
# Auth (pick one):
#   Service account (CI default):
#     GDRIVE_FSSPEC_CREDENTIALS_PATH=/path/to/sa.json
#     GDRIVE_FSSPEC_DRIVE=your-shared-drive-name
#   User OAuth (My Drive or a shared drive you can access):
#     GDRIVE_FSSPEC_CREDENTIALS_TYPE=cache   # or browser for first login
#     GDRIVE_FSSPEC_DRIVE=optional-shared-drive-name
# ---------------------------------------------------------------------------

from typing import cast

import pytest
from conftest import TESTDIR, FsFactory

from gdrive_fsspec.core import GoogleDriveFile, GoogleDriveFileSystem

# Listing cache can be stale or wrong after writes/deletes.
DIRCACHE_XFAIL = pytest.mark.xfail(
    reason="dircache not updated correctly after mutations",
    strict=True,
)


def _test_path(name: str) -> str:
    return f"{TESTDIR}/{name}"


@pytest.mark.integration
def test_simple(fs: GoogleDriveFileSystem) -> None:
    assert fs.ls("")
    data = b"hello"
    filename = _test_path("testfile")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)
    assert fs.cat(filename) == data


@pytest.mark.integration
def test_overwrite_updates_in_place(fs: GoogleDriveFileSystem) -> None:
    """Writing to an existing path updates it instead of creating a duplicate."""
    filename = _test_path("overwrite")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"first")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"second longer content")

    fs.invalidate_cache()
    entries = fs.ls(TESTDIR, detail=True)
    match = [e for e in entries if e["name"] == filename]
    assert len(match) == 1, "overwrite created a duplicate child"
    assert fs.cat(filename) == b"second longer content"


@pytest.mark.integration
def test_overwrite_preserves_file_id(fs: GoogleDriveFileSystem) -> None:
    """Overwrite PATCHes the existing file rather than delete-and-recreate."""
    filename = _test_path("overwrite_id")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"v1")
    fs.invalidate_cache()
    original_id = fs.info(filename)["id"]

    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"v2")
    fs.invalidate_cache()

    assert fs.info(filename)["id"] == original_id
    assert fs.cat(filename) == b"v2"


@pytest.mark.integration
@DIRCACHE_XFAIL
def test_overwrite_updates_live_dircache(fs: GoogleDriveFileSystem) -> None:
    """The live dircache reflects an overwrite without re-listing.

    Exercises the in-place dircache update on commit: ``ls`` populates the
    cache, the overwrite must replace that entry (not append a second one).

    Currently xfails on the broader dircache-drift bug: ``ls`` on a directory
    whose parent is already cached never caches that directory's own children,
    so the commit-time update has no entry to replace. The server-state tests
    above confirm the overwrite itself is correct.
    """
    filename = _test_path("overwrite_cache")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"one")
    # Populate the dircache for the parent.
    fs.ls(TESTDIR, detail=True)

    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"two!")

    entries = fs.ls(TESTDIR, detail=True)  # served from cache
    match = [e for e in entries if e["name"] == filename]
    assert len(match) == 1
    assert match[0]["size"] == 4


@pytest.mark.integration
def test_create_directory(fs: GoogleDriveFileSystem) -> None:
    fs.makedirs(_test_path("data"))
    fs.makedirs(_test_path("data/bar/baz"))

    assert fs.exists(_test_path("data"))
    assert fs.exists(_test_path("data/bar"))
    assert fs.exists(_test_path("data/bar/baz"))

    data = b"intermediate path"
    with fs.open(_test_path("data/bar/test"), "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)
    assert fs.cat(_test_path("data/bar/test")) == data


@pytest.mark.integration
def test_rm_file_removes_from_listing(fs: GoogleDriveFileSystem) -> None:
    filename = _test_path("to_delete")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"gone soon")

    assert fs.exists(filename)
    fs.rm(filename)
    assert not fs.exists(filename)


@pytest.mark.integration
@DIRCACHE_XFAIL
def test_rm_recursive_deletes_directory_tree(fs: GoogleDriveFileSystem) -> None:
    root = _test_path("tree")
    fs.makedirs(root + "/a/b")
    with fs.open(root + "/a/b/leaf", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"leaf")

    fs.rm(root, recursive=True)

    assert not fs.exists(root)
    assert not fs.exists(root + "/a")
    assert not fs.exists(root + "/a/b/leaf")


@pytest.mark.integration
def test_rmdir_empty_directory(fs: GoogleDriveFileSystem) -> None:
    path = _test_path("empty_dir")
    fs.mkdir(path)

    assert fs.exists(path)
    fs.rmdir(path)
    assert not fs.exists(path)


@pytest.mark.integration
def test_rmdir_non_empty_raises(fs: GoogleDriveFileSystem) -> None:
    path = _test_path("nonempty_dir")
    fs.mkdir(path)
    with fs.open(path + "/child", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"x")

    with pytest.raises(ValueError, match="non-empty"):
        fs.rmdir(path)


@pytest.mark.integration
def test_read_with_seek(fs: GoogleDriveFileSystem) -> None:
    data = b"0123456789abcdef"
    filename = _test_path("seekable")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)

    with fs.open(filename, "rb") as f:
        f.seek(4)
        assert f.read(4) == b"4567"
        assert f.read(2) == b"89"


@pytest.mark.integration
def test_multiblock_upload(fs: GoogleDriveFileSystem) -> None:
    """Resumable upload across multiple chunks.

    With the 5 MiB default block size every write fits in a single chunk, so
    the 308-continuation loop in ``_upload_chunk`` (and its ``Content-Range:
    bytes X-Y/*`` wildcard) is otherwise never exercised live. A small block
    size forces several chunks plus the final commit.
    """
    # Several full blocks plus a partial trailing block.
    block_size = 256 * 1024  # minimum Drive accepts is a 256 KiB multiple
    data = b"abcd" * (block_size // 4 * 3 + 7)
    fn = _test_path("multiblock")
    with fs.open(fn, "wb", block_size=block_size) as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)

    assert fs.cat(fn) == data
    assert fs.info(fn)["size"] == len(data)


@pytest.mark.integration
def test_transaction_rollback_discards_upload(fs: GoogleDriveFileSystem) -> None:
    """A failed transaction cancels an in-progress resumable upload.

    This is the path that was previously broken: rollback calls
    ``GoogleDriveFile.discard()``, which issues the resumable-session DELETE.
    The upload must have actually started (a block flushed, so a session URI
    exists) for the cancel to do real work, hence the small block size and a
    payload larger than one block.
    """
    block_size = 256 * 1024
    data = b"z" * (block_size * 2)
    fn = _test_path("rolled_back")

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with fs.transaction:
            f = cast(
                GoogleDriveFile,
                fs.open(fn, "wb", block_size=block_size, autocommit=False),
            )
            try:
                f.write(data)  # flushes ≥1 block → opens the resumable session
                assert f.location is not None, "expected an open upload session"
                raise Boom("abort before commit")
            finally:
                f.closed = True

    # discard() clears location only after a successful DELETE/499, so this
    # proves the rollback actually cancelled the session (not merely that the
    # file was never committed, which a leaked session would also satisfy).
    assert f.location is None, "rollback should cancel the resumable session"
    # The aborted upload must not have produced a committed file.
    fs.invalidate_cache()
    assert not fs.exists(fn)


@pytest.mark.integration
def test_discard_after_partial_write(fs: GoogleDriveFileSystem) -> None:
    """Directly cancelling a started upload leaves no committed file.

    Exercises ``discard()`` end-to-end against the live API (DELETE on the
    session URI, accepting Google's 499) without relying on the transaction
    machinery.
    """
    block_size = 256 * 1024
    fn = _test_path("discarded")
    f = cast(
        GoogleDriveFile,
        fs.open(fn, "wb", block_size=block_size, autocommit=False),
    )
    try:
        f.write(b"q" * (block_size + 1))  # start the session
        assert f.location is not None
        f.discard()
        assert f.location is None  # cleared after a successful cancel
    finally:
        f.closed = True

    fs.invalidate_cache()
    assert not fs.exists(fn)


@pytest.mark.integration
@DIRCACHE_XFAIL
def test_ls_detail_includes_metadata(fs: GoogleDriveFileSystem) -> None:
    filename = _test_path("detail_check")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"meta")

    entries = fs.ls(TESTDIR, detail=True)
    match = [e for e in entries if e["name"] == filename]
    assert len(match) == 1
    assert match[0]["type"] == "file"
    assert match[0]["size"] == 4
    assert "id" in match[0]


@pytest.mark.integration
@DIRCACHE_XFAIL
def test_nested_ls_lists_children(fs: GoogleDriveFileSystem) -> None:
    parent = _test_path("nested_parent")
    fs.mkdir(parent)
    with fs.open(parent + "/child.txt", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"child")

    names = fs.ls(parent)
    assert parent + "/child.txt" in names


@pytest.mark.integration
def test_root_file_id_rejects_file(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A regular file ID must not be accepted as the filesystem root."""
    filename = _test_path("root_is_a_file")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"x")
    file_id = fs.info(filename)["id"]

    with pytest.raises(NotADirectoryError):
        make_fs(root_file_id=file_id)


@pytest.mark.integration
def test_root_file_id_accepts_folder(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A folder ID is a valid root and lists its children from ``ls("")``."""
    folder = _test_path("root_folder")
    fs.mkdir(folder)
    with fs.open(folder + "/child", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"data")
    folder_id = fs.info(folder)["id"]

    rooted = make_fs(root_file_id=folder_id)
    names = [item["name"] for item in rooted.ls("", detail=True)]
    assert "child" in names


@pytest.mark.integration
def test_root_file_id_scoped_to_shared_drive(
    fs: GoogleDriveFileSystem,
    make_fs: FsFactory,
    requires_shared_drive: None,
) -> None:
    """A folder inside the configured shared drive works as ``root_file_id``."""
    folder = _test_path("scoped_root")
    fs.mkdir(folder)
    folder_id = fs.info(folder)["id"]

    rooted = make_fs(root_file_id=folder_id)
    assert rooted.root_file_id == folder_id
    assert rooted.ls("") == []


@pytest.mark.integration
def test_shared_drive_root_lists(
    make_fs: FsFactory,
    requires_shared_drive: None,
) -> None:
    """Listing ``ls("")`` at the shared-drive root succeeds."""
    drive_fs = make_fs()
    listing = drive_fs.ls("")
    assert isinstance(listing, list)


@pytest.mark.integration
def test_info_returns_directory_for_root(fs: GoogleDriveFileSystem) -> None:
    info = fs.info("")
    assert info["type"] == "directory"
    assert info["size"] == 0
    assert info["id"] == fs.root_file_id
