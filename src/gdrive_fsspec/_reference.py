"""Drive IDs and resource keys parsed from folder links."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True)
class DriveReference:
    id: str
    resource_key: str | None = None

    @classmethod
    def from_folder(cls, value: str) -> DriveReference:
        if _ID.fullmatch(value) and value != "root":
            return cls(value)
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname != "drive.google.com":
            raise ValueError("Expected a Google Drive folder URL or folder ID")
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
            raise ValueError("Invalid folder ID")
        return cls(file_id, query.get("resourcekey", [None])[0])

    def query(self) -> dict[str, str]:
        params = {"id": self.id}
        if self.resource_key:
            params["resourcekey"] = self.resource_key
        return params
