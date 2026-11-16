"""Read sources and bounded local snapshots."""

from __future__ import annotations

import errno
import io
from collections.abc import Iterable
from tempfile import SpooledTemporaryFile
from threading import Lock
from typing import Protocol

from typing_extensions import Buffer

from .typing_utils import override

READ_CHUNK_SIZE = 1024 * 1024
_MEMORY_LIMIT = 8 * READ_CHUNK_SIZE
_SNAPSHOT_LIMIT = 256 * 1024 * 1024


class ReadSource(Protocol):
    @property
    def size(self) -> int: ...

    def read_range(self, start: int | None, end: int | None) -> bytes: ...

    def close(self) -> None: ...


class ReadSnapshot(io.BufferedIOBase):
    _buffer: SpooledTemporaryFile[bytes] | None = None

    def __init__(self) -> None:
        self._lock = Lock()
        super().__init__()
        self.size = 0
        self._buffer = SpooledTemporaryFile(max_size=_MEMORY_LIMIT, mode="w+b")  # noqa: SIM115 - owned and closed by this read source

    @classmethod
    def from_chunks(
        cls,
        chunks: Iterable[bytes],
        *,
        expected_size: int | None = None,
    ) -> ReadSnapshot:
        snapshot = cls()
        try:
            if expected_size is not None:
                snapshot._check_size(expected_size)
            for chunk in chunks:
                snapshot.write(chunk)
            if expected_size is not None and snapshot.size != expected_size:
                raise OSError("Snapshot stream length does not match expected size")
            snapshot.seek(0)
            return snapshot
        except BaseException:
            snapshot.close()
            raise

    def _open_buffer(self) -> SpooledTemporaryFile[bytes]:
        if self._buffer is None:
            raise ValueError("I/O operation on closed snapshot")
        return self._buffer

    def _check_size(self, size: int) -> None:
        if size > _SNAPSHOT_LIMIT:
            raise OSError(
                errno.EFBIG,
                f"This file is too large to download in full (limit: {_SNAPSHOT_LIMIT / 1024**2:g} MiB)",
            )

    @override
    def write(self, buffer: Buffer, /) -> int:
        target = self._open_buffer()
        self._check_size(target.tell() + memoryview(buffer).nbytes)
        written = target.write(buffer)
        self.size = max(self.size, target.tell())
        return written

    @override
    def read(self, size: int | None = -1, /) -> bytes:
        return self._open_buffer().read(-1 if size is None else size)

    @override
    def seek(self, offset: int, whence: int = 0, /) -> int:
        return self._open_buffer().seek(offset, whence)

    @override
    def tell(self) -> int:
        return self._open_buffer().tell()

    def read_range(self, start: int | None, end: int | None) -> bytes:
        with self._lock:
            first = start or 0
            last = min(end if end is not None else self.size, self.size)
            self.seek(first)
            return self.read(max(0, last - first))

    @override
    def flush(self) -> None:
        if self._buffer is not None:
            self._buffer.flush()

    @override
    def readable(self) -> bool:
        return not self.closed

    @override
    def writable(self) -> bool:
        return not self.closed

    @override
    def seekable(self) -> bool:
        return not self.closed

    @override
    def close(self) -> None:
        with self._lock:
            try:
                super().close()
            finally:
                buffer, self._buffer = self._buffer, None
                if buffer is not None:
                    buffer.close()
