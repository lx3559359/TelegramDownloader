from datetime import UTC, datetime

from telegram_downloader.content import SearchResult
from telegram_downloader.content_progress import SearchProgressReporter, SearchResultBatch
from telegram_downloader.domain import MediaKind


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


def test_search_result_batch_is_immutable_and_generation_scoped() -> None:
    result = SearchResult(
        "r1",
        "s1",
        "a1",
        "peer",
        7,
        None,
        "media",
        MediaKind.VIDEO,
        "clip.mp4",
        12,
        datetime(2026, 8, 21, tzinfo=UTC),
        "caption",
        "thumb",
        True,
        False,
        False,
    )
    batch = SearchResultBatch("s1", 3, (result,), stable=False)
    assert batch.search_id == "s1"
    assert batch.generation == 3
    assert batch.results == (result,)
    assert batch.stable is False
