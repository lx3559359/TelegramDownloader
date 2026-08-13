from datetime import UTC, datetime
from pathlib import Path

from telegram_downloader.domain import MediaKind
from telegram_downloader.files import (
    archive_target,
    classify_media,
    disambiguate_target,
    sanitize_component,
)


def test_sanitize_windows_reserved_and_illegal_names() -> None:
    assert sanitize_component("CON") == "_CON_"
    assert sanitize_component("con.txt") == "_con.txt_"
    assert sanitize_component('bad<>:"/\\|?*name') == "bad_________name"
    assert sanitize_component("..") == "unnamed"


def test_sanitize_component_preserves_extension_when_truncating() -> None:
    cleaned = sanitize_component("a" * 200 + ".mp4", maximum=32)

    assert len(cleaned) == 32
    assert cleaned.endswith(".mp4")


def test_classify_archive_before_generic_document() -> None:
    assert (
        classify_media("application/zip", "backup.zip", False, False, False)
        is MediaKind.ARCHIVE
    )
    assert (
        classify_media("audio/ogg", "voice.ogg", False, True, False)
        is MediaKind.VOICE
    )


def test_archive_target_uses_source_month_kind_and_message_id(tmp_path: Path) -> None:
    target = archive_target(
        tmp_path,
        "My:Channel",
        datetime(2026, 8, 13, tzinfo=UTC),
        MediaKind.VIDEO,
        "clip?.mp4",
    )

    assert target == tmp_path / "My_Channel" / "2026-08" / "video" / "clip_.mp4"
    assert disambiguate_target(target, 42).name == "clip__42.mp4"


def test_disambiguate_target_does_not_duplicate_existing_suffix(tmp_path: Path) -> None:
    target = tmp_path / "clip__42.mp4"

    assert disambiguate_target(target, 42) == target
