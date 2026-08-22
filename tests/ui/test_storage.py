from datetime import UTC, datetime
from pathlib import PurePosixPath

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from telegram_downloader.storage_maintenance import (
    ManualCleanupConfirmation,
    SafeCleanupConfirmation,
    StoragePreviewCategory,
)
from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategorySummary,
    StorageEntry,
    StorageExecutionItem,
    StorageExecutionResult,
    StorageInventory,
    StorageMaintenanceState,
    StorageResultCode,
    StorageTrigger,
)
from telegram_downloader.ui.storage import (
    ManualCleanupDialog,
    StorageCategoryModel,
    StoragePage,
)

NOW = datetime(2026, 8, 22, 8, tzinfo=UTC)


def automatic_inventory() -> StorageInventory:
    return StorageInventory(
        NOW,
        5 * 1024**3,
        (
            StorageEntry(
                "temp-entry",
                PurePosixPath("data/temp/old.tmp"),
                StorageCategory.TEMP,
                20,
                1,
                True,
            ),
        ),
        (
            StorageCategorySummary(
                StorageCategory.TEMP,
                NOW,
                2,
                100,
                1,
                20,
            ),
        ),
    )


def download_inventory() -> StorageInventory:
    return StorageInventory(
        NOW,
        5 * 1024**3,
        (
            StorageEntry(
                "selectable",
                PurePosixPath("downloads/media.mp4.part"),
                StorageCategory.DOWNLOAD_PART,
                30,
                1,
                True,
                task_id="task-1",
                display_name="资料群",
            ),
            StorageEntry(
                "protected",
                PurePosixPath("downloads/unknown.bin.part"),
                StorageCategory.DOWNLOAD_PART,
                40,
                1,
                False,
                StorageResultCode.PROTECTED_BY_TASK,
            ),
        ),
        (
            StorageCategorySummary(
                StorageCategory.DOWNLOAD_PART,
                NOW,
                2,
                70,
                1,
                30,
            ),
            StorageCategorySummary(
                StorageCategory.CORRUPT_ARCHIVE,
                NOW,
                0,
                0,
                0,
                0,
            ),
        ),
    )


def test_storage_page_has_fixed_overviews_categories_and_actions(qtbot) -> None:
    page = StoragePage()
    qtbot.addWidget(page)

    assert StorageCategoryModel.HEADERS == (
        "类别",
        "当前大小",
        "可释放",
        "保留策略",
        "最近扫描",
        "状态",
    )
    assert page.category_model.rowCount() == 7
    assert len(page.overview_value_labels) == 4
    assert all(label.text() == "尚未扫描" for label in page.overview_value_labels)
    assert page.scan_button.text() == "重新扫描"
    assert page.cancel_button.text() == "取消"
    assert "安全" in page.safe_cleanup_button.text()
    assert "分片" in page.download_button.text()
    assert page.last_summary_label.text() == "尚无清理记录"


def test_set_inventory_updates_overviews_without_fabricating_missing_categories(
    qtbot,
) -> None:
    page = StoragePage()
    qtbot.addWidget(page)

    page.set_inventory(automatic_inventory())

    assert page.disk_free_value.text() == "5.00 GiB"
    assert page.managed_value.text() == "100 B"
    assert page.safe_reclaim_value.text() == "20 B"
    assert page.manual_reclaim_value.text() == "尚未扫描"
    temp_row = list(StorageCategory).index(StorageCategory.TEMP)
    assert page.category_model.data(page.category_model.index(temp_row, 1)) == "100 B"


def test_manual_dialog_protected_rows_are_not_checkable(qtbot) -> None:
    dialog = ManualCleanupDialog()
    qtbot.addWidget(dialog)
    dialog.set_entries(download_inventory().entries)
    selectable = dialog.model.index(0, 0)
    protected = dialog.model.index(1, 0)

    assert dialog.model.flags(selectable) & Qt.ItemFlag.ItemIsUserCheckable
    assert not dialog.model.flags(protected) & Qt.ItemFlag.ItemIsUserCheckable
    assert dialog.model.data(selectable, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked


def test_automatic_enable_requires_confirmation(qtbot, monkeypatch) -> None:
    page = StoragePage()
    qtbot.addWidget(page)
    emitted: list[bool] = []
    page.automatic_changed.connect(emitted.append)
    answers = iter([QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: next(answers))

    page.automatic_checkbox.click()
    assert page.automatic_checkbox.isChecked() is False
    assert emitted == []
    page.automatic_checkbox.click()

    assert page.automatic_checkbox.isChecked() is True
    assert emitted == [True]


def test_safe_confirmation_emits_execute_only_after_yes(qtbot, monkeypatch) -> None:
    page = StoragePage()
    qtbot.addWidget(page)
    confirmation = SafeCleanupConfirmation(
        "safe-confirm",
        (StoragePreviewCategory(StorageCategory.TEMP, 1, 20),),
        1,
        20,
        100.0,
    )
    emitted: list[str] = []
    page.safe_execute_requested.connect(emitted.append)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Yes,
    )

    page.present_safe_confirmation(confirmation)

    assert emitted == ["safe-confirm"]


def test_manual_cleanup_requires_two_yes_answers(qtbot, monkeypatch) -> None:
    page = StoragePage()
    qtbot.addWidget(page)
    page.set_inventory(download_inventory())
    dialog = page.manual_dialog
    dialog.model.setData(
        dialog.model.index(0, 0),
        Qt.CheckState.Checked,
        Qt.ItemDataRole.CheckStateRole,
    )
    prepared: list[object] = []
    executed: list[str] = []
    page.manual_prepare_requested.connect(prepared.append)
    page.manual_execute_requested.connect(executed.append)
    answers = iter(
        [
            QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ]
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: next(answers))

    qtbot.mouseClick(dialog.prepare_button, Qt.MouseButton.LeftButton)
    assert prepared == [("selectable",)]
    confirmation = ManualCleanupConfirmation("manual-confirm", 1, 30, 100.0)
    page.present_manual_confirmation(confirmation)
    assert executed == []
    page.present_manual_confirmation(confirmation)

    assert executed == ["manual-confirm"]


def test_busy_state_keeps_cancel_and_result_distinguishes_state_save_failure(
    qtbot,
) -> None:
    page = StoragePage()
    qtbot.addWidget(page)
    page.set_state(StorageMaintenanceState())
    page.set_busy(True)
    assert page.scan_button.isEnabled() is False
    assert page.cancel_button.isEnabled() is True
    result = StorageExecutionResult(
        "plan",
        StorageTrigger.MANUAL_SAFE,
        NOW,
        NOW,
        StorageResultCode.STATE_SAVE_FAILED,
        (
            StorageExecutionItem(
                "entry",
                StorageCategory.TEMP,
                StorageResultCode.COMPLETED,
                20,
            ),
        ),
    )

    page.show_result(result)

    assert "清理完成，记录保存失败" in page.result_label.text()
    assert "已回滚" not in page.result_label.text()


def test_page_action_buttons_emit_intent_only_signals(qtbot) -> None:
    page = StoragePage()
    qtbot.addWidget(page)

    with qtbot.waitSignal(page.scan_requested, timeout=500):
        page.scan_button.click()
    page.set_busy(True)
    with qtbot.waitSignal(page.cancel_requested, timeout=500):
        page.cancel_button.click()
    page.set_busy(False)
    with qtbot.waitSignal(page.safe_prepare_requested, timeout=500):
        page.safe_cleanup_button.click()
    with qtbot.waitSignal(page.download_scan_requested, timeout=500):
        page.download_button.click()


def test_page_keeps_category_table_visible_at_workspace_size(qtbot) -> None:
    page = StoragePage()
    qtbot.addWidget(page)
    page.resize(900, 680)
    page.show()
    qtbot.wait(20)

    assert page.category_table.isVisible()
    assert page.category_table.height() >= 180
    assert page.result_label.isVisible()
