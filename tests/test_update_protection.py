import json
from pathlib import Path

from telegram_downloader.paths import PortablePaths
from telegram_downloader.update_protection import UpdateProtectionProvider


def journal_value() -> dict[str, object]:
    transaction_id = "a" * 32
    return {
        "schemaVersion": 1,
        "transactionId": transaction_id,
        "state": "installing",
        "oldVersion": "0.14.0",
        "targetVersion": "0.15.0",
        "backup": f"data/update/backup/0.14.0-to-0.15.0-{transaction_id[:8]}",
        "extraction": f"data/update/staging/extracted-{transaction_id}",
        "oldFiles": ["TelegramDownloader.exe"],
        "newFiles": ["TelegramDownloader.exe"],
        "backedUp": ["TelegramDownloader.exe"],
        "installed": ["TelegramDownloader.exe"],
    }


def write_journal(paths: PortablePaths, value: object) -> None:
    paths.update_journal.write_text(json.dumps(value), encoding="utf-8")


def test_missing_update_journal_returns_empty_protection(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()

    snapshot = UpdateProtectionProvider(paths).snapshot()

    assert snapshot.protected == frozenset()
    assert snapshot.fail_closed is False
    assert snapshot.protects(paths.update_staging / "old.zip") is False


def test_valid_update_journal_protects_active_transaction_paths(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    value = journal_value()
    write_journal(paths, value)

    snapshot = UpdateProtectionProvider(paths).snapshot()

    backup = paths.root / str(value["backup"])
    extraction = paths.root / str(value["extraction"])
    health = paths.update_staging / f"health-{value['transactionId']}.ok"
    assert snapshot.fail_closed is False
    assert snapshot.protects(paths.update_journal)
    assert snapshot.protects(backup / "TelegramDownloader.exe")
    assert snapshot.protects(extraction / "TelegramDownloader.exe")
    assert snapshot.protects(health)
    assert snapshot.protects(paths.update_staging / "runtime.zip")
    assert snapshot.protects(paths.update_backup / "unrelated") is False


def test_corrupt_update_journal_fails_closed_for_update_roots(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    paths.update_journal.write_text("not-json", encoding="utf-8")

    snapshot = UpdateProtectionProvider(paths).snapshot()

    assert snapshot.fail_closed is True
    assert snapshot.protects(paths.update_staging / "anything.zip")
    assert snapshot.protects(paths.update_backup / "anything" / "file")
    assert snapshot.protects(paths.update_journal)


def test_outside_or_duplicate_update_journal_paths_fail_closed(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    value = journal_value()
    value["backup"] = "C:/outside"
    write_journal(paths, value)
    assert UpdateProtectionProvider(paths).snapshot().fail_closed is True

    text = json.dumps(journal_value())
    text = text.replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1')
    paths.update_journal.write_text(text, encoding="utf-8")
    assert UpdateProtectionProvider(paths).snapshot().fail_closed is True
