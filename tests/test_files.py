from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.domain import MediaKind
from telegram_downloader.files import (
    DownloadNamingSettings,
    archive_target,
    classify_media,
    disambiguate_target,
    render_download_target,
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


def test_default_naming_template_matches_existing_archive_layout(tmp_path: Path) -> None:
    date = datetime(2026, 8, 13, tzinfo=UTC)

    target = render_download_target(
        tmp_path,
        DownloadNamingSettings(),
        "My:Channel",
        date,
        MediaKind.VIDEO,
        42,
        "clip?.mp4",
    )

    assert target == archive_target(
        tmp_path,
        "My:Channel",
        date,
        MediaKind.VIDEO,
        "clip?.mp4",
    )


def test_custom_naming_template_renders_all_requested_fields(tmp_path: Path) -> None:
    naming = DownloadNamingSettings(
        "{year}/{month}/{source}/{media_type}/{message_date}/{message_id}",
        "{stem}_{message_id}{extension}",
    )

    target = render_download_target(
        tmp_path,
        naming,
        "资料:群",
        datetime(2026, 8, 13, tzinfo=UTC),
        MediaKind.PHOTO,
        987,
        "原图?.jpg",
    )

    assert target == (
        tmp_path
        / "2026"
        / "08"
        / "资料_群"
        / "photo"
        / "2026-08-13"
        / "987"
        / "原图__987.jpg"
    )
    assert target.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize(
    ("directory", "filename"),
    [
        ("../{source}", "{original_name}"),
        ("C:/{source}", "{original_name}"),
        ("{source}\\{year}", "{original_name}"),
        ("{unknown}", "{original_name}"),
        ("{source}/{year:>5}", "{original_name}"),
        ("{source}", "../{original_name}"),
        ("{source}", "{message_id}"),
    ],
)
def test_naming_templates_reject_unsafe_or_lossy_syntax(
    directory: str,
    filename: str,
) -> None:
    with pytest.raises(ValueError):
        DownloadNamingSettings(directory, filename)


def test_rendered_placeholder_values_cannot_escape_download_root(tmp_path: Path) -> None:
    target = render_download_target(
        tmp_path,
        DownloadNamingSettings("{source}/{message_id}", "{original_name}"),
        "../../CON",
        datetime(2026, 8, 13, tzinfo=UTC),
        MediaKind.DOCUMENT,
        7,
        "../NUL.txt",
    )

    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert ".." not in target.relative_to(tmp_path).parts
