# gdrive-fsspec

<p align="center">
  <img src="assets/google-drive-icon.svg" alt="Google Drive" width="80">
</p>

<p align="center">
  <a href="https://github.com/marimo-team/gdrive-fsspec/actions/workflows/ci.yaml"><img src="https://github.com/marimo-team/gdrive-fsspec/actions/workflows/ci.yaml/badge.svg" alt="CI"></a>
  <!-- <a href="https://pypi.org/project/gdrive-fsspec/"><img src="https://img.shields.io/pypi/v/gdrive-fsspec.svg" alt="PyPI"></a> -->
  <!-- <a href="https://pypi.org/project/gdrive-fsspec/"><img src="https://img.shields.io/pypi/pyversions/gdrive-fsspec.svg" alt="Python versions"></a> -->
  <a href="https://github.com/marimo-team/gdrive-fsspec/blob/main/LICENSE"><img src="https://img.shields.io/github/license/marimo-team/gdrive-fsspec.svg" alt="license"></a>
</p>

<p align="center">
  <a href="https://filesystem-spec.readthedocs.io/en/latest/usage.html"><strong>fsspec docs</strong></a> ·
  <a href="https://github.com/marimo-team/gdrive-fsspec/blob/main/CONTRIBUTING.md"><strong>contributing</strong></a>
</p>

---

**gdrive-fsspec** implements the [fsspec](https://filesystem-spec.readthedocs.io/) interface for Google Drive. List directories, read and write files, and plug into any library that speaks fsspec.

## Quickstart

```sh
# Currently, you need to install from the GitHub repository, there is no PyPI package yet.
pip install git+https://github.com/marimo-team/gdrive-fsspec
```

```python
from gdrive_fsspec import GoogleDriveFileSystem

# First run: token="browser". Later: token="cache"
fs = GoogleDriveFileSystem(token="cache")

for entry in fs.ls(""):
    print(entry["name"], entry["type"])

with fs.open("my-folder/data.csv", "rb") as f:
    print(f.read())
```

Most filesystem operations follow the [fsspec usage guide](https://filesystem-spec.readthedocs.io/en/latest/usage.html).

[![Open in molab](https://molab.marimo.io/molab-shield.svg)](https://molab.marimo.io/notebooks/nb_ALKUExChiFKFMrGvHkXMq1)

## Why use gdrive-fsspec

1. **fsspec-native** — same API as S3, GCS, and local filesystems; works with ecosystem tools that accept an `AbstractFileSystem`.
2. **Flexible auth** — user OAuth, service accounts, or anonymous read-only access to public files.
3. **Shared Drives** — target a Shared Drive by name via the `drive=` argument (required for service-account uploads).
4. **Scoped access** — `read_only` or `full_control` OAuth scopes.

## Authentication

### User OAuth (personal Drive)

A browser opens on first use; the token is cached for later sessions.

```python
fs = GoogleDriveFileSystem(token="browser")  # first time
fs = GoogleDriveFileSystem(token="cache")    # reuse cached token
```

On headless or remote machines (SSH, containers, CI), pass `use_local_webserver=False` in `auth_kwargs` to authenticate via the console:

```python
fs = GoogleDriveFileSystem(
    token="browser",
    auth_kwargs={"use_local_webserver": False},
)
```

### Service account

Provide a dict with the service account credentials from the GCP console (same content as the downloaded JSON). See [Google's service account key docs](https://cloud.google.com/iam/docs/service-account-creds#key-types).

```python
fs = GoogleDriveFileSystem(
    creds=service_account_credentials,
    token="service_account",
    drive="My Shared Drive",  # required for uploads
)
```

Service accounts have no personal storage quota. Uploads must target a [Shared Drive](https://developers.google.com/workspace/drive/api/guides/about-shareddrives) where the account is at least a **Contributor**. See [Google's storage-limit errors](https://developers.google.com/workspace/drive/api/guides/handle-errors#storage-limit).

### Anonymous (public files)

For files shared publicly ("anyone with the link"), no authentication is needed:

```python
fs = GoogleDriveFileSystem(token="anon")
```

See the ``GoogleDriveFileSystem`` docstring for `root_file_id`, `access`, `spaces`, and other options.

## Google-native files (Docs, Sheets, Slides)

Google Workspace files have no binary content and cannot be downloaded directly; they must be *exported* to another format. `gdrive-fsspec` does this transparently: opening one for reading exports it to a sensible default format (Docs → `text/plain`, Sheets → `xlsx`, Slides/Drawings → PDF).

```python
# Reads the doc's text via an automatic export.
with fs.open("notes/meeting.gdoc", "rb") as f:
    print(f.read().decode())

# Choose a specific export format.
with fs.open("notes/meeting.gdoc", "rb", export_mime_type="application/pdf") as f:
    pdf_bytes = f.read()

# Or export explicitly. Omit mime_type to use the default.
csv_bytes = fs.export("data/sheet.gsheet", "text/csv")
```

The valid targets for each file type are available via `fs.export_formats`. Note that exporting a multi-sheet spreadsheet to `text/csv` only yields the first sheet, which is why `xlsx` is the default.

## Installation

```sh
pip install git+https://github.com/marimo-team/gdrive-fsspec
```

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, and CI.

## Related projects

- [PyDrive2](https://github.com/iterative/PyDrive2?tab=readme-ov-file#fsspec-filesystem) — another fsspec-compatible Google Drive implementation
