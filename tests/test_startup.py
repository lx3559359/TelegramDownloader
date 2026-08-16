from PySide6.QtWidgets import QWidget

from telegram_downloader import __main__ as entry
from telegram_downloader.startup import create_startup_indicator


def test_startup_indicator_is_visible_updates_and_closes(qtbot) -> None:
    indicator = create_startup_indicator()
    qtbot.addWidget(indicator.widget)

    assert indicator.widget.isVisible() is True
    assert indicator.status == "正在启动 Telegram 下载器…"

    indicator.set_status("正在准备本地数据…")

    assert indicator.status == "正在准备本地数据…"

    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    indicator.finish(host)

    assert indicator.widget.isVisible() is False
    indicator.close()
    indicator.close()


def test_gui_bootstrap_closes_indicator_after_runner_returns(tmp_path) -> None:
    events: list[object] = []

    class Indicator:
        def set_status(self, text: str) -> None:
            events.append(("status", text))

        def close(self) -> None:
            events.append("close")

    indicator = Indicator()

    def runner(root, *, startup_indicator) -> int:
        events.append(("run", root, startup_indicator))
        return 7

    result = entry._run_gui(
        tmp_path,
        startup_factory=lambda: indicator,
        runner=runner,
    )

    assert result == 7
    assert events == [
        ("status", "正在加载运行组件…"),
        ("run", tmp_path, indicator),
        "close",
    ]


def test_gui_bootstrap_closes_indicator_when_runner_fails(tmp_path) -> None:
    closed: list[bool] = []

    class Indicator:
        def set_status(self, _text: str) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    def runner(_root, *, startup_indicator) -> int:
        del startup_indicator
        raise RuntimeError("synthetic startup failure")

    try:
        entry._run_gui(
            tmp_path,
            startup_factory=Indicator,
            runner=runner,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("startup failure was not propagated")

    assert closed == [True]
