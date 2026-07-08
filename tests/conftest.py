import os
from dataclasses import dataclass
from typing import Any, Callable, Generator, NamedTuple
from unittest import mock

import pytest

from gdrive_fsspec import GoogleDriveFileSystem
from gdrive_fsspec.core import AuthMethod

TESTDIR = "gdrive_fsspec_testdir"

FsFactory = Callable[..., GoogleDriveFileSystem]


def empty_headers() -> dict[str, str]:
    return {}


def empty_listing() -> list[dict[str, Any]]:
    return []


def empty_files_list_response() -> dict[str, Any]:
    files: list[Any] = []
    return {"files": files}


class MockedDriveFS(NamedTuple):
    fs: GoogleDriveFileSystem
    files: mock.Mock
    service: mock.Mock
    authed_http: mock.Mock


@pytest.fixture()
def mocked_fs(anon_fs: GoogleDriveFileSystem) -> MockedDriveFS:
    files = mock.Mock()
    service = mock.Mock()
    authed_http = mock.Mock()
    # Raw resumable-upload calls go through fs.authed_http.request; the
    # discovery client's files()._http points at the same transport so tests
    # can assert on either handle interchangeably.
    files._http = authed_http
    anon_fs.files = files
    anon_fs.service = service
    anon_fs.authed_http = authed_http
    return MockedDriveFS(anon_fs, files, service, authed_http)


@pytest.fixture()
def anon_fs() -> GoogleDriveFileSystem:
    # skip_instance_cache keeps each test's dircache isolated; fsspec otherwise
    # returns the same cached instance for identical constructor arguments.
    return GoogleDriveFileSystem(token="anon", skip_instance_cache=True)


# ---------------------------------------------------------------------------
# Integration-test drive profiles
#
# A single service-account key backs every ``service_account`` profile; each
# profile just points at a different target (a shared drive with a specific
# role, or the account's own My Drive).
#
# Whether a service-account key is configured picks the identity for the default
# ``fs``/``make_fs`` suite: when ``GDRIVE_FSSPEC_CREDENTIALS_PATH`` is set the
# suite runs as the full-access service account; when it is unset the suite runs
# as a user via OAuth (the ``user`` profile), logging in through the browser once
# per session and reusing the cached token thereafter. See the header of
# ``tests/test_integration.py`` for the full environment-variable reference.
# ---------------------------------------------------------------------------

# Profile identifiers.
CONTENT_MANAGER = "content_manager"
READONLY = "readonly"
FULL_ACCESS = "full_access"
SA_MY_DRIVE = "sa_my_drive"
USER = "user"

# Shared service-account key (path to JSON or the JSON string itself).
_CREDS_ENV = "GDRIVE_FSSPEC_CREDENTIALS_PATH"


@dataclass(frozen=True)
class DriveProfile:
    """A named credential/target combination for live integration tests.

    The ``writable``/``can_trash``/``can_permanent_delete`` flags describe the
    access level the profile is expected to have; fixtures use them to pick a
    safe teardown strategy and tests use them to assert permission boundaries.
    """

    id: str
    token: AuthMethod
    creds_env: str | None
    drive_env: str | None
    writable: bool
    can_trash: bool
    can_permanent_delete: bool
    description: str


PROFILES: dict[str, DriveProfile] = {
    CONTENT_MANAGER: DriveProfile(
        id=CONTENT_MANAGER,
        token="service_account",
        creds_env=_CREDS_ENV,
        drive_env="GDRIVE_FSSPEC_DRIVE_CONTENT_MANAGER",
        writable=True,
        can_trash=True,
        can_permanent_delete=False,
        description="Service account with Content manager access on a shared drive.",
    ),
    READONLY: DriveProfile(
        id=READONLY,
        token="service_account",
        creds_env=_CREDS_ENV,
        drive_env="GDRIVE_FSSPEC_DRIVE_READONLY",
        writable=False,
        can_trash=False,
        can_permanent_delete=False,
        description="Service account with Viewer access on a shared drive.",
    ),
    FULL_ACCESS: DriveProfile(
        id=FULL_ACCESS,
        token="service_account",
        creds_env=_CREDS_ENV,
        drive_env="GDRIVE_FSSPEC_DRIVE_FULL_ACCESS",
        writable=True,
        can_trash=True,
        can_permanent_delete=True,
        description="Service account with full (Manager) access on a shared drive.",
    ),
    SA_MY_DRIVE: DriveProfile(
        id=SA_MY_DRIVE,
        token="service_account",
        creds_env=_CREDS_ENV,
        drive_env=None,
        writable=False,
        can_trash=False,
        can_permanent_delete=False,
        description="Service account without a shared drive (its own quota-less My Drive).",
    ),
    USER: DriveProfile(
        id=USER,
        token="cache",
        creds_env=None,
        drive_env=None,
        writable=True,
        can_trash=True,
        can_permanent_delete=True,
        description=(
            "User OAuth identity (My Drive). Uses a cached token; a one-time "
            "browser login populates the cache when it is missing."
        ),
    ),
}


def _creds_value(profile: DriveProfile) -> str | None:
    return os.getenv(profile.creds_env) if profile.creds_env else None


def _drive_value(profile: DriveProfile) -> str | None:
    if profile.drive_env is None:
        return None
    return os.getenv(profile.drive_env)


def _service_account_configured() -> bool:
    return bool((os.getenv(_CREDS_ENV) or "").strip())


def _default_profile_id() -> str:
    """Pick the identity for the default ``fs``/``make_fs`` suite.

    A configured service-account key selects full access; otherwise the suite
    runs as a user via OAuth so the same CRUD tests can be exercised locally
    without a service account.
    """
    return FULL_ACCESS if _service_account_configured() else USER


def _oauth_cache_available() -> bool:
    """True when a cached user OAuth token exists (so no browser prompt fires)."""
    try:
        import pydata_google_auth.cache

        path = pydata_google_auth.cache.READ_WRITE._path
    except Exception:
        return False
    return bool(path) and os.path.exists(path)


# Guard so the interactive browser login runs at most once per test session.
_user_login_done = False


def _force_browser_login() -> bool:
    """True when the user asked to re-run the browser login (ignoring any cache)."""
    return bool((os.getenv("GDRIVE_FSSPEC_FORCE_BROWSER") or "").strip())


def _ensure_user_login() -> None:
    """Make sure a cached user token exists, logging in via browser once.

    ``token="cache"`` would itself launch the browser flow on a cache miss, but
    it would do so for *every* filesystem the suite builds. Instead we perform a
    single ``token="browser"`` login up front and let every later instance reuse
    the cache. Never triggers in CI (guarded by the caller).

    Set ``GDRIVE_FSSPEC_FORCE_BROWSER=1`` to force a fresh browser login even
    when a cached token already exists (``token="browser"`` clears the cache and
    re-authenticates). It still happens at most once per session.
    """
    global _user_login_done
    if _user_login_done:
        return
    if _oauth_cache_available() and not _force_browser_login():
        _user_login_done = True
        return
    # Populate the cache with one interactive login for the whole session.
    GoogleDriveFileSystem(token="browser", skip_instance_cache=True)
    _user_login_done = True


def _require_profile(profile_id: str) -> DriveProfile:
    """Return the profile, skipping the test when it is not configured."""
    profile = PROFILES[profile_id]
    if profile.creds_env is not None:
        creds = _creds_value(profile)
        if not creds or not creds.strip():
            pytest.skip(f"{profile.creds_env} not set (profile {profile_id!r})")
    if profile.drive_env is not None:
        drive = _drive_value(profile)
        if not drive or not drive.strip():
            pytest.skip(f"{profile.drive_env} not set (profile {profile_id!r})")
    if profile.token in ("cache", "browser"):
        # The browser login is interactive; never attempt it in CI.
        if os.getenv("CI"):
            pytest.skip(f"cannot run interactive OAuth profile {profile_id!r} in CI")
        _ensure_user_login()
    return profile


def _build_fs(profile: DriveProfile, **overrides: Any) -> GoogleDriveFileSystem:
    return GoogleDriveFileSystem(
        skip_instance_cache=True,
        token=profile.token,
        creds=_creds_value(profile),
        drive=_drive_value(profile),
        **overrides,
    )


def _remove_testdir(instance: GoogleDriveFileSystem) -> None:
    """Best-effort delete of ``TESTDIR``, falling back to trash when needed.

    Managers can permanently delete; content managers can only trash. Try the
    stronger operation first and fall back so cleanup works for either role.
    """
    try:
        if not instance.exists(TESTDIR):
            return
    except (OSError, PermissionError):
        return
    for permanent in (True, False):
        try:
            instance.rm(TESTDIR, recursive=True, permanent=permanent)
            return
        except (OSError, PermissionError):
            continue


def _reset_testdir(instance: GoogleDriveFileSystem) -> None:
    """Give ``instance`` a fresh, empty ``TESTDIR``."""
    _remove_testdir(instance)
    instance.mkdir(TESTDIR, create_parents=True)


ProfiledFactory = Callable[[str], FsFactory]


@pytest.fixture()
def fs_factory() -> Generator[ProfiledFactory, None, None]:
    """Build live filesystems for any profile, cleaning up ``TESTDIR`` afterwards.

    ``fs_factory(profile_id)`` returns a factory that constructs as many
    instances of that profile as a test needs (e.g. a second one rooted at a
    different ``root_file_id``). Every instance created through this fixture has
    its ``TESTDIR`` removed during teardown.
    """
    created: list[GoogleDriveFileSystem] = []

    def _for(profile_id: str) -> FsFactory:
        profile = _require_profile(profile_id)

        def _make(**overrides: Any) -> GoogleDriveFileSystem:
            instance = _build_fs(profile, **overrides)
            created.append(instance)
            return instance

        return _make

    yield _for

    for instance in created:
        _remove_testdir(instance)


@pytest.fixture()
def make_fs(fs_factory: ProfiledFactory) -> FsFactory:
    """Factory for the default identity's filesystems.

    Full-access service account when ``GDRIVE_FSSPEC_CREDENTIALS_PATH`` is set,
    otherwise the OAuth user identity, so the same CRUD suite runs either way.
    """
    return fs_factory(_default_profile_id())


@pytest.fixture()
def fs(make_fs: FsFactory) -> GoogleDriveFileSystem:
    """A single default-identity filesystem with a fresh ``TESTDIR`` already created."""
    instance = make_fs()
    _reset_testdir(instance)
    return instance


@pytest.fixture()
def content_manager_fs(fs_factory: ProfiledFactory) -> GoogleDriveFileSystem:
    """A content-manager filesystem with a fresh ``TESTDIR`` (can trash, not delete)."""
    instance = fs_factory(CONTENT_MANAGER)()
    _reset_testdir(instance)
    return instance


@pytest.fixture()
def readonly_fs(fs_factory: ProfiledFactory) -> GoogleDriveFileSystem:
    """A viewer-only filesystem (list/read succeed; every mutation is denied)."""
    return fs_factory(READONLY)()


@pytest.fixture()
def sa_my_drive_fs(fs_factory: ProfiledFactory) -> GoogleDriveFileSystem:
    """A service-account filesystem with no shared drive (quota-less My Drive)."""
    return fs_factory(SA_MY_DRIVE)()


@pytest.fixture()
def requires_shared_drive() -> None:
    """Skip when no full-access shared drive is configured."""
    if not _drive_value(PROFILES[FULL_ACCESS]):
        pytest.skip("full-access shared drive not configured")
