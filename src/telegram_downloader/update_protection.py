from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from telegram_downloader.paths import PortablePaths
from telegram_downloader.update_helper import (
    UpdateTransactionError,
    load_update_journal,
)


@dataclass(frozen=True, slots=True)
class UpdateProtectionSnapshot:
    protected: frozenset[Path]
    fail_closed: bool

    def protects(self, path: Path) -> bool:
        candidate = Path(path).resolve()
        return any(
            candidate == protected or candidate.is_relative_to(protected)
            for protected in self.protected
        )


class UpdateProtectionProvider:
    def __init__(self, paths: PortablePaths) -> None:
        self.paths = paths

    def snapshot(self) -> UpdateProtectionSnapshot:
        if not self.paths.update_journal.exists():
            return UpdateProtectionSnapshot(frozenset(), False)
        try:
            journal = load_update_journal(self.paths)
            transaction_id = str(journal["transactionId"])
            protected = {
                self.paths.update_journal.resolve(),
                self.paths.update_staging.resolve(),
                self.paths.guard(self.paths.root / Path(str(journal["backup"]))),
                self.paths.guard(self.paths.root / Path(str(journal["extraction"]))),
                self.paths.guard(
                    self.paths.update_staging / f"health-{transaction_id}.ok"
                ),
            }
            return UpdateProtectionSnapshot(frozenset(protected), False)
        except (OSError, UpdateTransactionError, ValueError):
            protected = frozenset(
                {
                    self.paths.update_staging.resolve(),
                    self.paths.update_backup.resolve(),
                    self.paths.update_journal.resolve(),
                }
            )
            return UpdateProtectionSnapshot(protected, True)
