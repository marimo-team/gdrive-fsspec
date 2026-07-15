# ---------------------------------------------------------------------------
# Integration tests (require live Google Drive credentials)
#
# Run: uv run pytest -v -m integration
# Parallel: uv run pytest -v -m integration -n auto --dist loadgroup
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

import time
from typing import Any, Callable, cast

import pytest
from conftest import TESTDIR, FsFactory
from googleapiclient.errors import HttpError

from gdrive_fsspec._constants import (
    _CHANGES_FIELDS,
    _CHANGES_PAGE_SIZE,
    _NUM_RETRIES,
    MultipleFilesError,
)
from gdrive_fsspec._file import GoogleDriveFile
from gdrive_fsspec.core import GoogleDriveFileSystem


def _test_path(name: str) -> str:
    return f"{TESTDIR}/{name}"


@pytest.mark.integration
def test_simple(fs: GoogleDriveFileSystem) -> None:
    assert fs.ls("")
    data = b"hello"
    filename = _test_path("testfile")
    with fs.open(filename, "wb") as f:
        f.write(data)
    assert fs.cat(filename) == data


@pytest.mark.integration
def test_overwrite_updates_in_place(fs: GoogleDriveFileSystem) -> None:
    """Writing to an existing path updates it instead of creating a duplicate."""
    filename = _test_path("overwrite")
    with fs.open(filename, "wb") as f:
        f.write(b"first")
    with fs.open(filename, "wb") as f:
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
        f.write(b"v1")
    original_id = fs.info(filename)["id"]

    with fs.open(filename, "wb") as f:
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
        f.write(b"one")
    # Populate the dircache for the parent.
    fs.ls(TESTDIR, detail=True)

    with fs.open(filename, "wb") as f:
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
        f.write(data)
    assert fs.cat(_test_path("data/bar/test")) == data


@pytest.mark.integration
def test_rm_file_trashes_by_default(fs: GoogleDriveFileSystem) -> None:
    # rm defaults to trashing: the file leaves the default listing but is
    # recoverable and still resolvable with trashed=True.
    filename = _test_path("to_delete")
    with fs.open(filename, "wb") as f:
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


@pytest.mark.integration
def test_cp_file_server_side_copy(fs: GoogleDriveFileSystem) -> None:
    # cp_file duplicates the content server-side into a new, independently
    # addressable file with its own id.
    src = _test_path("cp_src.txt")
    dst = _test_path("cp_dst.txt")
    data = b"server-side copy"
    with fs.open(src, "wb") as f:
        f.write(data)

    fs.cp_file(src, dst)

    assert fs.cat(dst) == data
    assert fs.cat(src) == data
    assert fs.info(src)["id"] != fs.info(dst)["id"]


@pytest.mark.integration
def test_cp_file_updates_live_dircache(fs: GoogleDriveFileSystem) -> None:
    # After a cached listing exists, cp_file records the new file so a cached
    # ls sees it without re-listing.
    src = _test_path("cp_cache_src.txt")
    dst = _test_path("cp_cache_dst.txt")
    with fs.open(src, "wb") as f:
        f.write(b"cache me")
    fs.ls(TESTDIR, detail=True)  # populate the parent's dircache

    fs.cp_file(src, dst)

    entries = fs.ls(TESTDIR, detail=True)  # served from cache
    match = [e for e in entries if e["name"] == dst]
    assert len(match) == 1
    assert match[0]["size"] == len(b"cache me")


@pytest.mark.integration
def test_copy_recursive_directory(fs: GoogleDriveFileSystem) -> None:
    # fsspec's recursive copy walks the tree and calls cp_file per entry;
    # directories are recreated and files copied server-side.
    src_dir = _test_path("cp_tree_src")
    dst_dir = _test_path("cp_tree_dst")
    fs.makedirs(f"{src_dir}/nested")  # writes require the parent dir to exist
    with fs.open(f"{src_dir}/a.txt", "wb") as f:
        f.write(b"a")
    with fs.open(f"{src_dir}/nested/b.txt", "wb") as f:
        f.write(b"b")

    fs.copy(src_dir, dst_dir, recursive=True)

    assert fs.cat(f"{dst_dir}/a.txt") == b"a"
    assert fs.cat(f"{dst_dir}/nested/b.txt") == b"b"


@pytest.mark.integration
def test_cp_file_overwrites_existing_destination(fs: GoogleDriveFileSystem) -> None:
    # Copying onto an existing file overwrites it (the old file is trashed), and
    # the destination path stays unique (no duplicate name is created).
    src = _test_path("cp_over_src.txt")
    dst = _test_path("cp_over_dst.txt")
    with fs.open(src, "wb") as f:
        f.write(b"new content")
    with fs.open(dst, "wb") as f:
        f.write(b"old content")
    old_id = fs.info(dst)["id"]

    fs.cp_file(src, dst)

    assert fs.cat(dst) == b"new content"
    # Overwrite replaced the file (new id) and did not leave a duplicate.
    assert fs.info(dst)["id"] != old_id
    entries = fs.ls(TESTDIR, detail=True)
    assert len([e for e in entries if e["name"] == dst]) == 1


@pytest.mark.integration
def test_cp_file_into_existing_directory(fs: GoogleDriveFileSystem) -> None:
    # A destination that is an existing directory means "copy into it" under the
    # source's basename, matching POSIX cp semantics.
    src = _test_path("cp_into_src.txt")
    dst_dir = _test_path("cp_into_dir")
    fs.makedirs(dst_dir)
    with fs.open(src, "wb") as f:
        f.write(b"into dir")

    fs.cp_file(src, dst_dir)

    assert fs.cat(f"{dst_dir}/cp_into_src.txt") == b"into dir"


@pytest.mark.integration
def test_copy_single_file_into_directory_via_public_api(
    fs: GoogleDriveFileSystem,
) -> None:
    # Through the public copy(): a single top-level file copied to an existing
    # directory lands inside it (fsspec's other_paths collapses the target to
    # the dir itself, which cp_file resolves as "copy into").
    src = _test_path("pubcp_src.txt")
    dst_dir = _test_path("pubcp_dir")
    fs.makedirs(dst_dir)
    with fs.open(src, "wb") as f:
        f.write(b"public copy into dir")

    fs.copy(src, dst_dir)

    assert fs.cat(f"{dst_dir}/pubcp_src.txt") == b"public copy into dir"


@pytest.mark.integration
def test_copy_recursive_overwrites_colliding_files(
    fs: GoogleDriveFileSystem,
) -> None:
    # A recursive copy whose destinations already exist must overwrite in place
    # and succeed, not abort partway on a name clash. dst_dir is created up front
    # so fsspec nests the tree under dst_dir/<src basename> on *both* runs, making
    # the second run collide with the first at identical target paths.
    src_dir = _test_path("recov_src")
    dst_dir = _test_path("recov_dst")
    nested_dst = f"{dst_dir}/recov_src"
    fs.makedirs(f"{src_dir}/nested")
    fs.makedirs(dst_dir)
    with fs.open(f"{src_dir}/a.txt", "wb") as f:
        f.write(b"v1")
    with fs.open(f"{src_dir}/nested/b.txt", "wb") as f:
        f.write(b"b")

    fs.copy(src_dir, dst_dir, recursive=True)
    assert fs.cat(f"{nested_dst}/a.txt") == b"v1"

    # Update a source file and copy again onto the now-populated destination.
    with fs.open(f"{src_dir}/a.txt", "wb") as f:
        f.write(b"v2-updated")
    fs.copy(src_dir, dst_dir, recursive=True)

    assert fs.cat(f"{nested_dst}/a.txt") == b"v2-updated"
    assert fs.cat(f"{nested_dst}/nested/b.txt") == b"b"
    # The overwrite did not leave a duplicate at the colliding path.
    entries = fs.ls(nested_dst, detail=True)
    assert len([e for e in entries if e["name"] == f"{nested_dst}/a.txt"]) == 1


@pytest.mark.integration
def test_mv_over_existing_destination(fs: GoogleDriveFileSystem) -> None:
    # A native mv onto an existing destination overwrites it (the old file is
    # trashed first) instead of failing on a name clash, leaving exactly one
    # live file at the destination and none at the source.
    src = _test_path("mv_over_src.txt")
    dst = _test_path("mv_over_dst.txt")
    with fs.open(src, "wb") as f:
        f.write(b"replacement")
    with fs.open(dst, "wb") as f:
        f.write(b"to be replaced")

    fs.mv(src, dst)

    assert fs.cat(dst) == b"replacement"
    assert not fs.exists(src)
    entries = fs.ls(TESTDIR, detail=True)
    assert len([e for e in entries if e["name"] == dst]) == 1


@pytest.mark.integration
def test_cp_file_creates_missing_destination_parent(
    fs: GoogleDriveFileSystem,
) -> None:
    # cp_file auto-creates the destination's parent directories, so a copy into
    # a not-yet-existing nested folder succeeds and the folders are materialized.
    src = _test_path("cp_parent_src.txt")
    dst = _test_path("cp_new_parent/deeper/cp_parent_dst.txt")
    data = b"needs new parents"
    with fs.open(src, "wb") as f:
        f.write(data)
    assert not fs.exists(_test_path("cp_new_parent"))

    fs.cp_file(src, dst)

    assert fs.isdir(_test_path("cp_new_parent"))
    assert fs.isdir(_test_path("cp_new_parent/deeper"))
    assert fs.cat(dst) == data


@pytest.mark.integration
def test_cp_file_preserves_larger_content(fs: GoogleDriveFileSystem) -> None:
    # A server-side copy must reproduce the source bytes exactly, not just short
    # inline payloads. Uses a multi-hundred-KB deterministic payload.
    src = _test_path("cp_large_src.bin")
    dst = _test_path("cp_large_dst.bin")
    # Deterministic, varied payload so a truncated/garbled copy would be caught.
    payload = bytes((i * 2654435761) & 0xFF for i in range(1_500_000))
    with fs.open(src, "wb") as f:
        f.write(payload)

    fs.cp_file(src, dst)

    copied = fs.cat(dst)
    assert len(copied) == len(payload)
    assert copied == payload
    assert fs.info(src)["id"] != fs.info(dst)["id"]


@pytest.mark.integration
def test_mv_preserves_file_id_and_moves_no_bytes(fs: GoogleDriveFileSystem) -> None:
    # Native mv is a single metadata update: the destination keeps the SAME file
    # id (no copy-then-delete), and the source path is gone without a trashed
    # duplicate left behind.
    src = _test_path("mv_src.txt")
    dst = _test_path("mv_dst.txt")
    data = b"move me in place"
    with fs.open(src, "wb") as f:
        f.write(data)
    original_id = fs.info(src)["id"]

    fs.mv(src, dst)

    assert fs.cat(dst) == data
    assert not fs.exists(src)
    # A metadata move relocates the existing resource, so the id is preserved.
    assert fs.info(dst)["id"] == original_id
    # The source path is not left as a trashed copy (nothing was duplicated).
    assert not fs.exists(src, trashed=True)


@pytest.mark.integration
def test_mv_rename_in_same_directory(fs: GoogleDriveFileSystem) -> None:
    # A same-parent rename changes only the name; the file id is preserved and
    # the parent listing shows the new name (and not the old).
    src = _test_path("mv_rename_before.txt")
    dst = _test_path("mv_rename_after.txt")
    data = b"rename me"
    with fs.open(src, "wb") as f:
        f.write(data)
    original_id = fs.info(src)["id"]

    fs.mv(src, dst)

    assert fs.info(dst)["id"] == original_id
    assert fs.cat(dst) == data
    names = fs.ls(TESTDIR)
    assert dst in names
    assert src not in names


@pytest.mark.integration
def test_mv_directory_moves_subtree(fs: GoogleDriveFileSystem) -> None:
    # Moving a directory relocates its whole subtree in a single metadata update
    # (recursive is irrelevant), preserving the folder id and its children.
    src_dir = _test_path("mv_dir_src")
    dst_dir = _test_path("mv_dir_dst")
    fs.makedirs(f"{src_dir}/nested")
    with fs.open(f"{src_dir}/a.txt", "wb") as f:
        f.write(b"a")
    with fs.open(f"{src_dir}/nested/b.txt", "wb") as f:
        f.write(b"b")
    original_dir_id = fs.info(src_dir)["id"]

    fs.mv(src_dir, dst_dir)

    assert not fs.exists(src_dir)
    assert fs.info(dst_dir)["id"] == original_dir_id
    assert fs.cat(f"{dst_dir}/a.txt") == b"a"
    assert fs.cat(f"{dst_dir}/nested/b.txt") == b"b"


@pytest.mark.integration
def test_mv_across_directories_preserves_id(fs: GoogleDriveFileSystem) -> None:
    # A cross-directory move re-parents the resource in place: same file id, gone
    # from the source directory, present in the destination directory.
    src_dir = _test_path("mv_x_src")
    dst_dir = _test_path("mv_x_dst")
    fs.makedirs(src_dir)
    fs.makedirs(dst_dir)
    src = f"{src_dir}/f.txt"
    dst = f"{dst_dir}/f.txt"
    with fs.open(src, "wb") as f:
        f.write(b"reparent me")
    original_id = fs.info(src)["id"]

    fs.mv(src, dst)

    assert fs.info(dst)["id"] == original_id
    assert fs.cat(dst) == b"reparent me"
    assert src not in fs.ls(src_dir)
    assert dst in fs.ls(dst_dir)


@pytest.mark.integration
def test_mv_into_existing_directory(fs: GoogleDriveFileSystem) -> None:
    # When the destination is an existing directory, the file is moved *into* it
    # under the source's basename (POSIX mv-into-directory semantics).
    src = _test_path("mv_into_src.txt")
    dst_dir = _test_path("mv_into_dir")
    fs.makedirs(dst_dir)
    with fs.open(src, "wb") as f:
        f.write(b"into the dir")
    original_id = fs.info(src)["id"]

    fs.mv(src, dst_dir)

    landed = f"{dst_dir}/mv_into_src.txt"
    assert fs.info(landed)["id"] == original_id
    assert fs.cat(landed) == b"into the dir"
    assert not fs.exists(src)


@pytest.mark.integration
def test_mv_into_own_parent_directory_is_noop(fs: GoogleDriveFileSystem) -> None:
    # Moving a file "into" the directory it already lives in resolves back to the
    # same resource and leaves it untouched.
    src_dir = _test_path("mv_noop_dir")
    fs.makedirs(src_dir)
    src = f"{src_dir}/f.txt"
    with fs.open(src, "wb") as f:
        f.write(b"stay put")
    original_id = fs.info(src)["id"]

    fs.mv(src, src_dir)

    assert fs.exists(src)
    assert fs.info(src)["id"] == original_id
    assert fs.cat(src) == b"stay put"


@pytest.mark.integration
def test_mv_directory_over_existing_file_raises(fs: GoogleDriveFileSystem) -> None:
    # A directory cannot overwrite an existing file at the destination.
    src_dir = _test_path("mv_conflict_dir")
    dst = _test_path("mv_conflict_dst.txt")
    fs.makedirs(src_dir)
    with fs.open(f"{src_dir}/inner.txt", "wb") as f:
        f.write(b"inner")
    with fs.open(dst, "wb") as f:
        f.write(b"occupied by a file")

    with pytest.raises(NotADirectoryError):
        fs.mv(src_dir, dst)

    # Both operands survive the refused move.
    assert fs.isdir(src_dir)
    assert fs.cat(dst) == b"occupied by a file"


@pytest.mark.integration
def test_mv_into_directory_with_same_named_subdir_raises(
    fs: GoogleDriveFileSystem,
) -> None:
    # Moving a file into a directory that already holds a subdirectory of the
    # source's name cannot overwrite that folder with a file.
    src = _test_path("mv_samesub_src.txt")
    dst_dir = _test_path("mv_samesub_dir")
    # dst_dir already contains a subdirectory named like the source file.
    fs.makedirs(f"{dst_dir}/mv_samesub_src.txt")
    with fs.open(src, "wb") as f:
        f.write(b"cannot clobber a folder")

    with pytest.raises(IsADirectoryError):
        fs.mv(src, dst_dir)

    # The source file is untouched by the refused move.
    assert fs.cat(src) == b"cannot clobber a folder"
    assert fs.isdir(f"{dst_dir}/mv_samesub_src.txt")


# ---------------------------------------------------------------------------
# Content-manager profile (drive-test): may trash but not permanently delete.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_content_manager_can_trash(content_manager_fs: GoogleDriveFileSystem) -> None:
    # Content managers have write + trash rights, so a normal rm() succeeds.
    filename = _test_path("cm_trash")
    with content_manager_fs.open(filename, "wb") as f:
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
            f.write(b"no quota here")


# ---------------------------------------------------------------------------
# Changes-API cache synchronization (opt-in changes_sync_interval)
#
# These drive a live Drive change feed, which is eventually consistent: an
# out-of-band mutation can take a second or two to appear, so assertions poll
# rather than reading once. The change feed is also ACCOUNT-WIDE (not scoped to
# TESTDIR), so concurrent tests' changes flow through it during a test's window.
#
# Two assertion styles handle that:
#   * Reconciliation tests (create/trash/delete/ttl) assert only that the target
#     file (dis)appears from its own dir after a sync. An unrelated change that
#     forces a full clear still yields the correct result, so these never fail
#     spuriously.
#   * Surgical/move tests must distinguish a surgical pop from a full clear, which
#     an unrelated full-clear-inducing change would corrupt. They instead capture
#     THIS test's real change from the feed (via ``_await_change_for``) and assert
#     ``_plan_invalidations`` on it — still end-to-end (real feed, real change
#     shape, real reducer) but immune to account-wide noise.
#
# Feed shapes below were confirmed live: trash keeps parents (removed=False,
# trashed=True), a move reports only the NEW parent, a hard delete arrives as
# removed=True with no file resource.
# ---------------------------------------------------------------------------

# Backoff schedule for eventually-consistent Drive change-feed polling. Shorter
# early intervals exit quickly when the feed is warm; later intervals preserve
# the ~12s worst-case budget of the original fixed 1s polling.
_SYNC_POLL_DELAYS_S = (0.0, 0.15, 0.25, 0.4, 0.5, 0.75, 1.0, 1.0, 1.5, 1.5, 2.0, 2.0)


def _until(predicate: Callable[[], bool]) -> bool:
    """Poll ``predicate`` with short-then-long sleeps between attempts."""
    for delay in _SYNC_POLL_DELAYS_S:
        if delay:
            time.sleep(delay)
        if predicate():
            return True
    return False


def _find_change_since(
    fs: GoogleDriveFileSystem, page_token: str, file_id: str
) -> dict[str, Any] | None:
    """Return the change for ``file_id`` since ``page_token``, if present.

    Pages through the change feed only until a matching entry is found,
    avoiding a full drain when the target change appears early.
    """
    scope = fs._changes_scope_kw()
    token: str | None = page_token
    while token is not None:
        response = (
            fs.service.changes()
            .list(
                pageToken=token,
                spaces=fs.spaces,
                includeRemoved=True,
                fields=_CHANGES_FIELDS,
                pageSize=_CHANGES_PAGE_SIZE,
                **scope,
            )
            .execute(num_retries=_NUM_RETRIES)
        )
        for change in response.get("changes", []):
            if change.get("fileId") == file_id:
                return cast("dict[str, Any]", change)
        token = response.get("nextPageToken")
    return None


def _await_change_for(
    fs: GoogleDriveFileSystem, start_token: str, file_id: str
) -> dict[str, Any]:
    """Poll the change feed from ``start_token`` until ``file_id``'s change appears.

    Returns that single change. Scans the whole (account-wide, eventually
    consistent) feed and picks out the one for ``file_id``, so unrelated
    concurrent changes are ignored. Each poll re-reads from the ORIGINAL
    ``start_token`` rather than advancing to the returned ``newStartPageToken``:
    the feed is eventually consistent, so advancing could move past a change
    that occurred before the token but only becomes visible on a later poll.
    """
    for delay in _SYNC_POLL_DELAYS_S:
        if delay:
            time.sleep(delay)
        change = _find_change_since(fs, start_token, file_id)
        if change is not None:
            return change
    raise AssertionError(f"no change for {file_id} appeared in the feed")


@pytest.mark.integration
def test_changes_sync_reconciles_out_of_band_create(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A file created out-of-band becomes visible after a changes sync.

    A ``watcher`` warms its cache and would otherwise treat the cached listing
    as authoritative (never seeing the external write). With ``_sync_cache``,
    the change is reconciled and the stale listing dropped, so the next ``ls``
    sees the new file. A second instance of the same identity performs the
    external write so it appears in the watcher's change feed. The ``fs``
    fixture is included so ``TESTDIR`` exists and is cleaned up.
    """
    watcher = make_fs(changes_sync_interval=0)  # sync on every cache read
    other = make_fs()

    subdir = _test_path("sync_dir")
    watcher.mkdir(subdir)
    # Baseline the changes token BEFORE the out-of-band write, then warm the
    # cache (the first sync only baselines and applies nothing).
    watcher._sync_cache()
    assert watcher.ls(subdir) == []

    # External create via a separate instance; invisible to the cached listing.
    new_file = f"{subdir}/external.txt"
    with other.open(new_file, "wb") as handle:
        handle.write(b"from another process")

    # Each ls triggers a sync (interval=0) that drops the stale listing and
    # re-lists once the create propagates into the feed.
    assert _until(lambda: new_file in watcher.ls(subdir))


@pytest.mark.integration
def test_changes_sync_surgical_invalidation_keeps_siblings(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A create in one cached dir drops only that dir's listing, not a sibling.

    Verifies the *surgical* path: the change maps via ``file.parents`` to the
    single affected directory. A regression to full-clear-everything would still
    surface the new file, so the real assertion is that the untouched sibling's
    cached listing survives the sync.
    """
    watcher = make_fs()  # sync driven manually below, not via a read interval
    other = make_fs()

    parent = _test_path("surgical")
    target = f"{parent}/target"
    sibling = f"{parent}/sibling"
    watcher.makedirs(target)
    watcher.makedirs(sibling)

    # Warm from the ROOT down so every cached directory's id is derivable (a
    # fully-mapped cache) — required for the surgical, non-full-clear path.
    watcher.ls("", detail=True)
    watcher.ls(TESTDIR, detail=True)
    watcher.ls(parent, detail=True)
    watcher.ls(target, detail=True)
    watcher.ls(sibling, detail=True)

    # Baseline the feed just before the write, so the only change we look for is
    # our own create.
    start_token = watcher._get_start_page_token()

    new_file = f"{target}/only_here.txt"
    with other.open(new_file, "wb") as handle:
        handle.write(b"surgical")
    file_id = other._path_to_id(new_file)

    # Assert the reducer's decision on the ACTUAL change from the live feed.
    # Testing the plan (rather than the post-sync cache state) keeps this a
    # genuine end-to-end test while staying immune to the account-wide change
    # feed, which also carries unrelated concurrent tests' changes.
    change = _await_change_for(watcher, start_token, file_id)
    assert change["file"]["parents"] == [watcher._path_to_id(target)]

    to_invalidate, full_clear = watcher._plan_invalidations(
        cast("list[Any]", [change]),
        watcher._build_dir_id_to_path(),
        watcher._build_id_to_paths(),
    )
    # Surgical: exactly the target directory is invalidated — no full clear, and
    # the sibling is untouched.
    assert full_clear is False
    assert to_invalidate == {target}


@pytest.mark.integration
def test_changes_sync_reconciles_out_of_band_trash(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """An out-of-band trash removes the file from a cached listing.

    Live feed shape: a trash arrives as ``removed=False, trashed=True`` with the
    file's parents intact, so it maps surgically to the parent directory.
    """
    watcher = make_fs(changes_sync_interval=0)
    other = make_fs()

    subdir = _test_path("trash_sync")
    doomed = f"{subdir}/doomed.txt"
    watcher.mkdir(subdir)
    with watcher.open(doomed, "wb") as handle:
        handle.write(b"bye")

    watcher._sync_cache()  # baseline
    assert doomed in watcher.ls(subdir)  # warm cache; file present

    other.rm(doomed)  # trash out-of-band (default rm)

    assert _until(lambda: doomed not in watcher.ls(subdir))


@pytest.mark.integration
def test_changes_sync_reconciles_out_of_band_hard_delete(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """An out-of-band permanent delete removes the file from a cached listing.

    Live feed shape: a hard delete arrives as ``removed=True`` with no file
    resource, so it is reconciled via the pre-sync ``id -> paths`` index (the
    dir that listed the now-deleted id is invalidated).
    """
    watcher = make_fs(changes_sync_interval=0)
    other = make_fs()

    subdir = _test_path("delete_sync")
    doomed = f"{subdir}/gone.txt"
    watcher.mkdir(subdir)
    with watcher.open(doomed, "wb") as handle:
        handle.write(b"poof")

    watcher._sync_cache()  # baseline
    assert doomed in watcher.ls(subdir)  # warm cache

    other.rm(doomed, permanent=True)  # hard delete out-of-band

    assert _until(lambda: doomed not in watcher.ls(subdir))


@pytest.mark.integration
def test_changes_sync_reconciles_out_of_band_move(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A move reconciles both the source and destination cached listings.

    Live feed shape: a move reports a single change carrying only the NEW
    parent. The source directory is invalidated via the pre-sync ``id -> paths``
    index; the destination via ``file.parents``. Uses the raw Drive update to
    reparent, since the filesystem has no ``mv`` yet.
    """
    watcher = make_fs()  # sync driven manually below
    other = make_fs()

    root = _test_path("move_sync")
    src = f"{root}/src"
    dst = f"{root}/dst"
    watcher.makedirs(src)
    watcher.makedirs(dst)
    mover = f"{src}/mover.txt"
    with watcher.open(mover, "wb") as handle:
        handle.write(b"move me")

    # Warm from the root down so src/dst ids are derivable (fully mapped).
    watcher.ls("", detail=True)
    watcher.ls(TESTDIR, detail=True)
    watcher.ls(root, detail=True)
    watcher.ls(src, detail=True)
    watcher.ls(dst, detail=True)
    assert mover in watcher.ls(src)
    assert watcher.ls(dst) == []

    file_id = watcher._path_to_id(mover)
    src_id = watcher._path_to_id(src)
    dst_id = watcher._path_to_id(dst)
    start_token = watcher._get_start_page_token()
    other.service.files().update(
        fileId=file_id,
        body={},
        addParents=dst_id,
        removeParents=src_id,
        supportsAllDrives=True,
    ).execute()

    # The reducer, given the real move change, invalidates BOTH the destination
    # (via the change's new parent) and the source (via the id -> paths index,
    # since the change carries only the new parent). Asserting the plan keeps
    # this immune to the account-wide feed's unrelated changes.
    change = _await_change_for(watcher, start_token, file_id)
    assert change["file"]["parents"] == [dst_id]  # only the NEW parent

    to_invalidate, full_clear = watcher._plan_invalidations(
        cast("list[Any]", [change]),
        watcher._build_dir_id_to_path(),
        watcher._build_id_to_paths(),
    )
    assert full_clear is False
    assert to_invalidate == {src, dst}


@pytest.mark.integration
def test_changes_sync_moved_cached_directory_drops_subtree(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """Moving a cached directory reconciles its own listing and descendants.

    Regression for the bug where only the parent listing was invalidated,
    leaving the moved directory's own cached listing (keyed by its now-stale
    path) and every descendant key serving stale data. Live feed shape (probed):
    a directory move reports one change carrying only the directory's NEW parent,
    so the source, the destination, and the moved dir's own subtree must all be
    dropped. Asserting ``_plan_invalidations`` on the real change keeps this
    immune to the account-wide feed's unrelated changes.
    """
    watcher = make_fs()  # sync driven manually below
    other = make_fs()

    root = _test_path("dir_move_sync")
    src = f"{root}/src"
    dst = f"{root}/dst"
    movable = f"{src}/movable"
    sub = f"{movable}/sub"
    watcher.makedirs(sub)
    watcher.makedirs(dst)
    with watcher.open(f"{sub}/leaf.txt", "wb") as handle:
        handle.write(b"leaf")

    # Warm from the root down so every dir id is derivable, including the
    # movable directory's OWN listing and its descendant ``sub``.
    for path in ("", TESTDIR, root, src, dst, movable, sub):
        watcher.ls(path, detail=True)
    assert movable in watcher.dircache
    assert sub in watcher.dircache

    movable_id = watcher._path_to_id(movable)
    src_id = watcher._path_to_id(src)
    dst_id = watcher._path_to_id(dst)
    start_token = watcher._get_start_page_token()
    other.service.files().update(
        fileId=movable_id,
        body={},
        addParents=dst_id,
        removeParents=src_id,
        supportsAllDrives=True,
    ).execute()

    change = _await_change_for(watcher, start_token, movable_id)
    assert change["file"]["parents"] == [dst_id]  # only the NEW parent

    to_invalidate, full_clear = watcher._plan_invalidations(
        cast("list[Any]", [change]),
        watcher._build_dir_id_to_path(),
        watcher._build_id_to_paths(),
    )
    assert full_clear is False
    # src (old location), dst (new location), and the moved dir's own listing
    # plus its descendant subtree must all be invalidated.
    assert {src, dst, movable, sub} <= to_invalidate


@pytest.mark.integration
def test_changes_sync_ttl_suppresses_polling(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """Within the TTL window an out-of-band create stays invisible; after it, visible.

    A generous interval means the second read is served from cache without a
    poll (stale), and only a read past the interval reconciles — proving the TTL
    gate actually suppresses syncs rather than polling every read.
    """
    watcher = make_fs(changes_sync_interval=3600)  # effectively "don't re-poll"
    other = make_fs()

    subdir = _test_path("ttl_sync")
    watcher.mkdir(subdir)
    watcher._sync_cache()  # baseline + stamp last-sync
    assert watcher.ls(subdir) == []  # warm; within TTL from here on

    new_file = f"{subdir}/late.txt"
    with other.open(new_file, "wb") as handle:
        handle.write(b"later")
    file_id = other.info(new_file)["id"]
    start_token = watcher._changes_page_token
    assert start_token is not None

    # Wait until the change is in the feed, then confirm the TTL keeps us from
    # seeing it (served from the warm cache, no poll).
    _await_change_for(other, start_token, file_id)
    assert watcher.ls(subdir) == []

    # Force the next read past the interval; now the sync runs and reconciles.
    watcher._last_sync_monotonic = None
    assert _until(lambda: new_file in watcher.ls(subdir))


@pytest.mark.integration
def test_changes_sync_renamed_cached_directory_drops_own_listing(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """Renaming a cached directory drops its own (now-stale) listing.

    Regression companion to the move case. Live feed shape (probed): a rename
    reports the directory's change with the new name and its unchanged parent,
    so the parent listing and the directory's own path key must both be
    invalidated. Asserting ``_plan_invalidations`` on the real change keeps this
    immune to the account-wide feed.
    """
    watcher = make_fs()  # sync driven manually below
    other = make_fs()

    root = _test_path("dir_rename_sync")
    movable = f"{root}/movable"
    watcher.makedirs(movable)
    with watcher.open(f"{movable}/leaf.txt", "wb") as handle:
        handle.write(b"leaf")

    # Warm from the root down so the movable dir's id is derivable (mapped).
    for path in ("", TESTDIR, root, movable):
        watcher.ls(path, detail=True)
    assert movable in watcher.dircache

    movable_id = watcher._path_to_id(movable)
    start_token = watcher._get_start_page_token()
    other.service.files().update(
        fileId=movable_id,
        body={"name": "renamed"},
        supportsAllDrives=True,
    ).execute()

    change = _await_change_for(watcher, start_token, movable_id)
    assert change["file"]["name"] == "renamed"

    to_invalidate, full_clear = watcher._plan_invalidations(
        cast("list[Any]", [change]),
        watcher._build_dir_id_to_path(),
        watcher._build_id_to_paths(),
    )
    assert full_clear is False
    # The parent listing (name changed) and the dir's own stale key both drop.
    assert {root, movable} <= to_invalidate


@pytest.mark.integration
def test_changes_sync_failure_serves_cache(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A failing sync must not break a cached read (C2).

    An invalid page token makes the next sync's ``changes.list`` fail; the
    read-side hook swallows it and serves the warm cache rather than raising.
    """
    watcher = make_fs(changes_sync_interval=0)  # sync on every read

    subdir = _test_path("sync_fail")
    watcher.mkdir(subdir)
    with watcher.open(f"{subdir}/keep.txt", "wb") as handle:
        handle.write(b"keep")
    keep = f"{subdir}/keep.txt"
    watcher.ls(subdir)  # warm the cache

    # Corrupt the page token so the next changes.list rejects it. Depending on
    # Drive this surfaces as an expired-token status (recovered internally) or a
    # generic error (swallowed by _maybe_sync_cache); either way ls must not
    # raise and must still serve the cached entry.
    watcher._changes_page_token = "totally-invalid-token"
    watcher._last_sync_monotonic = None

    names = watcher.ls(subdir)
    assert keep in names


@pytest.mark.integration
@pytest.mark.xfail(reason="dircache not updated correctly after mutations", strict=True)
def test_rm_recursive_deletes_directory_tree(fs: GoogleDriveFileSystem) -> None:
    root = _test_path("tree")
    fs.makedirs(root + "/a/b")
    with fs.open(root + "/a/b/leaf", "wb") as f:
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
        f.write(b"x")

    with pytest.raises(ValueError, match="non-empty"):
        fs.rmdir(path)


@pytest.mark.integration
def test_read_with_seek(fs: GoogleDriveFileSystem) -> None:
    data = b"0123456789abcdef"
    filename = _test_path("seekable")
    with fs.open(filename, "wb") as f:
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
        f.write(b"child")

    names = fs.ls(parent)
    assert parent + "/child.txt" in names


@pytest.mark.integration
def test_deep_cold_path_resolves(fs: GoogleDriveFileSystem, make_fs: FsFactory) -> None:
    """info/exists/cat resolve a deep path from a cold cache.

    Exercises the targeted per-component resolution (``_find_child_by_name``)
    end to end: a fresh filesystem with no cached listings must walk
    ``a/b/c/d/leaf.txt`` component by component against live Drive.
    """
    deep_dir = _test_path("deep/a/b/c/d")
    leaf = f"{deep_dir}/leaf.txt"
    fs.makedirs(deep_dir)
    with fs.open(leaf, "wb") as handle:
        handle.write(b"deep")

    # A second instance starts with an empty cache, so resolution is fully cold.
    cold = make_fs()
    assert cold.exists(leaf)
    info = cold.info(leaf)
    assert info["name"] == leaf
    assert info["type"] == "file"
    assert info["size"] == 4
    assert cold.cat(leaf) == b"deep"
    # A missing sibling under the same deep parent must raise, not hang.
    with pytest.raises(FileNotFoundError):
        cold.info(f"{deep_dir}/missing.txt")


@pytest.mark.integration
def test_duplicate_name_raises_multiple_files_error(
    fs: GoogleDriveFileSystem,
) -> None:
    """Two identically-named files in one folder surface MultipleFilesError.

    Drive permits duplicate names; ``_find_child_by_name`` must detect the
    ambiguity (via its exact-match count) rather than silently pick one. Uses
    raw ``files.create`` twice, since ``open(..., "wb")`` overwrites in place.
    """
    folder = _test_path("dupes")
    fs.mkdir(folder)
    parent_id = fs._path_to_id(folder)
    for _ in range(2):
        fs.files.create(
            body={"name": "dup.txt", "parents": [parent_id]},
            supportsAllDrives=True,
        ).execute()

    dup = f"{folder}/dup.txt"
    # Resolve without the listing cache so it goes through _find_child_by_name.
    fs.invalidate_cache()
    with pytest.raises(MultipleFilesError):
        fs.info(dup)


@pytest.mark.integration
def test_root_file_id_rejects_file(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A regular file ID must not be accepted as the filesystem root."""
    filename = _test_path("root_is_a_file")
    with fs.open(filename, "wb") as f:
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
        f.write(b"delete me")

    assert fs.exists(filename)
    fs.rm(filename)

    assert not fs.exists(filename)


# ---------------------------------------------------------------------------
# Google-native files (Docs/Sheets/Slides): read via automatic export (#25).
#
# open("rb")/cat/get cannot download these directly — get_media returns
# 403 fileNotDownloadable — so the file is materialized via files.export. They
# are created here by *importing* convertible content (text/CSV) under a
# Google-native target MIME type, since open("wb") only writes opaque binaries.
# ---------------------------------------------------------------------------

_DOC_MIME = "application/vnd.google-apps.document"
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _import_native(
    fs: GoogleDriveFileSystem,
    name: str,
    target_mime: str,
    content: bytes,
    source_mime: str,
) -> str:
    """Create a Google-native file by importing convertible content.

    Uploads ``content`` with a Google-native ``target_mime`` so Drive converts
    it into a real Doc/Sheet/etc. Returns the new file's id; the file lands in
    ``TESTDIR`` (cleaned up by the fixture) and the cache is invalidated so it
    resolves by path afterwards.
    """
    from googleapiclient.http import MediaInMemoryUpload

    parent_id = fs._path_to_id(TESTDIR)
    media = MediaInMemoryUpload(content, mimetype=source_mime, resumable=False)
    created = fs.files.create(
        body={"name": name, "mimeType": target_mime, "parents": [parent_id]},
        media_body=media,
        supportsAllDrives=True,
        fields="id",
    ).execute()
    fs.invalidate_cache()
    return created["id"]


@pytest.mark.integration
def test_native_doc_reads_via_export(fs: GoogleDriveFileSystem) -> None:
    """A Google Doc reads through open()/cat via an automatic text export."""
    _import_native(
        fs, "note", _DOC_MIME, b"Hello native export integration", "text/plain"
    )
    path = _test_path("note")

    assert fs.info(path)["mimeType"] == _DOC_MIME
    # Default export for a Doc is text/plain; the imported text round-trips
    # (allowing for an added BOM/trailing newline, hence a substring check).
    with fs.open(path, "rb") as handle:
        data = handle.read()
    assert b"Hello native export integration" in data
    assert handle.size == len(data)
    # cat goes through the same read path.
    assert b"Hello native export integration" in fs.cat(path)


@pytest.mark.integration
def test_native_doc_export_pdf(fs: GoogleDriveFileSystem) -> None:
    """A Doc exports to PDF via both fs.export() and the open() kwarg."""
    _import_native(fs, "pdf_doc", _DOC_MIME, b"pdf me", "text/plain")
    path = _test_path("pdf_doc")

    pdf = fs.export(path, "application/pdf")
    assert pdf.startswith(b"%PDF")

    with fs.open(path, "rb", export_mime_type="application/pdf") as handle:
        assert handle.read().startswith(b"%PDF")


@pytest.mark.integration
def test_native_sheet_defaults_to_xlsx(fs: GoogleDriveFileSystem) -> None:
    """A Sheet defaults to XLSX (not CSV, which would drop all but sheet 1)."""
    _import_native(fs, "grid", _SHEET_MIME, b"a,b,c\n1,2,3\n", "text/csv")
    path = _test_path("grid")

    with fs.open(path, "rb") as handle:
        data = handle.read()
    # XLSX is a zip container ("PK" magic); CSV would be plain text.
    assert data[:2] == b"PK"
    assert handle.size == len(data)


@pytest.mark.integration
def test_native_unsupported_export_mime_raises(fs: GoogleDriveFileSystem) -> None:
    """Requesting an export target Drive does not offer raises a clear error."""
    _import_native(fs, "bad_export", _DOC_MIME, b"x", "text/plain")
    path = _test_path("bad_export")

    with pytest.raises(ValueError, match="Supported export types"):
        fs.open(path, "rb", export_mime_type="image/png")
