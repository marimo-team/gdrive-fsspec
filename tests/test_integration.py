# ---------------------------------------------------------------------------
# Integration tests (require live Google Drive credentials)
#
# Run: uv run pytest -v -m integration
#
# Each profile targets a different drive and has different roles.
# Any profile whose variables are unset is skipped, so you can run a subset locally.
#
#   Service-account key (shared by every service-account profile):
#     GDRIVE_FSSPEC_CREDENTIALS_PATH=/path/to/sa.json   # path or JSON string
#
#   Per-profile shared-drive targets (name or ID):
#     GDRIVE_FSSPEC_DRIVE_FULL_ACCESS=drive-full-access     # Manager role
#     GDRIVE_FSSPEC_DRIVE_CONTENT_MANAGER=drive-test        # Content-manager role
#     GDRIVE_FSSPEC_DRIVE_READONLY=drive-readonly           # Viewer role
#
#   sa_my_drive profile:  same key, no shared drive (the SA's own My Drive).
#
# The default ``fs``/``make_fs`` suite runs as the full-access service account
# when GDRIVE_FSSPEC_CREDENTIALS_PATH is set. With it unset, the same suite runs
# as a user via OAuth: a one-time browser login populates the cache and every
# later filesystem reuses it. That OAuth path is skipped in CI (which always has
# the service-account key). Shared-drive-only tests skip under OAuth (My Drive
# has no shared drive).
# ---------------------------------------------------------------------------

from typing import cast

import pytest
from conftest import TESTDIR, FsFactory
from googleapiclient.errors import HttpError

from gdrive_fsspec.core import GoogleDriveFile, GoogleDriveFileSystem


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
    original_id = fs.info(filename)["id"]

    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"v2")

    assert fs.info(filename)["id"] == original_id
    assert fs.cat(filename) == b"v2"


@pytest.mark.integration
def test_overwrite_updates_live_dircache(fs: GoogleDriveFileSystem) -> None:
    """The live dircache reflects an overwrite without re-listing.

    Exercises the in-place dircache update on commit: ``ls`` populates the
    cache, the overwrite must replace that entry (not append a second one).
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
def test_rm_file_trashes_by_default(fs: GoogleDriveFileSystem) -> None:
    # rm defaults to trashing: the file leaves the default listing but is
    # recoverable and still resolvable with trashed=True.
    filename = _test_path("to_delete")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"gone soon")

    assert fs.exists(filename)
    fs.rm(filename)
    assert not fs.exists(filename)
    # Trashed, not purged: it is still findable when trashed items are included.
    assert fs.info(filename, trashed=True)["trashed"] is True


@pytest.mark.integration
def test_rm_permanent_removes_file(fs: GoogleDriveFileSystem) -> None:
    # permanent=True hard-deletes: the file is gone even from trashed listings.
    filename = _test_path("to_purge")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"gone for good")

    assert fs.exists(filename)
    fs.rm(filename, permanent=True)
    assert not fs.exists(filename)
    with pytest.raises(FileNotFoundError):
        fs.info(filename, trashed=True)


@pytest.mark.integration
def test_rm_missing_file_raises_file_not_found(
    fs: GoogleDriveFileSystem,
) -> None:
    # Deleting a file that does not exist should surface FileNotFoundError, not a
    # raw HttpError. rm resolves the file via info() first, which raises when the
    # path is absent.
    with pytest.raises(FileNotFoundError):
        fs.rm(_test_path("never_existed"))


# ---------------------------------------------------------------------------
# Content-manager profile (drive-test): may trash but not permanently delete.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_content_manager_can_trash(content_manager_fs: GoogleDriveFileSystem) -> None:
    # Content managers have write + trash rights, so a normal rm() succeeds.
    filename = _test_path("cm_trash")
    with content_manager_fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"trash me")

    assert content_manager_fs.exists(filename)
    content_manager_fs.rm(filename)
    assert not content_manager_fs.exists(filename)


@pytest.mark.integration
def test_content_manager_permanent_delete_raises_permission_error(
    content_manager_fs: GoogleDriveFileSystem,
) -> None:
    # permanent=True checks capabilities.canDelete and raises an actionable
    # PermissionError (mentioning Manager access) instead of a masked 404, since
    # a content manager lacks the Manager role required to hard-delete.
    filename = _test_path("cm_forbidden_delete")
    with content_manager_fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"cannot delete me")

    assert content_manager_fs.exists(filename)
    with pytest.raises(PermissionError, match="Manager"):
        content_manager_fs.rm(filename, permanent=True)


# ---------------------------------------------------------------------------
# Read-only profile (drive-readonly): reads succeed, every mutation is denied.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_readonly_lists_root(readonly_fs: GoogleDriveFileSystem) -> None:
    listing = readonly_fs.ls("", detail=True)
    assert isinstance(listing, list)


@pytest.mark.integration
def test_readonly_write_is_denied(readonly_fs: GoogleDriveFileSystem) -> None:
    # Uploads run the resumable-session initiation, which surfaces the 403 as an
    # OSError (IOError) rather than a raw HttpError.
    with pytest.raises(OSError):
        with readonly_fs.open("gdrive_fsspec_readonly_probe", "wb") as f:
            # pyrefly: ignore [bad-argument-type]
            f.write(b"nope")


@pytest.mark.integration
def test_readonly_mkdir_is_denied(readonly_fs: GoogleDriveFileSystem) -> None:
    # Folder creation goes through the discovery client, so the 403 arrives as
    # an HttpError.
    with pytest.raises(HttpError):
        readonly_fs.mkdir("gdrive_fsspec_readonly_dir")


@pytest.mark.integration
def test_readonly_trash_is_denied_for_existing_file(
    readonly_fs: GoogleDriveFileSystem,
) -> None:
    # A viewer can see files but not trash them. This needs a pre-seeded file in
    # the read-only drive (the viewer cannot create one); skip when empty.
    entries = readonly_fs.ls("", detail=True)
    files = [e for e in entries if e["type"] == "file"]
    if not files:
        pytest.skip("drive-readonly has no seed file to attempt trashing")
    target = files[0]["name"]
    with pytest.raises(PermissionError, match="Trash"):
        readonly_fs.rm(target)


# ---------------------------------------------------------------------------
# Service account without a shared drive: its own My Drive has no storage quota.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sa_my_drive_lists_root(sa_my_drive_fs: GoogleDriveFileSystem) -> None:
    listing = sa_my_drive_fs.ls("", detail=True)
    assert isinstance(listing, list)


@pytest.mark.integration
def test_sa_my_drive_upload_exceeds_quota(
    sa_my_drive_fs: GoogleDriveFileSystem,
) -> None:
    # Service accounts cannot own files in their own My Drive (no quota), so an
    # upload must fail rather than silently succeed.
    with pytest.raises(OSError):
        with sa_my_drive_fs.open("gdrive_fsspec_sa_probe", "wb") as f:
            # pyrefly: ignore [bad-argument-type]
            f.write(b"no quota here")


@pytest.mark.integration
@pytest.mark.xfail(reason="dircache not updated correctly after mutations", strict=True)
def test_rm_recursive_deletes_directory_tree(fs: GoogleDriveFileSystem) -> None:
    root = _test_path("tree")
    fs.makedirs(root + "/a/b")
    with fs.open(root + "/a/b/leaf", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"leaf")

    fs.rm(root, recursive=True)

    # Recursive delete only pops the root listing from dircache; nested
    # listings populated during setup may linger until invalidated.
    # fs.invalidate_cache()

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

    assert not fs.exists(fn)


@pytest.mark.integration
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


@pytest.mark.integration
def test_info_honors_fields_with_warm_listing_cache(
    fs: GoogleDriveFileSystem,
) -> None:
    """``info(fields=...)`` must fetch from the API, not stale ``ls`` dircache."""
    filename = _test_path("info_fields")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"x")

    fs.ls(TESTDIR, detail=True)
    cached = next(e for e in fs.dircache[TESTDIR] if e["name"] == filename)
    assert "capabilities" not in cached

    info = fs.info(filename, fields="capabilities/canDelete")
    assert info.get("capabilities", {}).get("canDelete") is True


@pytest.mark.integration
def test_ls_with_fields_bypasses_stale_cache(fs: GoogleDriveFileSystem) -> None:
    """``ls(..., fields=...)`` must refetch when the canonical cache lacks fields."""
    filename = _test_path("ls_fields")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"abc")

    fs.ls(TESTDIR, detail=True)
    cached = next(e for e in fs.dircache[TESTDIR] if e["name"] == filename)
    assert "capabilities" not in cached

    entries = fs.ls(TESTDIR, detail=True, fields="capabilities/canDelete")
    match = [e for e in entries if e["name"] == filename]
    assert len(match) == 1
    capabilities = match[0]["capabilities"]
    assert isinstance(capabilities, dict)
    assert capabilities["canDelete"] is True


@pytest.mark.integration
def test_ls_with_fields_does_not_overwrite_canonical_cache(
    fs: GoogleDriveFileSystem,
) -> None:
    """Non-canonical ``ls`` reads fresh data but leaves the default dircache unchanged."""
    filename = _test_path("ls_no_cache_pollution")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"x")

    fs.ls(TESTDIR, detail=True)
    before = next(e for e in fs.dircache[TESTDIR] if e["name"] == filename)

    fs.ls(TESTDIR, detail=True, fields="capabilities/canDelete")

    after = next(e for e in fs.dircache[TESTDIR] if e["name"] == filename)
    assert after == before
    assert "capabilities" not in after


@pytest.mark.integration
def test_rm_succeeds_after_exists_warmed_cache(fs: GoogleDriveFileSystem) -> None:
    """Regression: ``exists()`` warming dircache must not cause a false ``PermissionError`` on ``rm``."""
    filename = _test_path("rm_after_exists")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"delete me")

    assert fs.exists(filename)
    fs.rm(filename)

    assert not fs.exists(filename)
