"""Read-only access to Drive's public web endpoints."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from tempfile import SpooledTemporaryFile
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.auth import AuthBase
from urllib3.util.retry import Retry

from ._constants import DIR_MIME_TYPE, LOGGER
from .types import FileInfo
from .typing_utils import override

_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class _DocumentType:
    mime_type: str
    formats: Mapping[str, str]
    format_in_path: bool = False


# MIME types and formats: https://developers.google.com/workspace/drive/api/guides/ref-export-formats
# Query-style public export routes follow https://github.com/wkentaro/gdown/blob/main/gdown/download.py
_DOCUMENT_TYPES: dict[str, _DocumentType] = {
    "document": _DocumentType(
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
    "spreadsheets": _DocumentType(
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
    "presentation": _DocumentType(
        "application/vnd.google-apps.presentation",
        {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
            "application/vnd.oasis.opendocument.presentation": "odp",
            "application/pdf": "pdf",
            "text/plain": "txt",
        },
    ),
    "drawings": _DocumentType(
        "application/vnd.google-apps.drawing",
        {
            "application/pdf": "pdf",
            "image/jpeg": "jpeg",  # Public route token, not the .jpg extension.
            "image/png": "png",
            "image/svg+xml": "svg",
        },
        format_in_path=True,
    ),
    "forms": _DocumentType("application/vnd.google-apps.form", {}),
}
_EXPORTS_BY_MIME: dict[str, tuple[str, _DocumentType]] = {
    document.mime_type: (kind, document)
    for kind, document in _DOCUMENT_TYPES.items()
    if document.formats
}


class _AnonymousAuth(AuthBase):
    @override
    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        r.headers.pop("Authorization", None)
        return r


class _AnonymousSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        # Explicit auth suppresses netrc lookup while retaining proxy/CA settings.
        self.auth = _AnonymousAuth()

    @override
    def rebuild_auth(
        self, prepared_request: requests.PreparedRequest, response: requests.Response
    ) -> None:
        # Requests normally reapplies netrc credentials after redirects.
        prepared_request.headers.pop("Authorization", None)


@dataclass(frozen=True)
class PublicReference:
    id: str
    resource_key: str | None = None

    @classmethod
    def from_folder(cls, value: str) -> PublicReference:
        if _ID.fullmatch(value) and value != "root":
            return cls(value)
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname != "drive.google.com":
            raise ValueError("Expected a public Google Drive folder URL or folder ID")
        match = re.fullmatch(
            r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)/?", parts.path
        )
        query = parse_qs(parts.query)
        if match:
            file_id = match[1]
        elif parts.path == "/folderview" and query.get("id"):
            file_id = query["id"][0]
        else:
            raise ValueError("Expected a Google Drive folder link")
        if not _ID.fullmatch(file_id) or file_id == "root":
            raise ValueError("Invalid public folder ID")
        return cls(file_id, query.get("resourcekey", [None])[0])

    def query(self) -> dict[str, str]:
        params = {"id": self.id}
        if self.resource_key:
            params["resourcekey"] = self.resource_key
        return params


class _FolderParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.recognized = False
        self.complete = False
        self.links: list[tuple[str, str]] = []
        self._depth = 0
        self._entry_depth: int | None = None
        self._entry_links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div":
            if attributes.get("id") == "flip-contents":
                if self.recognized:
                    raise OSError("Drive returned duplicate folder containers")
                self.recognized = True
                self._depth = 1
            elif self._depth:
                self._depth += 1
                if "flip-entry" in (attributes.get("class") or "").split() or (
                    attributes.get("id") or ""
                ).startswith("entry-"):
                    if self._entry_depth is not None:
                        raise OSError("Drive returned nested folder entries")
                    self._entry_depth = self._depth
                    self._entry_links = []
        if tag == "a" and self._depth:
            if self._entry_depth is None:
                raise OSError(
                    "Drive returned a link outside a recognizable folder entry"
                )
            self._href = attributes.get("href")
            self._text = []

    @override
    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self._entry_links.append((self._href, "".join(self._text)))
            self._href = None
        if tag == "div" and self._depth:
            if self._depth == self._entry_depth:
                if len(self._entry_links) != 1:
                    raise OSError("Drive returned an incomplete folder entry")
                self.links.extend(self._entry_links)
                self._entry_depth = None
            self._depth -= 1
            if not self._depth:
                self.complete = True


class _ConfirmationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._in_form = attributes.get("id") == "download-form"
            if self._in_form:
                self.action = attributes.get("action")
        if tag == "input" and self._in_form and attributes.get("type") == "hidden":
            name, value = attributes.get("name"), attributes.get("value")
            if name is not None and value is not None:
                self.fields[name] = value

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def _entry(url: str, name: str) -> tuple[PublicReference, str | None]:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise OSError("Public folder contains an unsupported file link")
    if parts.hostname == "drive.google.com" and "/folders/" in parts.path:
        try:
            return PublicReference.from_folder(url), DIR_MIME_TYPE
        except ValueError as error:
            raise OSError(f"Unsupported public folder link: {name!r}") from error
    query = parse_qs(parts.query)
    if parts.hostname == "drive.google.com" and parts.path == "/open":
        file_id = query.get("id", [""])[0]
        if _ID.fullmatch(file_id):
            return PublicReference(file_id, query.get("resourcekey", [None])[0]), None
    match = re.fullmatch(
        r"/([a-z]+)/d/(?:e/)?([A-Za-z0-9_-]+)(?:/.*)?",
        parts.path,
    )
    if (
        match is None
        or parts.hostname not in {"drive.google.com", "docs.google.com"}
        or (match[1] != "file" and match[1] not in _DOCUMENT_TYPES)
    ):
        raise OSError(f"Unsupported public Drive entry: {name!r}")
    key = parse_qs(parts.query).get("resourcekey", [None])[0]
    kind = match[1]
    mime = None if kind == "file" else _DOCUMENT_TYPES[kind].mime_type
    return PublicReference(match[2], key), mime


def _check_status(response: requests.Response) -> None:
    if response.status_code == 404:
        raise FileNotFoundError("Public Drive item is missing or inaccessible")
    if (
        response.status_code in {401, 403}
        or urlsplit(response.url).hostname == "accounts.google.com"
    ):
        raise PermissionError("Drive item requires sign-in or does not allow downloads")
    response.raise_for_status()


class PublicDriveClient:
    def __init__(self, root: PublicReference) -> None:
        self._references = {root.id: root}
        self._session = _AnonymousSession()
        self._session.headers.update(
            {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
        )
        retry = Retry(
            total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504)
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry))

    def close(self) -> None:
        self._session.close()

    @staticmethod
    def export_formats() -> dict[str, list[str]]:
        return {
            mime: list(document.formats)
            for mime, (_, document) in _EXPORTS_BY_MIME.items()
        }

    def list_children(self, folder_id: str, prefix: str) -> list[FileInfo]:
        ref = self._references.get(folder_id, PublicReference(folder_id))
        with self._session.get(
            "https://drive.google.com/embeddedfolderview",
            params=ref.query(),
            timeout=30,
        ) as response:
            _check_status(response)
            parser = _FolderParser()
            parser.feed(response.text)
        if not parser.recognized or not parser.complete:
            raise OSError("Drive did not return a recognizable public folder listing")
        entries: list[FileInfo] = []
        seen: set[str] = set()
        for url, name in parser.links:
            if not name or "/" in name or name in {".", ".."}:
                LOGGER.warning(
                    "Skipping public Drive entry with an unsupported path name: %r",
                    name,
                )
                continue
            try:
                ref, mime = _entry(url, name)
            except OSError as error:
                LOGGER.warning(
                    "Skipping unsupported public Drive entry %r: %s", name, error
                )
                continue
            if ref.id in seen:
                continue
            self._references[ref.id] = ref
            seen.add(ref.id)
            is_folder = mime == DIR_MIME_TYPE
            entry: FileInfo = {
                "id": ref.id,
                "name": f"{prefix}/{name}" if prefix else name,
                "type": "directory" if is_folder else "file",
                "size": 0 if is_folder else None,
            }
            if mime is not None:
                entry["mimeType"] = mime
            entries.append(entry)
        return sorted(entries, key=lambda entry: entry["name"])

    @contextmanager
    def _download(
        self,
        file_id: str,
        headers: dict[str, str] | None = None,
        *,
        resolved_url: str | None = None,
    ) -> Iterator[requests.Response]:
        if resolved_url is not None:
            response = self._session.get(
                resolved_url, headers=headers, stream=True, timeout=30
            )
            try:
                if (
                    response.status_code == 206
                    and "attachment"
                    in response.headers.get("Content-Disposition", "").lower()
                ):
                    yield response
                    return
            finally:
                response.close()
        ref = self._references.get(file_id, PublicReference(file_id))
        url = "https://drive.google.com/uc?" + urlencode(
            {**ref.query(), "export": "download"}
        )
        for _ in range(3):
            response = self._session.get(url, headers=headers, stream=True, timeout=30)
            try:
                if response.status_code != 416:
                    _check_status(response)
                if (
                    "attachment"
                    in response.headers.get("Content-Disposition", "").lower()
                    or response.status_code == 416
                ):
                    yield response
                    return
                parser = _ConfirmationParser()
                content = response.raw.read(128 * 1024, decode_content=True)
                parser.feed(content.decode("utf-8", errors="replace"))
                action = urljoin(response.url, parser.action or "")
                parts = urlsplit(action)
                if (
                    parser.action is None
                    or parts.scheme != "https"
                    or parts.hostname != "drive.usercontent.google.com"
                    or parts.path != "/download"
                ):
                    raise PermissionError(
                        "Drive did not return a downloadable file; check sharing, download restrictions, and quota"
                    )
                params = {
                    **dict(parse_qsl(parts.query, keep_blank_values=True)),
                    **parser.fields,
                    **ref.query(),
                }
                url = parts._replace(query=urlencode(params), fragment="").geturl()
            finally:
                response.close()
        raise OSError("Drive download confirmation did not resolve")

    def prepare_read(self, file_id: str) -> PublicRead:
        with self._download(file_id, {"Range": "bytes=0-0"}) as response:
            if response.status_code == 206:
                match = _CONTENT_RANGE.fullmatch(
                    response.headers.get("Content-Range", "")
                )
                if (
                    match is None
                    or (match[1], match[2]) != ("0", "0")
                    or int(match[3]) <= 0
                ):
                    raise OSError("Drive returned an invalid byte range")
                if len(response.raw.read(2, decode_content=True)) != 1:
                    raise OSError("Drive returned an incomplete byte range")
                return PublicRead(
                    self,
                    file_id,
                    int(match[3]),
                    _validator(response),
                    resolved_url=response.url,
                )
            if response.status_code == 416:
                return self._prepare_empty(file_id)
            snapshot = SpooledTemporaryFile(max_size=8 * _CHUNK_SIZE, mode="w+b")  # noqa: SIM115 - ownership transfers to PublicRead
            try:
                for chunk in response.iter_content(_CHUNK_SIZE):
                    snapshot.write(chunk)
                size = snapshot.tell()
                snapshot.seek(0)
                return PublicRead(self, file_id, size, snapshot=snapshot)
            except BaseException:
                snapshot.close()
                raise

    def _prepare_empty(self, file_id: str) -> PublicRead:
        with self._download(file_id) as response:
            if response.status_code != 200 or next(response.iter_content(1), b""):
                raise OSError("Drive rejected a range on a nonempty file")
        return PublicRead(self, file_id, 0)

    def read_range(
        self,
        file_id: str,
        start: int,
        end: int,
        size: int,
        validator: str | None,
        resolved_url: str | None = None,
    ) -> tuple[bytes, str]:
        headers = {"Range": f"bytes={start}-{end - 1}"}
        if validator is not None:
            headers["If-Range"] = validator
        with self._download(file_id, headers, resolved_url=resolved_url) as response:
            expected = f"bytes {start}-{end - 1}/{size}"
            if (
                response.status_code != 206
                or response.headers.get("Content-Range") != expected
            ):
                raise OSError(
                    "Drive file changed or the server did not honor the requested byte range"
                )
            if validator is not None and _validator(response) != validator:
                raise OSError("Drive file changed while it was open")
            data = response.raw.read(end - start + 1, decode_content=True)
            if len(data) != end - start:
                raise OSError("Drive returned an incomplete byte range")
            return data, response.url

    def export(self, file_id: str, source_mime: str, target_mime: str) -> bytes:
        kind, document = _EXPORTS_BY_MIME[source_mime]
        ref = self._references.get(file_id, PublicReference(file_id))
        export_format = document.formats[target_mime]
        url = f"https://docs.google.com/{kind}/d/{file_id}/export"
        params: dict[str, str] = {}
        if document.format_in_path:
            url += f"/{export_format}"
        else:
            params["format"] = export_format
        if ref.resource_key:
            params["resourcekey"] = ref.resource_key
        with self._session.get(
            url,
            params=params,
            timeout=30,
        ) as response:
            _check_status(response)
            if (
                "attachment"
                not in response.headers.get("Content-Disposition", "").lower()
            ):
                raise PermissionError("Drive did not return an exported document")
            return response.content


def _validator(response: requests.Response) -> str | None:
    etag = response.headers.get("ETag")
    if etag and not etag.startswith("W/"):
        return etag
    return response.headers.get("Last-Modified")


@dataclass
class PublicRead:
    client: PublicDriveClient
    file_id: str
    size: int
    validator: str | None = None
    snapshot: SpooledTemporaryFile[bytes] | None = None
    resolved_url: str | None = None

    def read_range(self, start: int | None, end: int | None) -> bytes:
        first = start or 0
        last = min(end if end is not None else self.size, self.size)
        if first >= last:
            return b""
        if self.snapshot is not None:
            self.snapshot.seek(first)
            return self.snapshot.read(last - first)
        data, self.resolved_url = self.client.read_range(
            self.file_id, first, last, self.size, self.validator, self.resolved_url
        )
        return data

    def close(self) -> None:
        if self.snapshot is not None:
            self.snapshot.close()
