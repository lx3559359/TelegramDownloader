import os
from pathlib import Path

import pytest

from telegram_downloader.thumbnail_cache import ThumbnailCache


def test_thumbnail_keys_are_hashed_and_account_scoped(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=1024)

    first = cache.put("a1:-1001:7:m7", b"one")
    second = cache.put("a2:-1001:7:m7", b"two")

    assert first is not None and first.parent == (tmp_path / "thumbnails").resolve()
    assert second is not None and first != second
    assert "-1001" not in first.name
    assert cache.get("a1:-1001:7:m7") == first


def test_oversized_or_empty_thumbnail_is_not_written(tmp_path: Path) -> None:
    cache = ThumbnailCache(
        tmp_path / "thumbnails",
        max_item_bytes=4,
        max_total_bytes=1024,
    )

    assert cache.put("empty", b"") is None
    assert cache.put("key", b"12345") is None
    assert list((tmp_path / "thumbnails").iterdir()) == []


def test_total_limit_evicts_least_recently_used_file(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=6)
    old = cache.put("old", b"111")
    recent = cache.put("recent", b"222")
    assert old is not None and recent is not None
    # Windows filesystems round sub-100ns values, so use distinct whole seconds.
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(recent, ns=(2_000_000_000, 2_000_000_000))

    newest = cache.put("newest", b"333")

    assert newest is not None
    assert old.exists() is False
    assert recent.exists() is True
    assert cache.total_bytes() == 6


def test_clear_returns_removed_count_and_bytes(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=1024)
    cache.put("a", b"12")
    cache.put("b", b"345")

    assert cache.clear() == (2, 5)
    assert cache.total_bytes() == 0


def test_delete_removes_only_the_requested_key(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=1024)
    cache.put("a", b"12")
    kept = cache.put("b", b"345")
    unrelated = cache.root / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    assert cache.delete("a") is True
    assert cache.delete("a") is False
    assert kept is not None and kept.exists()
    assert unrelated.exists()


def test_cache_limits_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="缩略图缓存上限必须大于零"):
        ThumbnailCache(tmp_path / "thumbnails", max_item_bytes=0)
    with pytest.raises(ValueError, match="缩略图缓存上限必须大于零"):
        ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=0)
