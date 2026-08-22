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


def test_startup_indicator_loads_explicit_cjk_font(qtbot, monkeypatch) -> None:
    from telegram_downloader.ui import theme

    monkeypatch.setattr(theme, "ensure_cjk_font", lambda: "QA CJK Font")

    indicator = create_startup_indicator()
    qtbot.addWidget(indicator.widget)

    assert indicator.font_family == "QA CJK Font"
    indicator.close()


def test_gui_bootstrap_closes_indicator_after_runner_returns(tmp_path) -> None:
    events: list[object] = []

    class Indicator:
        def set_status(self, text: str) -> None:
            events.append(("status", text))

        def close(self) -> None:
            events.append("close")

    indicator = Indicator()

    def runner(root, *, startup_indicator, background) -> int:
        events.append(("run", root, startup_indicator, background))
        return 7

    result = entry._run_gui(
        tmp_path,
        startup_factory=lambda: indicator,
        runner=runner,
    )

    assert result == 7
    assert events == [
        ("status", "正在加载运行组件…"),
        ("run", tmp_path, indicator, False),
        "close",
    ]


def test_gui_bootstrap_closes_indicator_when_runner_fails(tmp_path) -> None:
    closed: list[bool] = []

    class Indicator:
        def set_status(self, _text: str) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    def runner(_root, *, startup_indicator, background) -> int:
        del startup_indicator
        assert background is False
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


def test_background_gui_bootstrap_does_not_create_startup_indicator(tmp_path) -> None:
    calls: list[tuple[object, bool]] = []

    def unexpected_indicator():
        raise AssertionError("background launch must not create a startup window")

    def runner(_root, *, startup_indicator, background) -> int:
        calls.append((startup_indicator, background))
        return 0

    assert (
        entry._run_gui(
            tmp_path,
            background=True,
            startup_factory=unexpected_indicator,
            runner=runner,
        )
        == 0
    )
    assert calls == [(None, True)]
