from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

from telegram_downloader.update_download import (
    HttpResponse,
    InsufficientUpdateSpaceError,
    ResumableUpdateDownloader,
    UpdateDownloadError,
)


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, int]] = []

    def open(self, url: str, start: int) -> HttpResponse:
        self.calls.append((url, start))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, headers, body = reply
        return HttpResponse(status, headers, BytesIO(body))


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_resumes_with_range_and_atomically_promotes(tmp_path) -> None:
    destination = tmp_path / "runtime.zip"
    destination.with_suffix(".zip.part").write_bytes(b"abc")
    transport = FakeTransport(
        [(206, {"Content-Range": "bytes 3-5/6", "Content-Length": "3"}, b"def")]
    )

    result = ResumableUpdateDownloader(transport, reserve_bytes=0).download(
        ("https://github.com/runtime.zip",), destination, 6, digest(b"abcdef")
    )

    assert result == destination
    assert destination.read_bytes() == b"abcdef"
    assert transport.calls == [("https://github.com/runtime.zip", 3)]
    assert not destination.with_suffix(".zip.part").exists()


def test_restarts_when_server_ignores_range(tmp_path) -> None:
    destination = tmp_path / "runtime.zip"
    destination.with_suffix(".zip.part").write_bytes(b"bad")
    transport = FakeTransport([(200, {"Content-Length": "6"}, b"abcdef")])

    ResumableUpdateDownloader(transport, reserve_bytes=0).download(
        ("https://modelscope.cn/runtime.zip",), destination, 6, digest(b"abcdef")
    )

    assert destination.read_bytes() == b"abcdef"


def test_fails_over_and_retries_corrupt_partial_from_zero(tmp_path) -> None:
    destination = tmp_path / "runtime.zip"
    destination.with_suffix(".zip.part").write_bytes(b"xxx")
    transport = FakeTransport(
        [
            (206, {"Content-Range": "bytes 3-5/6"}, b"def"),
            (200, {"Content-Length": "6"}, b"abcdef"),
        ]
    )

    ResumableUpdateDownloader(transport, reserve_bytes=0).download(
        ("https://github.com/runtime.zip", "https://modelscope.cn/runtime.zip"),
        destination,
        6,
        digest(b"abcdef"),
    )

    assert destination.read_bytes() == b"abcdef"
    assert transport.calls == [
        ("https://github.com/runtime.zip", 3),
        ("https://modelscope.cn/runtime.zip", 0),
    ]


def test_preserves_partial_on_network_failure_and_rejects_final_mismatch(tmp_path) -> None:
    destination = tmp_path / "runtime.zip"
    transport = FakeTransport([OSError("offline"), (200, {}, b"wrong!")])

    with pytest.raises(UpdateDownloadError):
        ResumableUpdateDownloader(transport, reserve_bytes=0).download(
            ("https://github.com/runtime.zip", "https://modelscope.cn/runtime.zip"),
            destination,
            6,
            digest(b"abcdef"),
        )

    assert not destination.exists()


def test_refuses_when_project_drive_has_insufficient_space(tmp_path) -> None:
    transport = FakeTransport([])
    downloader = ResumableUpdateDownloader(
        transport,
        reserve_bytes=2,
        free_bytes=lambda _path: 7,
    )

    with pytest.raises(InsufficientUpdateSpaceError):
        downloader.download(("https://github.com/runtime.zip",), tmp_path / "x.zip", 6, "a" * 64)

    assert transport.calls == []
