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


def test_storage_cleanup_events_sum_bytes_without_private_context() -> None:
    batcher = NotificationBatcher(window_seconds=5.0)
    private = "secret task-name D:\\private\\storage-state.json"
    for identity, count, byte_count in (
        ("cleanup-1", 2, 100 * 1024**2),
        ("cleanup-2", 3, 50 * 1024**2),
    ):
        batcher.record(
            ApplicationEvent(
                EventKind.STORAGE_CLEANED,
                identity=identity,
                count=count,
                route=NotificationRoute.MAINTENANCE,
                private_context=private,
                byte_count=byte_count,
            ),
            now=1.0,
        )

    payload = batcher.flush_due(now=6.0)[0]
    serialized = f"{payload.title} {payload.body}"

    assert payload.title == "已释放存储空间"
    assert payload.body == "后台安全清理已删除 5 项，释放 150.0 MiB"
    assert payload.route is NotificationRoute.MAINTENANCE
    assert "secret" not in serialized
    assert "task-name" not in serialized
    assert "private" not in serialized


def test_storage_cleanup_failure_uses_fixed_safe_text() -> None:
    batcher = NotificationBatcher(window_seconds=5.0)
    batcher.record(
        ApplicationEvent(
            EventKind.STORAGE_CLEANUP_FAILED,
            identity="cleanup-failed",
            count=2,
            route=NotificationRoute.MAINTENANCE,
            private_context="secret-file.part D:\\private",
        ),
        now=0.0,
    )

    payload = batcher.flush_due(now=5.0)[0]

    assert payload.title == "自动清理需要处理"
    assert payload.body == "自动清理有 2 项未处理，请打开维护中心查看"
    assert "secret-file" not in payload.body


def test_event_byte_count_is_non_negative_integer() -> None:
    for invalid in (-1, True, 1.5):
        try:
            ApplicationEvent(
                EventKind.STORAGE_CLEANED,
                identity="cleanup",
                count=1,
                route=NotificationRoute.MAINTENANCE,
                byte_count=invalid,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid byte count was accepted")
