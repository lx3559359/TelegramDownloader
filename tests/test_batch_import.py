from pathlib import Path

import pytest

from telegram_downloader.batch_import import (
    MAX_BATCH_LINKS,
    parse_batch_links,
    read_batch_text_files,
)


def test_batch_links_normalize_deduplicate_and_keep_invalid_statistics() -> None:
    parsed = parse_batch_links(
        (
            " https://t.me/example/42 ",
            "HTTPS://WWW.T.ME/example/42/?single",
            "not-a-link",
            "",
            "https://t.me/second_channel",
        )
    )

    assert parsed.input_count == 4
    assert [item.normalized_url for item in parsed.links] == [
        "https://t.me/example/42",
        "https://t.me/second_channel",
    ]
    assert parsed.duplicate_count == 1
    assert len(parsed.issues) == 1
    assert parsed.issues[0].line_number == 3


def test_batch_links_require_valid_input_and_enforce_limit() -> None:
    with pytest.raises(ValueError, match="至少输入一条有效"):
        parse_batch_links(("invalid", " "))

    with pytest.raises(ValueError, match=str(MAX_BATCH_LINKS)):
        parse_batch_links(tuple(f"https://t.me/channel_{index}" for index in range(101)))


def test_batch_txt_reader_supports_utf8_bom_and_gb18030(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    first.write_bytes("https://t.me/first_channel\n".encode("utf-8-sig"))
    second = tmp_path / "第二批.TXT"
    second.write_bytes("https://t.me/second_channel\n".encode("gb18030"))

    text = read_batch_text_files((first, second))

    assert text.splitlines() == [
        "https://t.me/first_channel",
        "https://t.me/second_channel",
    ]


def test_batch_txt_reader_rejects_non_txt_and_oversized_content(tmp_path: Path) -> None:
    wrong = tmp_path / "links.csv"
    wrong.write_text("https://t.me/example", encoding="utf-8")
    with pytest.raises(ValueError, match="TXT"):
        read_batch_text_files((wrong,))

    oversized = tmp_path / "large.txt"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        read_batch_text_files((oversized,))
