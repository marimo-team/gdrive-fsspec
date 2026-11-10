"""Public web access uses HTTP responses, never an authenticated Drive resource."""

from __future__ import annotations

from collections.abc import Generator
from io import BytesIO
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from conftest import make_api_fs
from urllib3.response import HTTPResponse

from gdrive_fsspec import GoogleDriveFileSystem
from gdrive_fsspec._constants import MultipleFilesError
from gdrive_fsspec._public import PublicDriveClient, PublicReference


def response(
    body: bytes = b"",
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    url: str = "https://drive.usercontent.google.com/download",
) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = url
    result.headers.update(headers or {})
    result.raw = HTTPResponse(body=BytesIO(body), preload_content=False)
    return result


def folder(*links: tuple[str, str]) -> requests.Response:
    entries = "".join(
        f'<div class="flip-entry"><a href="{url}">{name}</a></div>'
        for name, url in links
    )
    return response(f'<div id="flip-contents">{entries}</div>'.encode())


def attachment(body: bytes, *, status: int = 200, **headers: str) -> requests.Response:
    return response(
        body,
        status=status,
        headers={"Content-Disposition": 'attachment; filename="file"', **headers},
    )


def partial(body: bytes, start: int, end: int, total: int) -> requests.Response:
    return attachment(
        body, status=206, **{"Content-Range": f"bytes {start}-{end}/{total}"}
    )


@pytest.fixture()
def public_fs() -> Generator[GoogleDriveFileSystem]:
    fs = GoogleDriveFileSystem(
        token="anon", root_file_id="public-root", skip_instance_cache=True
    )
    try:
        yield fs
    finally:
        fs.close()


def client(fs: GoogleDriveFileSystem) -> PublicDriveClient:
    assert fs._public is not None
    return fs._public


def serve(fs: GoogleDriveFileSystem, *responses: requests.Response) -> mock.Mock:
    get = mock.Mock(side_effect=responses)
    client(fs)._session.get = get
    return get


@pytest.mark.parametrize(
    "url",
    [
        "folder-id",
        "https://drive.google.com/drive/folders/folder-id?usp=sharing&resourcekey=key",
        "https://drive.google.com/drive/u/0/folders/folder-id?resourcekey=key",
        "https://drive.google.com/folderview?id=folder-id&resourcekey=key",
    ],
)
def test_folder_reference(url: str) -> None:
    ref = PublicReference.from_folder(url)
    assert ref.id == "folder-id"
    assert ref.resource_key == ("key" if "resourcekey" in url else None)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "root",
        "https://example.com/drive/folders/a",
        "http://drive.google.com/drive/folders/a",
        "https://drive.google.com/file/d/a/view",
        "../folder",
    ],
)
def test_invalid_folder_reference(url: str) -> None:
    with pytest.raises(ValueError):
        PublicReference.from_folder(url)


def test_anon_builds_no_api_client() -> None:
    with mock.patch("gdrive_fsspec.core.build") as build:
        fs = GoogleDriveFileSystem(
            token="anon", root_file_id="folder-id", skip_instance_cache=True
        )
    try:
        assert fs._api is None
        assert fs.access == "read_only"
        build.assert_not_called()
        with pytest.raises(PermissionError):
            _ = fs.service
    finally:
        fs.close()


def test_listing_is_lazy_and_preserves_names_and_keys(
    public_fs: GoogleDriveFileSystem,
) -> None:
    get = serve(
        public_fs,
        folder(
            (
                "A &amp; B.txt",
                "https://drive.google.com/file/d/binary/view?resourcekey=child-key",
            ),
            (
                "Nested",
                "https://drive.google.com/drive/folders/nested?resourcekey=folder-key",
            ),
            ("Sheet", "https://docs.google.com/spreadsheets/d/sheet/edit"),
        ),
    )
    entries = public_fs.ls("", detail=True)
    assert [entry["name"] for entry in entries] == ["A & B.txt", "Nested", "Sheet"]
    assert [entry["size"] for entry in entries] == [None, 0, None]
    assert public_fs.info("A & B.txt")["id"] == "binary"
    assert public_fs.ls("") == ["A & B.txt", "Nested", "Sheet"]
    assert client(public_fs)._references["binary"].resource_key == "child-key"
    assert get.call_count == 1


def test_nested_lookup_and_cache(public_fs: GoogleDriveFileSystem) -> None:
    get = serve(
        public_fs,
        folder(
            ("dir", "https://drive.google.com/drive/folders/nested?resourcekey=key")
        ),
        folder(("file", "https://drive.google.com/file/d/child/view")),
    )
    assert public_fs.info("dir/file")["id"] == "child"
    assert public_fs.ls("dir") == ["dir/file"]
    assert get.call_count == 2
    assert get.call_args.kwargs["params"] == {"id": "nested", "resourcekey": "key"}
    public_fs.invalidate_cache("dir")
    assert "dir" not in public_fs.dircache


def test_duplicate_names_raise(public_fs: GoogleDriveFileSystem) -> None:
    serve(
        public_fs,
        folder(
            ("same", "https://drive.google.com/file/d/first/view"),
            ("same", "https://drive.google.com/file/d/second/view"),
        ),
    )
    with pytest.raises(MultipleFilesError):
        public_fs.info("same")


def test_empty_folder_is_distinct_from_unknown_html(
    public_fs: GoogleDriveFileSystem,
) -> None:
    serve(public_fs, folder(), response(b"<html>Sign in</html>"))
    assert public_fs.ls("") == []
    public_fs.invalidate_cache()
    with pytest.raises(OSError, match="recognizable"):
        public_fs.ls("")
    assert "" not in public_fs.dircache


@pytest.mark.parametrize(
    "status,error",
    [
        (404, FileNotFoundError),
        (403, PermissionError),
        (401, PermissionError),
        (500, requests.HTTPError),
    ],
)
def test_listing_http_errors(
    public_fs: GoogleDriveFileSystem, status: int, error: type[Exception]
) -> None:
    serve(public_fs, response(status=status))
    with pytest.raises(error):
        public_fs.ls("")


@pytest.mark.parametrize(
    "name,url",
    [
        ("../file", "https://drive.google.com/file/d/file/view"),
        ("file", "https://example.com/file/d/file/view"),
        (".", "https://drive.google.com/file/d/file/view"),
    ],
)
def test_unrepresentable_entries_are_not_silently_dropped(
    public_fs: GoogleDriveFileSystem,
    name: str,
    url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    serve(
        public_fs,
        folder((name, url), ("ok", "https://drive.google.com/file/d/ok/view")),
    )
    assert public_fs.ls("") == ["ok"]
    assert public_fs.info("ok")["id"] == "ok"
    assert "Skipping" in caplog.text


def test_binary_read_seek_and_close(public_fs: GoogleDriveFileSystem) -> None:
    responses = [
        folder(("file", "https://drive.google.com/file/d/file/view?resourcekey=key")),
        partial(b"a", 0, 0, 6),
        partial(b"def", 3, 5, 6),
        partial(b"ab", 0, 1, 6),
    ]
    responses[
        1
    ].url = "https://drive.usercontent.google.com/download?id=file&resourcekey=key"
    get = serve(public_fs, *responses)
    with public_fs.open("file", "rb", cache_type="none") as opened:
        assert opened.size == 6
        assert opened.seek(3) == 3
        assert opened.read(3) == b"def"
        assert opened.read() == b""
        opened.seek(0)
        assert opened.read(2) == b"ab"
    assert get.call_args_list[2].kwargs["headers"] == {"Range": "bytes=3-5"}
    assert "resourcekey=key" in get.call_args_list[2].args[0]
    assert all(res.raw.closed for res in responses)


def test_ignored_initial_range_uses_one_local_snapshot(
    public_fs: GoogleDriveFileSystem,
) -> None:
    get = serve(public_fs, attachment(b"abcdef"))
    opened = client(public_fs).prepare_read("file")
    snapshot = opened.snapshot
    assert snapshot is not None
    try:
        assert opened.read_range(3, 6) == b"def"
        assert opened.read_range(0, 2) == b"ab"
        assert get.call_count == 1
    finally:
        opened.close()
    assert snapshot.closed


@pytest.mark.parametrize(
    "res",
    [
        partial(b"b", 1, 1, 6),
        partial(b"", 0, 0, 6),
    ],
)
def test_invalid_initial_range(
    public_fs: GoogleDriveFileSystem, res: requests.Response
) -> None:
    serve(public_fs, res)
    with pytest.raises(OSError, match="byte range"):
        client(public_fs).prepare_read("file")


@pytest.mark.parametrize(
    "res",
    [
        attachment(b"abcdef"),
        partial(b"bc", 1, 2, 6),
        partial(b"a", 0, 1, 6),
        partial(b"ab", 0, 1, 9),
    ],
)
def test_invalid_followup_range(
    public_fs: GoogleDriveFileSystem, res: requests.Response
) -> None:
    serve(public_fs, partial(b"a", 0, 0, 6), res, attachment(b"abcdef"))
    opened = client(public_fs).prepare_read("file")
    with pytest.raises(OSError):
        opened.read_range(0, 2)


def test_validator_is_sent_and_content_changes_raise(
    public_fs: GoogleDriveFileSystem,
) -> None:
    first = partial(b"a", 0, 0, 6)
    first.headers["ETag"] = '"old"'
    second = partial(b"ab", 0, 1, 6)
    second.headers["ETag"] = '"new"'
    get = serve(public_fs, first, second)
    opened = client(public_fs).prepare_read("file")
    with pytest.raises(OSError, match="changed"):
        opened.read_range(0, 2)
    assert get.call_args.kwargs["headers"]["If-Range"] == '"old"'


def test_empty_binary(public_fs: GoogleDriveFileSystem) -> None:
    serve(public_fs, response(status=416), attachment(b""))
    opened = client(public_fs).prepare_read("file")
    assert opened.size == 0
    assert opened.read_range(0, None) == b""


def test_download_confirmation_and_html_attachment(
    public_fs: GoogleDriveFileSystem,
) -> None:
    form = b'<form id="download-form" action="https://drive.usercontent.google.com/download"><input type="hidden" name="confirm" value="t"></form>'
    get = serve(public_fs, response(form), attachment(b"<html>actual file</html>"))
    opened = client(public_fs).prepare_read("file")
    try:
        assert opened.read_range(0, None) == b"<html>actual file</html>"
        assert "confirm=t" in get.call_args.args[0]
    finally:
        opened.close()


@pytest.mark.parametrize(
    "html",
    [
        b"<html>quota exceeded</html>",
        b'<form id="download-form" action="https://example.com/download"></form>',
    ],
)
def test_interstitial_is_not_returned_as_file_bytes(
    public_fs: GoogleDriveFileSystem, html: bytes
) -> None:
    serve(public_fs, response(html))
    with pytest.raises(PermissionError):
        client(public_fs).prepare_read("file")


def test_native_export_is_one_snapshot(public_fs: GoogleDriveFileSystem) -> None:
    get = serve(
        public_fs,
        folder(
            (
                "sheet",
                "https://docs.google.com/spreadsheets/d/sheet/edit?resourcekey=key",
            )
        ),
        attachment(b"exported xlsx"),
    )
    with public_fs.open("sheet", "rb") as opened:
        assert opened.read(8) == b"exported"
        opened.seek(0)
        assert opened.read() == b"exported xlsx"
    assert get.call_count == 2
    assert get.call_args.kwargs["params"] == {"format": "xlsx", "resourcekey": "key"}


def test_native_export_format_and_denial(public_fs: GoogleDriveFileSystem) -> None:
    serve(
        public_fs,
        folder(("sheet", "https://docs.google.com/spreadsheets/d/sheet/edit")),
        response(b"<html>download denied</html>"),
    )
    with pytest.raises(ValueError, match="Supported export types"):
        public_fs.export("sheet", "unknown")
    with pytest.raises(PermissionError, match="exported"):
        public_fs.export("sheet", "text/csv")


@pytest.mark.parametrize(
    "method,args",
    [
        ("mkdir", ("dir",)),
        ("makedirs", ("dir",)),
        ("rm", ("file",)),
        ("rm_file", ("file",)),
        ("rmdir", ("dir",)),
        ("mv", ("src", "dest")),
        ("cp_file", ("src", "dest")),
        ("touch", ("file",)),
        ("open", ("file", "wb")),
    ],
)
def test_mutations_fail_before_network(
    public_fs: GoogleDriveFileSystem, method: str, args: tuple[str, ...]
) -> None:
    get = serve(public_fs)
    with pytest.raises(PermissionError):
        getattr(public_fs, method)(*args)
    get.assert_not_called()


@pytest.mark.parametrize("path", ["", "file"])
@pytest.mark.parametrize("trashed,fields", [(False, "version"), (True, None)])
def test_api_options_rejected_even_for_root(
    public_fs: GoogleDriveFileSystem, path: str, trashed: bool, fields: str | None
) -> None:
    with pytest.raises(NotImplementedError):
        public_fs.info(path, trashed=trashed, fields=fields)
    with pytest.raises(NotImplementedError):
        public_fs.ls(path, detail=True, trashed=trashed, fields=fields)


def test_reconnect_closes_session_and_drops_cached_identity(
    public_fs: GoogleDriveFileSystem,
) -> None:
    serve(public_fs, folder())
    public_fs.ls("")
    old = client(public_fs)
    old.close = mock.Mock()
    public_fs.__dict__["export_formats"] = {"stale": ["stale"]}
    public_fs.connect("anon")
    old.close.assert_called_once()
    assert client(public_fs) is not old
    assert not public_fs.dircache
    assert "stale" not in public_fs.export_formats


def test_invalid_root_does_not_exist(public_fs: GoogleDriveFileSystem) -> None:
    serve(public_fs, response(status=404))
    assert not public_fs.exists("")


def test_public_content_identity_is_explicitly_unsupported(
    public_fs: GoogleDriveFileSystem,
) -> None:
    with pytest.raises(NotImplementedError):
        public_fs.checksum("file")
    with pytest.raises(NotImplementedError):
        public_fs.ukey("file")


@pytest.mark.parametrize(
    "html",
    [
        b'<div id="flip-contents"><div class="flip-entry">missing link</div></div>',
        b'<div id="flip-contents"><div class="flip-entry"><a href="https://drive.google.com/file/d/file/view">file</a></div>',
    ],
)
def test_incomplete_listing_is_not_cached(
    public_fs: GoogleDriveFileSystem, html: bytes
) -> None:
    serve(public_fs, response(html))
    with pytest.raises(OSError):
        public_fs.ls("")
    assert "" not in public_fs.dircache


def test_unsupported_native_files_remain_listable(
    public_fs: GoogleDriveFileSystem,
) -> None:
    serve(
        public_fs,
        folder(
            ("form", "https://docs.google.com/forms/d/form/edit"),
            ("file", "https://drive.google.com/file/d/file/view"),
        ),
    )
    assert public_fs.ls("") == ["file", "form"]
    with pytest.raises(ValueError, match="no export formats"):
        public_fs.open("form", "rb")


def test_initial_range_must_have_positive_total(
    public_fs: GoogleDriveFileSystem,
) -> None:
    serve(public_fs, partial(b"a", 0, 0, 0))
    with pytest.raises(OSError, match="invalid byte range"):
        client(public_fs).prepare_read("file")


def test_failed_file_initialization_closes_download_snapshot(
    public_fs: GoogleDriveFileSystem,
) -> None:
    serve(public_fs, attachment(b"downloaded file"))
    source = client(public_fs).prepare_read("file")
    assert source.snapshot is not None
    serve(public_fs, folder(("file", "https://drive.google.com/file/d/file/view")))
    with (
        mock.patch.object(client(public_fs), "prepare_read", return_value=source),
        pytest.raises(KeyError) as error,
    ):
        public_fs.open("file", "rb", cache_type="invalid-cache")
    assert error.value.__traceback__ is not None
    assert source.snapshot.closed


@pytest.mark.parametrize(
    "entry",
    [
        '<div id="entry-file" class="new-entry"><a href="https://drive.google.com/file/d/file/view">file</a></div>',
        '<div class="flip-entry"><a href="https://drive.google.com/file/d/file/view">file</a></div>',
    ],
)
def test_entry_id_and_class_are_both_recognized(
    public_fs: GoogleDriveFileSystem, entry: str
) -> None:
    serve(public_fs, response(f'<div id="flip-contents">{entry}</div>'.encode()))
    assert public_fs.ls("") == ["file"]


def test_unrecognized_listing_link_is_not_silently_dropped(
    public_fs: GoogleDriveFileSystem,
) -> None:
    serve(
        public_fs,
        response(
            b'<div id="flip-contents"><a href="https://drive.google.com/file/d/file/view">file</a></div>'
        ),
    )
    with pytest.raises(OSError, match="recognizable folder entry"):
        public_fs.ls("")


def test_reconnection_updates_effective_access() -> None:
    fs = make_api_fs()
    fs.root_file_id = "public-root"
    fs.dircache[""] = [{"id": "private", "name": "private", "type": "file", "size": 1}]
    try:
        fs.connect("anon")
        assert fs.access == "read_only"
        assert not fs.dircache
        with mock.patch.object(fs, "_connect_cache", return_value=mock.Mock()):
            fs.connect("cache")
        assert fs.access == "full_control"
        assert fs._public is None
    finally:
        fs.close()


@pytest.mark.parametrize(
    "root,spaces,sync",
    [(None, "drive", None), ("folder-id", "photos", None), ("folder-id", "drive", 5)],
)
def test_invalid_anonymous_configuration(
    root: str | None, spaces: str, sync: int | None
) -> None:
    with pytest.raises(ValueError):
        GoogleDriveFileSystem(
            token="anon",
            root_file_id=root,
            spaces=spaces,
            changes_sync_interval=sync,
            skip_instance_cache=True,
        )


def test_proxy_and_ca_settings_do_not_enable_netrc_auth(
    public_fs: GoogleDriveFileSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/company-ca.pem")
    session = client(public_fs)._session
    settings = session.merge_environment_settings(
        "https://drive.google.com/", {}, False, None, None
    )
    assert settings["proxies"]["https"] == "http://proxy.example:3128"
    assert settings["verify"] == "/tmp/company-ca.pem"
    with mock.patch(
        "requests.sessions.get_netrc_auth", side_effect=AssertionError("netrc read")
    ):
        prepared = session.prepare_request(
            requests.Request(
                "GET", "https://drive.google.com/", headers={"Authorization": "private"}
            )
        )
        assert "Authorization" not in prepared.headers
        prepared.headers["Authorization"] = "private"
        session.rebuild_auth(prepared, response())
        assert "Authorization" not in prepared.headers


@pytest.mark.parametrize(
    "url",
    [
        "https://colab.research.google.com/drive/notebook",
        "https://sites.google.com/view/site",
        "https://docs.google.com/videos/d/video/edit",
        "https://drive.google.com/drive/folders/not/a/folder",
    ],
)
def test_unknown_file_types_do_not_block_supported_siblings(
    public_fs: GoogleDriveFileSystem, url: str, caplog: pytest.LogCaptureFixture
) -> None:
    serve(
        public_fs,
        folder(("unsupported", url), ("ok", "https://drive.google.com/file/d/ok/view")),
    )
    assert public_fs.ls("") == ["ok"]
    assert "Skipping" in caplog.text
    assert "mimeType" not in public_fs.info("ok")


def test_open_id_links_remain_listable(public_fs: GoogleDriveFileSystem) -> None:
    serve(
        public_fs,
        folder(("file", "https://drive.google.com/open?id=file&resourcekey=key")),
    )
    assert public_fs.info("file")["id"] == "file"
    assert client(public_fs)._references["file"].resource_key == "key"


def test_block_reads_reuse_resolved_download_url(
    public_fs: GoogleDriveFileSystem,
) -> None:
    resolved = (
        "https://drive.usercontent.google.com/download?id=file&confirm=t&uuid=resolved"
    )
    initial = partial(b"a", 0, 0, 10)
    initial.url = resolved
    form = response(
        b'<form id="download-form" action="https://drive.usercontent.google.com/download?route=value&amp;empty="><input type="hidden" name="confirm" value="t"></form>'
    )
    blocks = [partial(bytes([index]), index, index, 10) for index in range(5)]
    for block in blocks:
        block.url = resolved
    get = serve(public_fs, form, initial, *blocks)
    source = client(public_fs).prepare_read("file")
    for index in range(5):
        assert source.read_range(index, index + 1) == bytes([index])
    assert get.call_count == 7
    query = parse_qs(
        urlsplit(get.call_args_list[1].args[0]).query, keep_blank_values=True
    )
    assert query["route"] == ["value"]
    assert query["empty"] == [""]
    assert all(call.args[0] == resolved for call in get.call_args_list[2:])


def test_expired_resolved_url_refreshes_once(public_fs: GoogleDriveFileSystem) -> None:
    first = partial(b"a", 0, 0, 6)
    first.url = "https://drive.usercontent.google.com/download?uuid=expired"
    refreshed = partial(b"bc", 1, 2, 6)
    refreshed.url = "https://drive.usercontent.google.com/download?uuid=fresh"
    final = partial(b"de", 3, 4, 6)
    get = serve(public_fs, first, response(status=403), refreshed, final)
    source = client(public_fs).prepare_read("file")
    assert source.read_range(1, 3) == b"bc"
    assert source.resolved_url == refreshed.url
    assert source.read_range(3, 5) == b"de"
    assert get.call_args_list[1].args[0] == first.url
    assert get.call_args_list[2].args[0].startswith("https://drive.google.com/uc?")
    assert get.call_args_list[3].args[0] == refreshed.url


@pytest.mark.parametrize("path", ["", "dir"])
def test_reading_directory_fails_before_download(
    public_fs: GoogleDriveFileSystem, path: str
) -> None:
    get = serve(
        public_fs, folder(("dir", "https://drive.google.com/drive/folders/dir"))
    )
    with (
        mock.patch.object(client(public_fs), "prepare_read") as prepare,
        pytest.raises(IsADirectoryError),
    ):
        public_fs.open(path, "rb")
    prepare.assert_not_called()
    assert get.call_count == 1


def test_public_disk_usage_has_explicit_error(public_fs: GoogleDriveFileSystem) -> None:
    get = serve(public_fs)
    with pytest.raises(NotImplementedError, match="disk-usage"):
        public_fs.du("")
    get.assert_not_called()


def test_authenticated_disk_usage_remains_supported(
    api_fs: GoogleDriveFileSystem,
) -> None:
    api_fs.dircache[""] = [{"id": "file", "name": "file", "type": "file", "size": 3}]
    assert api_fs.du("") == 3
    assert api_fs.du("", total=False) == {"file": 3}


@pytest.mark.parametrize(
    "kind,source_mime,formats",
    [
        (
            "document",
            "application/vnd.google-apps.document",
            {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "application/vnd.oasis.opendocument.text": "odt",
                "application/rtf": "rtf",
                "application/pdf": "pdf",
                "text/plain": "txt",
                "text/html": "html",
                "application/zip": "zip",
                "application/epub+zip": "epub",
                "text/markdown": "md",
            },
        ),
        (
            "spreadsheets",
            "application/vnd.google-apps.spreadsheet",
            {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
                "application/vnd.oasis.opendocument.spreadsheet": "ods",
                "application/pdf": "pdf",
                "application/zip": "zip",
                "text/csv": "csv",
                "text/tab-separated-values": "tsv",
            },
        ),
        (
            "presentation",
            "application/vnd.google-apps.presentation",
            {
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
                "application/vnd.oasis.opendocument.presentation": "odp",
                "application/pdf": "pdf",
                "text/plain": "txt",
            },
        ),
        (
            "drawings",
            "application/vnd.google-apps.drawing",
            {
                "application/pdf": "pdf",
                "image/jpeg": "jpeg",
                "image/png": "png",
                "image/svg+xml": "svg",
            },
        ),
    ],
)
def test_published_export_formats_route_through_public_client(
    public_fs: GoogleDriveFileSystem,
    kind: str,
    source_mime: str,
    formats: dict[str, str],
) -> None:
    serve(
        public_fs,
        folder(
            ("native", f"https://docs.google.com/{kind}/d/native/edit?resourcekey=key")
        ),
    )
    assert public_fs.info("native")["mimeType"] == source_mime
    assert set(public_fs.export_formats[source_mime]) == set(formats)
    for target_mime, parameter in formats.items():
        get = serve(public_fs, attachment(b"exported content"))
        assert public_fs.export("native", target_mime) == b"exported content"
        if kind == "drawings":
            assert (
                get.call_args.args[0]
                == f"https://docs.google.com/drawings/d/native/export/{parameter}"
            )
            assert get.call_args.kwargs["params"] == {"resourcekey": "key"}
        else:
            assert (
                get.call_args.args[0]
                == f"https://docs.google.com/{kind}/d/native/export"
            )
            assert get.call_args.kwargs["params"] == {
                "format": parameter,
                "resourcekey": "key",
            }


@pytest.mark.parametrize(
    "kind,expected_mime",
    [
        ("document", "text/plain"),
        (
            "spreadsheets",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("presentation", "application/pdf"),
        ("drawings", "image/png"),
    ],
)
def test_broader_catalog_preserves_default_preferences(
    public_fs: GoogleDriveFileSystem, kind: str, expected_mime: str
) -> None:
    serve(
        public_fs, folder(("native", f"https://docs.google.com/{kind}/d/native/edit"))
    )
    source = public_fs.info("native")["mimeType"]
    assert public_fs._resolve_export_mime(source, None) == expected_mime


def test_native_html_export_is_valid_content(public_fs: GoogleDriveFileSystem) -> None:
    serve(
        public_fs,
        folder(("doc", "https://docs.google.com/document/d/doc/edit")),
        attachment(b"<html><body>Document content</body></html>"),
    )
    with public_fs.open("doc", "rb", export_mime_type="text/html") as opened:
        assert opened.read() == b"<html><body>Document content</body></html>"
