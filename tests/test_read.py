import errno
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from tempfile import SpooledTemporaryFile
from threading import Event
from unittest import mock

import pytest

from gdrive_fsspec._read import ReadSnapshot


def test_snapshot_range_reads_do_not_share_seek_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_positioned, second_started, second_positioned, allow_read = (
        Event(),
        Event(),
        Event(),
        Event(),
    )
    with ReadSnapshot.from_chunks([b"abcdefgh"]) as snapshot:
        seek = snapshot.seek

        def controlled_seek(offset: int, whence: int = 0) -> int:
            result = seek(offset, whence)
            if offset == 0:
                first_positioned.set()
                assert allow_read.wait(2)
            else:
                second_positioned.set()
            return result

        def second_range() -> bytes:
            second_started.set()
            return snapshot.read_range(4, 8)

        monkeypatch.setattr(snapshot, "seek", controlled_seek)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(snapshot.read_range, 0, 4)
            try:
                assert first_positioned.wait(2)
                second = executor.submit(second_range)
                assert second_started.wait(2)
                interleaved = second_positioned.wait(0.05)
            finally:
                allow_read.set()
            assert not interleaved
            assert first.result(timeout=2) == b"abcd"
            assert second.result(timeout=2) == b"efgh"


def test_snapshot_spills_to_disk_and_releases_storage() -> None:
    with mock.patch("gdrive_fsspec._read._MEMORY_LIMIT", 4):
        snapshot = ReadSnapshot.from_chunks([b"abcd", b"efgh"])
    buffer = snapshot._buffer
    assert buffer is not None
    try:
        assert snapshot.size == 8
        assert snapshot.readable() and snapshot.writable() and snapshot.seekable()
        assert snapshot.read_range(3, 6) == b"def"
        assert snapshot.read_range(6, None) == b"gh"
        snapshot.seek(0)
        assert snapshot.read(None) == b"abcdefgh"
    finally:
        snapshot.close()
    assert buffer.closed
    assert snapshot._buffer is None
    snapshot.close()
    with pytest.raises(ValueError, match="closed snapshot"):
        snapshot.read()


def test_snapshot_limit_is_checked_before_writing() -> None:
    with ReadSnapshot(max_size=4) as snapshot:
        snapshot.write(b"abcd")
        with pytest.raises(OSError) as error:
            snapshot.write(b"e")
        assert error.value.errno == errno.EFBIG
        assert snapshot.size == 4
        snapshot.seek(0)
        assert snapshot.read() == b"abcd"


def test_snapshot_size_hint_rejects_before_reading_chunks() -> None:
    read_chunk = mock.Mock(return_value=b"large")
    with pytest.raises(OSError, match="max_snapshot_size=4"):
        ReadSnapshot.from_chunks(
            (read_chunk() for _ in range(1)), max_size=4, size_hint=5
        )
    read_chunk.assert_not_called()


@pytest.mark.parametrize("stream_error", [False, True])
def test_failed_snapshot_stream_closes_storage(stream_error: bool) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"abcd"
        if stream_error:
            raise OSError("stream interrupted")
        yield b"e"

    with SpooledTemporaryFile[bytes](mode="w+b") as buffer:
        with (
            mock.patch("gdrive_fsspec._read.SpooledTemporaryFile", return_value=buffer),
            pytest.raises(OSError),
        ):
            ReadSnapshot.from_chunks(chunks(), max_size=4)
        assert buffer.closed


def test_snapshot_zero_limit_accepts_only_empty_content() -> None:
    with ReadSnapshot.from_chunks([], max_size=0) as snapshot:
        assert snapshot.size == 0
        assert snapshot.read_range(0, None) == b""
    with pytest.raises(OSError, match="max_snapshot_size=0"):
        ReadSnapshot.from_chunks([b"x"], max_size=0)
    with pytest.raises(ValueError, match="nonnegative"):
        ReadSnapshot(max_size=-1)
