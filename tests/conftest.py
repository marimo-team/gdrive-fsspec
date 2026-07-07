import os
from dataclasses import dataclass
from typing import Any, Callable, Generator, NamedTuple, cast, get_args
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


@dataclass(frozen=True)
class DriveConfig:
    """Credentials/target for a live ``GoogleDriveFileSystem``, read from env."""

    token: AuthMethod
    creds: str | None
    drive: str | None

    @classmethod
    def from_env(cls) -> "DriveConfig":
        token = os.getenv("GDRIVE_FSSPEC_CREDENTIALS_TYPE", "service_account")
        if token not in get_args(AuthMethod):
            raise ValueError(f"Invalid token: {token}")
        return cls(
            token=cast(AuthMethod, token),
            creds=os.getenv("GDRIVE_FSSPEC_CREDENTIALS_PATH"),
            drive=os.getenv("GDRIVE_FSSPEC_DRIVE"),
        )

    @property
    def configured(self) -> bool:
        if self.token == "service_account":
            return bool(self.creds and self.creds.strip())
        return True

    def build(self, **overrides: Any) -> GoogleDriveFileSystem:
        return GoogleDriveFileSystem(
            skip_instance_cache=True,
            token=self.token,
            creds=self.creds,
            drive=self.drive,
            **overrides,
        )


@pytest.fixture()
def requires_shared_drive() -> None:
    """Skip when ``GDRIVE_FSSPEC_DRIVE`` is not set (shared-drive-only scenarios)."""
    if not os.getenv("GDRIVE_FSSPEC_DRIVE"):
        pytest.skip("GDRIVE_FSSPEC_DRIVE not set")


@pytest.fixture()
def make_fs() -> Generator[FsFactory, None, None]:
    """Factory for live filesystems sharing one credential set and teardown.

    Tests can build as many instances as they need (e.g. a second one rooted at
    a different ``root_file_id``); ``TESTDIR`` is cleaned up once afterwards.
    """
    config = DriveConfig.from_env()
    if not config.configured:
        # Only service-account auth can be unconfigured (missing creds path);
        # other token types are always considered configured.
        pytest.skip("GDRIVE_FSSPEC_CREDENTIALS_PATH not set")

    created: list[GoogleDriveFileSystem] = []

    def _make(**overrides: Any) -> GoogleDriveFileSystem:
        fs = config.build(**overrides)
        created.append(fs)
        return fs

    yield _make

    if created:
        primary = created[0]
        if primary.exists(TESTDIR):
            try:
                primary.rm(TESTDIR, recursive=True, permanent=True)
            except IOError:
                pass


@pytest.fixture()
def fs(make_fs: FsFactory) -> GoogleDriveFileSystem:
    """A single live filesystem with a fresh ``TESTDIR`` already created."""
    instance = make_fs()
    if instance.exists(TESTDIR):
        instance.rm(TESTDIR, recursive=True, permanent=True)
    instance.mkdir(TESTDIR, create_parents=True)
    return instance


@pytest.fixture()
def readonly_fs() -> GoogleDriveFileSystem:
    """A live filesystem authenticated as a read-only (viewer) identity.

    Used to exercise permission-denied paths: it can list files created by the
    privileged ``fs`` fixture but cannot delete them. Requires a separate
    service-account key with viewer-only access to ``GDRIVE_FSSPEC_DRIVE``;
    skips when that key is not configured.
    """
    creds = os.getenv("GDRIVE_FSSPEC_READONLY_CREDENTIALS_PATH")
    if not creds or not creds.strip():
        pytest.skip("GDRIVE_FSSPEC_READONLY_CREDENTIALS_PATH not set")
    drive = os.getenv("GDRIVE_FSSPEC_DRIVE")
    if not drive:
        # The permission-denied scenario is shared-drive-specific.
        pytest.skip("GDRIVE_FSSPEC_DRIVE not set")
    return GoogleDriveFileSystem(
        skip_instance_cache=True,
        token="service_account",
        creds=creds,
        drive=drive,
    )
