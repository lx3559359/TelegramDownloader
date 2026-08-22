from telegram_downloader.notifications import (
    ApplicationEvent,
    EventKind,
    NotificationBatcher,
    NotificationRoute,
)


def completed_event(identity: str, count: int = 1) -> ApplicationEvent:
    return ApplicationEvent(
        EventKind.DOWNLOAD_COMPLETED,
        identity=identity,
        count=count,
        route=NotificationRoute.TASKS,
    )


def test_task_terminal_event_is_deduplicated_and_contains_no_private_text() -> None:
    batcher = NotificationBatcher(window_seconds=5.0)
    private = "private-channel secret-file.mp4 D:\\private"
    event = ApplicationEvent(
        EventKind.DOWNLOAD_COMPLETED,
        identity="task-1",
        count=1,
        route=NotificationRoute.TASKS,
        private_context=private,
    )

    assert batcher.record(event, now=10.0) is True
    assert batcher.record(event, now=11.0) is False
    payload = batcher.flush_due(now=15.0)[0]

    serialized = f"{payload.title} {payload.body}"
    assert "private-channel" not in serialized
    assert "secret-file.mp4" not in serialized
    assert "D:\\private" not in serialized


def test_three_completion_events_are_coalesced_without_postponing_deadline() -> None:
    batcher = NotificationBatcher(window_seconds=5.0)

    batcher.record(completed_event("task-0"), now=0.0)
    batcher.record(completed_event("task-1"), now=2.0)
    batcher.record(completed_event("task-2"), now=4.9)

    assert batcher.next_deadline == 5.0
    assert batcher.flush_due(now=4.99) == []
    payload = batcher.flush_due(now=5.0)[0]
    assert payload.body == "3 个下载任务已完成"
    assert batcher.next_deadline is None


def test_same_task_cannot_emit_two_different_terminal_notifications() -> None:
    batcher = NotificationBatcher(window_seconds=5.0)

    assert batcher.record(completed_event("task-1"), now=1.0) is True
    assert (
        batcher.record(
            ApplicationEvent(
                EventKind.DOWNLOAD_FAILED,
                identity="task-1",
                count=1,
                route=NotificationRoute.TASKS,
            ),
            now=2.0,
        )
        is False
    )


def test_subscription_counts_are_summed_in_their_own_route() -> None:
    batcher = NotificationBatcher(window_seconds=5.0)
    for identity, count in (("poll-1", 2), ("poll-2", 3)):
        batcher.record(
            ApplicationEvent(
                EventKind.SUBSCRIPTION_MATCH,
                identity=identity,
                count=count,
                route=NotificationRoute.SUBSCRIPTIONS,
            ),
            now=1.0,
        )

    payload = batcher.flush_due(now=6.0)[0]

    assert payload.body == "已加入 5 个媒体到下载队列"
    assert payload.route is NotificationRoute.SUBSCRIPTIONS
