import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.catalog import CatalogError, CatalogRepository
from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind


def dialog(account: str, peer: str, title: str, now: datetime) -> ContentDialog:
    return ContentDialog(
        account,
        peer,
        title,
        "",
        DialogKind.GROUP,
        False,
        True,
        now,
    )


def test_dialog_sync_is_account_scoped_and_marks_missing_unavailable(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号一"), now)
    repo.upsert_account(AccountProfile("a2", "账号二"), now)
    repo.replace_dialogs("a1", [dialog("a1", "-1001", "群一", now)], now)
    repo.replace_dialogs("a2", [dialog("a2", "-1002", "群二", now)], now)

    repo.replace_dialogs("a1", [], now)

    assert repo.list_dialogs("a1") == []
    stale = repo.list_dialogs("a1", include_unavailable=True)
    assert stale == [replace(dialog("a1", "-1001", "群一", now), available=False)]
    assert [item.title for item in repo.list_dialogs("a2")] == ["群二"]


def test_dialog_sync_updates_title_kind_and_archive_state(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    original = dialog("a1", "-1001", "旧标题", now)
    repo.replace_dialogs("a1", [original], now)
    changed = replace(
        original,
        title="新标题",
        username="new_name",
        kind=DialogKind.CHANNEL,
        archived=True,
    )

    repo.replace_dialogs("a1", [changed], now)

    assert repo.list_dialogs("a1") == [changed]


def test_most_recent_account_supports_offline_history(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "旧账号"), now)
    repo.upsert_account(AccountProfile("a2", "最近账号"), now.replace(second=1))

    assert repo.most_recent_account() == AccountProfile("a2", "最近账号")


def test_initialize_rejects_unknown_newer_schema(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=2")

    with pytest.raises(CatalogError, match="版本"):
        CatalogRepository(database).initialize()
