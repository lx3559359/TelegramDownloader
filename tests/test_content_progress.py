from telegram_downloader.content_progress import SearchProgressReporter


def test_reporter_throttles_intermediate_updates_and_forces_final_event() -> None:
    now = [0.0]
    events = []
    reporter = SearchProgressReporter(events.append, clock=lambda: now[0])

    for index in range(9):
        reporter.record(matched=index < 2)

    assert events == []
    now[0] = 0.2
    reporter.record(matched=True)
    assert [(item.inspected, item.matched) for item in events] == [(10, 3)]

    reporter.finish("正在整理结果")

    assert events[-1].phase == "正在整理结果"
