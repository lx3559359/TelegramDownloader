import asyncio
import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    AccountProfile,
    ContentDialog,
    ContentSearchQuery,
    ContentSourceKind,
    DialogKind,
    SearchCursor,
    SearchResult,
    SearchScope,
    SearchStatus,
)
from telegram_downloader.content_browser import (
    ContentBrowserService,
    NothingToQueueError,
    SearchRetryPolicy,
)
from telegram_downloader.content_progress import (
    DialogSyncProgress,
    SearchProgress,
    SearchResultBatch,
)
from telegram_downloader.domain import (
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.gateway import (
    FloodWaitError,
    GatewayError,
    RemoteMedia,
    RemoteSearchHit,
    RemoteSearchPage,
    TransientNetworkError,
)
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.thumbnail_cache import ThumbnailCache


def make_dialog(
    account_id: str,
    peer_ref: str,
    title: str,
    now: datetime,
) -> ContentDialog:
    return ContentDialog(
        account_id,
        peer_ref,
        title,
        "",
        DialogKind.GROUP,
        False,
        True,
        now,
    )


def make_hit(
    message_id: int,
    now: datetime,
    *,
    grouped_id: int | None = None,
    peer_ref: str = "-1001",
    source_title: str = "资料群",
    source_kind: ContentSourceKind = ContentSourceKind.GROUP,
) -> RemoteSearchHit:
    remote = RemoteMedia(
        peer_ref,
        source_title,
        message_id,
        grouped_id,
        f"m{message_id}",
        MediaKind.VIDEO,
        f"{message_id}.mp4",
        10,
        now,
        source_kind,
    )
    return RemoteSearchHit(
        remote,
        f"摘要 {message_id}",
        f"{peer_ref}:{message_id}:m{message_id}",
    )


class FakeGateway:
    def __init__(self, profile: AccountProfile) -> None:
        self.profile = profile
        self.dialogs: list[ContentDialog] = []
        self.pages: list[RemoteSearchPage | BaseException] = []
        self.all_pages: list[RemoteSearchPage | BaseException] = []
        self.page_progress: list[SearchProgress | None] = []
        self.albums: dict[tuple[str, int], tuple[RemoteSearchHit, ...]] = {}
        self.thumbnail_values: dict[int, bytes | BaseException | None] = {}
        self.profile_calls = 0
        self.search_cursors: list[SearchCursor | None] = []
        self.all_search_cursors: list[SearchCursor | None] = []
        self.album_calls: list[tuple[str, int]] = []
        self.thumbnail_calls: list[int] = []

    async def account_profile(self) -> AccountProfile:
        self.profile_calls += 1
        return self.profile

    def iter_content_dialogs(self, account_id: str):
        async def generate():
            for item in self.dialogs:
                yield replace(item, account_id=account_id)

        return generate()

    async def search_media_page(self, peer_ref, query, cursor, *, on_progress=None):
        self.search_cursors.append(cursor)
        progress = self.page_progress.pop(0) if self.page_progress else None
        if progress is not None and on_progress is not None:
            on_progress(progress)
        value = self.pages.pop(0)
        if isinstance(value, BaseException):
            raise value
        if progress is None and on_progress is not None:
            on_progress(SearchProgress(len(value.items), len(value.items), "正在整理结果"))
        return value

    async def search_all_media_page(self, query, cursor, *, on_progress=None):
        self.all_search_cursors.append(cursor)
        value = self.all_pages.pop(0)
        if isinstance(value, BaseException):
            raise value
        if on_progress is not None:
            on_progress(SearchProgress(len(value.items), len(value.items), "正在整理结果"))
        return value

    async def expand_album(self, peer_ref, message_id, grouped_id):
        self.album_calls.append((peer_ref, grouped_id))
        return self.albums.get((peer_ref, grouped_id), ())

    async def load_thumbnail(self, peer_ref, message_id, media_id):
        self.thumbnail_calls.append(message_id)
        value = self.thumbnail_values.get(message_id)
        if isinstance(value, BaseException):
            raise value
        return value


class PlannerStub:
    def __init__(self, existing=None) -> None:
        self.existing = set(existing or ())

    def existing_media_keys(self, keys):
        return set(keys) & self.existing


def make_query(
    now: datetime,
    keyword: str = "安装",
    *,
    limit: int = 500,
) -> ContentSearchQuery:
    return ContentSearchQuery(
        keyword,
        ScanFilters(
            now,
            now,
            frozenset({MediaKind.VIDEO}),
            limit,
        ),
    )


def initialized_catalog(tmp_path: Path) -> CatalogRepository:
    catalog = CatalogRepository(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    return catalog


async def prepared_online_service(
    tmp_path: Path,
    now: datetime,
    gateway: FakeGateway,
    *,
    album_concurrency: int = 4,
    thumbnail_concurrency: int = 4,
    sleep=asyncio.sleep,
    retry_policy: SearchRetryPolicy | None = None,
) -> ContentBrowserService:
    catalog = initialized_catalog(tmp_path)
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: now,
        album_concurrency=album_concurrency,
        thumbnail_concurrency=thumbnail_concurrency,
        sleep=sleep,
        retry_policy=retry_policy,
    )
    await service.activate_account()
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    return service


def test_search_retry_policy_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="等待上限"):
        SearchRetryPolicy(maximum_wait_seconds=0)
    with pytest.raises(ValueError, match="重试次数"):
        SearchRetryPolicy(maximum_retries=-1)


@pytest.mark.asyncio
async def test_short_flood_wait_counts_down_and_retries_same_cursor(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        FloodWaitError(3),
        RemoteSearchPage((make_hit(9, now),), None, True),
    ]
    gateway.page_progress = [SearchProgress(7, 2, "正在扫描"), None]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=fake_sleep,
    )
    progress: list[SearchProgress] = []

    with caplog.at_level(
        logging.WARNING,
        logger="telegram_downloader.content_browser",
    ):
        session, results = await service.start_search(
            "-1001",
            make_query(now, "隐私关键词"),
            on_progress=progress.append,
        )

    countdown = [item for item in progress if "自动重试" in item.phase]
    assert [item.phase for item in countdown] == [
        "Telegram 限流，3 秒后自动重试（1/2）",
        "Telegram 限流，2 秒后自动重试（1/2）",
        "Telegram 限流，1 秒后自动重试（1/2）",
    ]
    assert all((item.inspected, item.matched) == (7, 2) for item in countdown)
    assert sleeps == [1, 1, 1]
    assert gateway.search_cursors == [None, None]
    assert session.status is SearchStatus.COMPLETED
    assert [item.message_id for item in results] == [9]
    assert "seconds=3" in caplog.text
    assert "attempt=1" in caplog.text
    assert "cursor=0" in caplog.text
    assert "隐私关键词" not in caplog.text
    assert "资料群" not in caplog.text


@pytest.mark.asyncio
async def test_cancelling_flood_wait_stops_before_retry(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [FloodWaitError(20)]

    async def cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=cancel_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.start_search("-1001", make_query(now))

    interrupted = service.latest_session("-1001")
    assert interrupted is not None
    assert interrupted.status is SearchStatus.INCOMPLETE
    assert interrupted.last_error == "搜索已取消"
    assert gateway.search_cursors == [None]


@pytest.mark.asyncio
async def test_five_page_search_reaches_500_across_short_flood_waits(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    pages = [
        RemoteSearchPage(
            tuple(
                make_hit(message_id, now)
                for message_id in range(upper, upper - 100, -1)
            ),
            SearchCursor(upper - 100),
            False,
        )
        for upper in (500, 400, 300, 200, 100)
    ]
    gateway.pages = [
        pages[0],
        FloodWaitError(1),
        pages[1],
        pages[2],
        FloodWaitError(1),
        pages[3],
        pages[4],
    ]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=fake_sleep,
    )

    session, results = await service.start_search(
        "-1001",
        make_query(now, limit=500),
    )
    while not session.exhausted:
        session, results = await service.load_more(session.id)

    assert session.status is SearchStatus.COMPLETED
    assert len(results) == 500
    assert len({item.id for item in results}) == 500
    assert sleeps == [1, 1]
    assert gateway.search_cursors == [
        None,
        SearchCursor(400),
        SearchCursor(400),
        SearchCursor(300),
        SearchCursor(200),
        SearchCursor(200),
        SearchCursor(100),
    ]


@pytest.mark.asyncio
async def test_long_flood_wait_returns_incomplete_session_that_can_continue(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        RemoteSearchPage((make_hit(10, now),), SearchCursor(10), False),
        FloodWaitError(121),
    ]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=fake_sleep,
    )

    session, results = await service.start_search("-1001", make_query(now))
    session, results = await service.load_more(session.id)

    assert session.status is SearchStatus.INCOMPLETE
    assert session.cursor == SearchCursor(10)
    assert session.last_error == "Telegram 请求需等待 121 秒"
    assert [item.message_id for item in results] == [10]
    assert sleeps == []

    gateway.pages = [RemoteSearchPage((make_hit(5, now),), None, True)]
    resumed, results = await service.load_more(session.id)

    assert resumed.status is SearchStatus.COMPLETED
    assert resumed.last_error is None
    assert [item.message_id for item in results] == [10, 5]
    assert gateway.search_cursors == [
        None,
        SearchCursor(10),
        SearchCursor(10),
    ]


@pytest.mark.asyncio
async def test_third_short_flood_wait_returns_incomplete_without_more_sleep(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [FloodWaitError(1), FloodWaitError(1), FloodWaitError(1)]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=fake_sleep,
    )

    session, results = await service.start_search("-1001", make_query(now))

    assert session.status is SearchStatus.INCOMPLETE
    assert session.last_error == "Telegram 请求需等待 1 秒"
    assert results == []
    assert sleeps == [1, 1]
    assert gateway.search_cursors == [None, None, None]


@pytest.mark.asyncio
async def test_sync_and_search_forward_incremental_progress(tmp_path: Path) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.dialogs = [
        make_dialog("a1", "-1001", "群一", now),
        make_dialog("a1", "-1002", "群二", now),
    ]
    gateway.pages = [RemoteSearchPage((make_hit(10, now),), None, True)]
    service = ContentBrowserService(
        initialized_catalog(tmp_path),
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: now,
    )
    await service.activate_account()
    sync_events: list[DialogSyncProgress] = []

    await service.sync_dialogs(on_progress=sync_events.append)

    assert [item.discovered for item in sync_events] == [1, 2]
    search_events: list[SearchProgress] = []

    await service.start_search(
        "-1001",
        make_query(now),
        on_progress=search_events.append,
    )

    assert search_events[-1].inspected == 1


@pytest.mark.asyncio
async def test_failed_dialog_sync_preserves_cached_dialogs(tmp_path: Path) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    old = make_dialog("a1", "-1001", "旧缓存群", now)
    catalog.replace_dialogs("a1", [old], now)

    class Gateway(FakeGateway):
        def iter_content_dialogs(self, account_id: str):
            async def generate():
                yield make_dialog(account_id, "-1002", "未完成的新群", now)
                raise TransientNetworkError("offline")

            return generate()

    gateway = Gateway(AccountProfile("a1", "账号一"))
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: now,
    )
    await service.activate_account()

    with pytest.raises(TransientNetworkError):
        await service.sync_dialogs()

    assert catalog.list_dialogs("a1") == [old]


@pytest.mark.asyncio
async def test_album_expansion_uses_at_most_four_concurrent_requests(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    reached_four = asyncio.Event()
    release = asyncio.Event()

    class Gateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__(AccountProfile("a1", "账号一"))
            self.active = 0
            self.max_active = 0

        async def expand_album(self, _peer_ref, _message_id, _grouped_id):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 4:
                reached_four.set()
            await release.wait()
            self.active -= 1
            return ()

    gateway = Gateway()
    gateway.pages = [
        RemoteSearchPage(
            tuple(make_hit(100 - value, now, grouped_id=value) for value in range(1, 6)),
            None,
            True,
        )
    ]
    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        album_concurrency=4,
    )
    operation = asyncio.create_task(service.start_search("-1001", make_query(now)))
    try:
        await asyncio.wait_for(reached_four.wait(), timeout=1)
        assert gateway.max_active == 4
    finally:
        release.set()
    _session, results = await operation

    assert [item.message_id for item in results] == [99, 98, 97, 96, 95]


@pytest.mark.asyncio
async def test_thumbnail_loading_uses_at_most_four_concurrent_requests(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    reached_four = asyncio.Event()
    release = asyncio.Event()

    class Gateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__(AccountProfile("a1", "账号一"))
            self.active = 0
            self.max_active = 0

        async def load_thumbnail(self, _peer_ref, _message_id, _media_id):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 4:
                reached_four.set()
            await release.wait()
            self.active -= 1
            return b"thumbnail"

    gateway = Gateway()
    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        thumbnail_concurrency=4,
    )
    session = service.catalog.begin_search(
        "s1", "a1", "-1001", "资料群", make_query(now), now
    )
    hits = [make_hit(100 - value, now) for value in range(6)]
    saved = [
        service._result_from_hit("a1", session, hit, queued=False) for hit in hits
    ]
    service.catalog.save_search_page("a1", "s1", session.generation, saved)
    assert service.get_result(saved[0].id) == saved[0]
    tasks = [asyncio.create_task(service.load_thumbnail(item.id)) for item in saved]
    try:
        await asyncio.wait_for(reached_four.wait(), timeout=1)
        assert gateway.max_active == 4
    finally:
        release.set()

    assert all(path is not None for path in await asyncio.gather(*tasks))


@pytest.mark.parametrize(
    ("name", "value"),
    [("album_concurrency", 0), ("thumbnail_concurrency", 0)],
)
def test_content_lookup_concurrency_must_be_positive(
    tmp_path: Path,
    name: str,
    value: int,
) -> None:
    with pytest.raises(ValueError, match="并发数"):
        ContentBrowserService(
            initialized_catalog(tmp_path / name),
            ThumbnailCache(tmp_path / f"{name}-thumbs"),
            **{name: value},
        )


@pytest.mark.asyncio
async def test_cached_account_is_available_offline_then_refreshes_online(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "旧缓存群", now)],
        now,
    )
    thumbnails = ThumbnailCache(tmp_path / "thumbs", max_total_bytes=1024)
    service = ContentBrowserService(catalog, thumbnails, clock=lambda: now)

    cached_profile, cached_dialogs = await service.activate_cached_account()

    assert cached_profile == AccountProfile("a1", "账号一")
    assert [item.title for item in cached_dialogs] == ["旧缓存群"]
    assert service.online is False
    assert service.list_sessions() == []

    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.dialogs = [make_dialog("a1", "-1002", "新同步群", now)]
    service.bind_online(gateway, PlannerStub())
    profile, cached = await service.activate_account()
    fresh = await service.sync_dialogs()

    assert profile == AccountProfile("a1", "账号一")
    assert [item.title for item in cached] == ["旧缓存群"]
    assert [item.title for item in fresh] == ["新同步群"]
    assert catalog.list_dialogs("a1") == fresh

    catalog.upsert_account(AccountProfile("a2", "账号二"), now)
    catalog.replace_dialogs(
        "a2",
        [make_dialog("a2", "-2001", "账号二群", now)],
        now,
    )
    gateway.profile = AccountProfile("a2", "账号二")

    switched, switched_dialogs = await service.activate_account()

    assert switched.account_id == "a2"
    assert [item.title for item in switched_dialogs] == ["账号二群"]
    assert service.list_dialogs() == switched_dialogs


@pytest.mark.asyncio
async def test_dialog_cache_age_and_empty_sync_are_tracked(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 8, tzinfo=UTC)
    current = [now]
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: current[0],
    )
    await service.activate_account()

    assert service.dialog_cache_stale(timedelta(seconds=60)) is True
    await service.sync_dialogs()
    assert service.dialog_cache_stale(timedelta(seconds=60)) is False

    current[0] = now + timedelta(seconds=61)
    assert service.dialog_cache_stale(timedelta(seconds=60)) is True


@pytest.mark.asyncio
async def test_latest_session_is_scoped_to_selected_dialog(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 8, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [
            make_dialog("a1", "-1001", "群一", now),
            make_dialog("a1", "-1002", "群二", now),
        ],
        now,
    )
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        clock=lambda: now,
    )
    await service.activate_cached_account()
    first = catalog.begin_search(
        "s1",
        "a1",
        "-1001",
        "群一",
        make_query(now, "甲"),
        now,
    )
    catalog.begin_search(
        "s2",
        "a1",
        "-1002",
        "群二",
        make_query(now, "乙"),
        now,
    )

    assert service.latest_session("-1001") == first
    assert service.latest_session("-9999") is None


@pytest.mark.asyncio
async def test_dialog_sync_calls_gateway_one_at_a_time(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingGateway(FakeGateway):
        def __init__(self):
            super().__init__(AccountProfile("a1", "账号一"))
            self.active = 0
            self.peak = 0
            self.calls = 0

        def iter_content_dialogs(self, account_id):
            async def generate():
                self.calls += 1
                self.active += 1
                self.peak = max(self.peak, self.active)
                entered.set()
                await release.wait()
                self.active -= 1
                if False:
                    yield None

            return generate()

    gateway = BlockingGateway()
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: now,
    )
    await service.activate_account()

    first = asyncio.create_task(service.sync_dialogs())
    await entered.wait()
    second = asyncio.create_task(service.sync_dialogs())
    await asyncio.sleep(0)

    assert gateway.calls == 1
    assert gateway.peak == 1

    release.set()
    await asyncio.gather(first, second)
    assert gateway.calls == 2
    assert gateway.peak == 1


@pytest.mark.asyncio
async def test_search_pages_expand_albums_deduplicate_and_persist_cursor(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        RemoteSearchPage(
            (make_hit(10, now, grouped_id=900), make_hit(9, now, grouped_id=900), make_hit(8, now)),
            SearchCursor(700),
            False,
        ),
        RemoteSearchPage((make_hit(8, now), make_hit(7, now)), None, True),
    ]
    gateway.albums[("-1001", 900)] = (
        make_hit(11, now, grouped_id=900),
        make_hit(10, now, grouped_id=900),
        make_hit(9, now, grouped_id=900),
    )
    planner = PlannerStub({("-1001", 8, "m8")})
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=planner,
        uuid_factory=iter(["search-1"]).__next__,
        clock=lambda: now,
    )
    await service.activate_account()

    session, first_page = await service.start_search("-1001", make_query(now))

    assert session.status is SearchStatus.RUNNING
    assert [item.message_id for item in first_page] == [11, 10, 9, 8]
    assert gateway.album_calls == [("-1001", 900)]
    assert catalog.get_session("a1", session.id).cursor == SearchCursor(700)
    queued = next(item for item in first_page if item.message_id == 8)
    assert queued.queued is True
    assert queued.selected is False

    session, all_results = await service.load_more(session.id)

    assert session.status is SearchStatus.COMPLETED
    assert session.exhausted is True
    assert [item.message_id for item in all_results] == [11, 10, 9, 8, 7]
    assert len(
        {(item.peer_ref, item.message_id, item.media_id) for item in all_results}
    ) == len(all_results)


@pytest.mark.asyncio
async def test_global_search_dispatches_without_a_dialog_and_keeps_sources(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    gateway.all_pages = [
        RemoteSearchPage(
            (
                make_hit(
                    10,
                    now,
                    peer_ref="-1001",
                    source_title="资料群",
                    source_kind=ContentSourceKind.GROUP,
                ),
                make_hit(
                    10,
                    now,
                    peer_ref="42",
                    source_title="联系人",
                    source_kind=ContentSourceKind.PRIVATE,
                ),
            ),
            None,
            True,
        )
    ]
    service = await prepared_online_service(tmp_path, now, gateway)

    session, results = await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        make_query(now),
        scope=SearchScope.ALL_DIALOGS,
    )

    assert session.scope is SearchScope.ALL_DIALOGS
    assert session.peer_ref == ALL_DIALOGS_SCOPE_REF
    assert session.dialog_title == ALL_DIALOGS_TITLE
    assert gateway.all_search_cursors == [None]
    assert {item.peer_ref for item in results} == {"-1001", "42"}
    assert {item.source_title for item in results} == {"资料群", "联系人"}
    assert {item.source_kind for item in results} == {
        ContentSourceKind.GROUP,
        ContentSourceKind.PRIVATE,
    }


@pytest.mark.asyncio
async def test_global_albums_with_equal_grouped_id_expand_per_peer(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    gateway.all_pages = [
        RemoteSearchPage(
            (
                make_hit(20, now, grouped_id=900, peer_ref="-1001"),
                make_hit(
                    30,
                    now,
                    grouped_id=900,
                    peer_ref="42",
                    source_title="联系人",
                    source_kind=ContentSourceKind.PRIVATE,
                ),
            ),
            None,
            True,
        )
    ]
    gateway.albums[("-1001", 900)] = (
        make_hit(20, now, grouped_id=900, peer_ref="-1001"),
        make_hit(19, now, grouped_id=900, peer_ref="-1001"),
    )
    gateway.albums[("42", 900)] = (
        make_hit(
            30,
            now,
            grouped_id=900,
            peer_ref="42",
            source_title="联系人",
            source_kind=ContentSourceKind.PRIVATE,
        ),
        make_hit(
            29,
            now,
            grouped_id=900,
            peer_ref="42",
            source_title="联系人",
            source_kind=ContentSourceKind.PRIVATE,
        ),
    )
    service = await prepared_online_service(tmp_path, now, gateway)

    _session, results = await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        make_query(now),
        scope=SearchScope.ALL_DIALOGS,
    )

    assert gateway.album_calls == [("-1001", 900), ("42", 900)]
    assert {(item.peer_ref, item.message_id) for item in results} == {
        ("-1001", 20),
        ("-1001", 19),
        ("42", 30),
        ("42", 29),
    }


@pytest.mark.asyncio
async def test_global_search_fills_one_application_page_from_sparse_server_pages(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    first_cursor = SearchCursor(900, 1, "-1001")
    second_cursor = SearchCursor(800, 2, "42")
    gateway.all_pages = [
        RemoteSearchPage(
            tuple(make_hit(value, now) for value in range(1, 11)),
            first_cursor,
            False,
        ),
        RemoteSearchPage(
            tuple(make_hit(value, now, peer_ref="42") for value in range(101, 191)),
            second_cursor,
            False,
        ),
    ]
    service = await prepared_online_service(tmp_path, now, gateway)
    events: list[SearchProgress] = []

    session, results = await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        make_query(now, limit=150),
        scope=SearchScope.ALL_DIALOGS,
        on_progress=events.append,
    )

    assert len(results) == 100
    assert gateway.all_search_cursors == [None, first_cursor]
    assert session.cursor == second_cursor
    assert session.status is SearchStatus.RUNNING
    assert [event.inspected for event in events] == [10, 100]


@pytest.mark.asyncio
async def test_global_result_limit_is_account_wide_and_cursor_stall_is_incomplete(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    stalled = SearchCursor(50, 9, "42")
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    gateway.all_pages = [
        RemoteSearchPage(
            tuple(make_hit(value, now) for value in range(1, 61)),
            stalled,
            False,
        ),
        RemoteSearchPage(
            tuple(make_hit(value, now, peer_ref="42") for value in range(101, 161)),
            stalled,
            False,
        ),
    ]
    service = await prepared_online_service(tmp_path, now, gateway)

    with pytest.raises(GatewayError, match="分页未前进"):
        await service.start_search(
            ALL_DIALOGS_SCOPE_REF,
            make_query(now, limit=100),
            scope=SearchScope.ALL_DIALOGS,
        )

    saved = service.list_sessions()[0]
    assert saved.scope is SearchScope.ALL_DIALOGS
    assert saved.status is SearchStatus.INCOMPLETE
    assert saved.cursor == stalled
    assert len(service.list_results(saved.id)) == 60


@pytest.mark.asyncio
async def test_global_limit_and_progress_are_account_wide(tmp_path: Path) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    gateway.all_pages = [
        RemoteSearchPage(
            tuple(make_hit(value, now, peer_ref="-1001") for value in range(1, 101)),
            SearchCursor(100, 4, "-1001"),
            False,
        ),
        RemoteSearchPage(
            tuple(make_hit(value, now, peer_ref="42") for value in range(101, 106)),
            SearchCursor(105, 5, "42"),
            False,
        ),
    ]
    service = await prepared_online_service(tmp_path, now, gateway)
    first_events: list[SearchProgress] = []
    more_events: list[SearchProgress] = []

    session, first_page = await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        make_query(now, limit=101),
        scope=SearchScope.ALL_DIALOGS,
        on_progress=first_events.append,
    )
    session, all_results = await service.load_more(
        session.id,
        on_progress=more_events.append,
    )

    assert len(first_page) == 100
    assert len(all_results) == 101
    assert session.status is SearchStatus.COMPLETED
    for events in (first_events, more_events):
        assert all(
            left.inspected <= right.inspected
            for left, right in zip(events, events[1:], strict=False)
        )


@pytest.mark.asyncio
async def test_search_stops_exactly_at_item_limit(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        RemoteSearchPage(
            tuple(make_hit(message_id, now) for message_id in range(200, 100, -1)),
            SearchCursor(100),
            False,
        ),
        RemoteSearchPage(
            tuple(make_hit(message_id, now) for message_id in range(100, 95, -1)),
            SearchCursor(95),
            False,
        ),
    ]
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        uuid_factory=iter(["search-1"]).__next__,
        clock=lambda: now,
    )
    await service.activate_account()

    session, first_page = await service.start_search(
        "-1001",
        make_query(now, limit=101),
    )
    session, results = await service.load_more(session.id)

    assert len(first_page) == 100
    assert len(results) == 101
    assert session.status is SearchStatus.COMPLETED
    assert session.exhausted is True
    assert results[-1].message_id == 100


@pytest.mark.asyncio
async def test_album_is_deferred_or_skipped_instead_of_split(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path / "deferred")
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    trigger = make_hit(500, now, grouped_id=900)
    album = (
        trigger,
        make_hit(499, now, grouped_id=900),
    )
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        RemoteSearchPage(
            tuple(make_hit(message_id, now) for message_id in range(700, 601, -1))
            + (trigger,),
            SearchCursor(400),
            False,
        ),
        RemoteSearchPage((trigger,), None, True),
    ]
    gateway.albums[("-1001", 900)] = album
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "deferred-thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        uuid_factory=iter(["search-1"]).__next__,
        clock=lambda: now,
    )
    await service.activate_account()

    session, first_page = await service.start_search(
        "-1001",
        make_query(now, limit=101),
    )

    assert len(first_page) == 99
    assert all(item.grouped_id is None for item in first_page)
    assert session.cursor == SearchCursor(501)

    session, all_results = await service.load_more(session.id)

    assert len(all_results) == 101
    assert [item.message_id for item in all_results[-2:]] == [500, 499]
    assert session.status is SearchStatus.COMPLETED

    limited_catalog = initialized_catalog(tmp_path / "skipped")
    limited_catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    limited_catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    limited_gateway = FakeGateway(AccountProfile("a1", "账号一"))
    limited_gateway.pages = [
        RemoteSearchPage(
            tuple(make_hit(message_id, now) for message_id in range(700, 601, -1))
            + (trigger,),
            None,
            True,
        )
    ]
    limited_gateway.albums[("-1001", 900)] = album
    limited_service = ContentBrowserService(
        limited_catalog,
        ThumbnailCache(tmp_path / "skipped-thumbs"),
        gateway=limited_gateway,
        planner=PlannerStub(),
        uuid_factory=iter(["search-2"]).__next__,
        clock=lambda: now,
    )
    await limited_service.activate_account()

    limited_session, limited_results = await limited_service.start_search(
        "-1001",
        make_query(now, limit=100),
    )

    assert len(limited_results) == 99
    assert limited_session.status is SearchStatus.COMPLETED
    assert limited_session.last_error == "达到数量上限"


@pytest.mark.asyncio
async def test_search_failure_keeps_last_successful_page_and_safe_error(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        RemoteSearchPage((make_hit(10, now),), SearchCursor(10), False),
        TransientNetworkError("隐私关键词和消息正文"),
    ]
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        uuid_factory=iter(["search-1"]).__next__,
        clock=lambda: now,
    )
    await service.activate_account()
    session, _results = await service.start_search(
        "-1001",
        make_query(now, "隐私关键词"),
    )

    with pytest.raises(TransientNetworkError):
        await service.load_more(session.id)

    failed = catalog.get_session("a1", session.id)
    assert failed.status is SearchStatus.INCOMPLETE
    assert failed.cursor == SearchCursor(10)
    assert failed.last_error == "Telegram 网络连接失败"
    assert len(catalog.list_results("a1", session.id)) == 1


@pytest.mark.asyncio
async def test_cancelled_search_keeps_partial_results_and_propagates(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        RemoteSearchPage((make_hit(10, now),), SearchCursor(10), False),
        asyncio.CancelledError(),
    ]
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        uuid_factory=iter(["search-1"]).__next__,
        clock=lambda: now,
    )
    await service.activate_account()
    session, _results = await service.start_search("-1001", make_query(now))

    with pytest.raises(asyncio.CancelledError):
        await service.load_more(session.id)

    cancelled = catalog.get_session("a1", session.id)
    assert cancelled.status is SearchStatus.INCOMPLETE
    assert cancelled.cursor == SearchCursor(10)
    assert len(catalog.list_results("a1", session.id)) == 1


def make_saved_result(
    search_id: str,
    now: datetime,
    result_id: str,
    message_id: int,
    *,
    selected: bool = True,
    available: bool = True,
) -> SearchResult:
    return SearchResult(
        result_id,
        search_id,
        "a1",
        "-1001",
        message_id,
        None,
        f"m{message_id}",
        MediaKind.VIDEO,
        f"{message_id}.mp4",
        10,
        now,
        f"摘要 {message_id}",
        f"a1:-1001:{message_id}:m{message_id}",
        selected,
        available,
        False,
    )


def test_prepare_global_download_uses_account_planner(tmp_path: Path) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号"), now)
    query = make_query(now)
    session = catalog.begin_search(
        "global-1",
        "a1",
        ALL_DIALOGS_SCOPE_REF,
        ALL_DIALOGS_TITLE,
        query,
        now,
        scope=SearchScope.ALL_DIALOGS,
    )
    values = [
        replace(
            make_saved_result(session.id, now, "r1", 10),
            peer_ref="-1001",
            source_title="资料群",
            source_kind=ContentSourceKind.GROUP,
        ),
        replace(
            make_saved_result(session.id, now, "r2", 11),
            peer_ref="42",
            source_title="联系人",
            source_kind=ContentSourceKind.PRIVATE,
        ),
    ]
    catalog.save_search_page("a1", session.id, session.generation, values)

    class Planner:
        def __init__(self) -> None:
            self.selected = []

        def existing_media_keys(self, keys):
            return set()

        def plan_account_search(self, received_query, selected):
            assert received_query == query
            self.selected = selected
            return SimpleNamespace(items=tuple(selected))

        def plan_selected(self, source_ref, source_title, received_query, selected):
            raise AssertionError("全账号搜索不应调用单会话计划器")

    planner = Planner()
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        planner=planner,
        clock=lambda: now,
    )
    service.account = AccountProfile("a1", "账号")

    preparation = service.prepare_download(session.id)

    assert len(preparation.preview.items) == 2
    assert {item.source_title for item in planner.selected} == {"资料群", "联系人"}


@pytest.mark.asyncio
async def test_prepare_and_finalize_queue_report_duplicates_and_unavailable(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    query = make_query(now)
    session = catalog.begin_search("search-1", "a1", "-1001", "资料群", query, now)
    results = [
        make_saved_result(session.id, now, "valid-1", 11),
        make_saved_result(session.id, now, "valid-2", 10),
        make_saved_result(
            session.id,
            now,
            "unavailable",
            9,
            available=False,
        ),
        make_saved_result(session.id, now, "duplicate", 8),
    ]
    catalog.save_search_page("a1", session.id, session.generation, results)

    tasks = TaskRepository(tmp_path / "tasks.sqlite3")
    tasks.initialize()
    occupied_task = TaskRecord(
        "occupied",
        SourceKind.CHANNEL_OR_GROUP,
        "-1001",
        "资料群",
        "telegram://peer/-1001",
        query.filters,
        TaskStatus.QUEUED,
        now,
        now,
    )
    duplicate_item = MediaItem(
        "occupied-item",
        occupied_task.id,
        "-1001",
        8,
        None,
        "m8",
        MediaKind.VIDEO,
        "8.mp4",
        tmp_path / "8.mp4",
        10,
        now,
    )
    tasks.create_task(occupied_task, [duplicate_item])
    planner = TaskPlanner(
        FakeGateway(AccountProfile("a1", "账号一")),
        tasks,
        tmp_path / "downloads",
        uuid_factory=iter(["selected", "item-11", "item-10"]).__next__,
        clock=lambda: now,
    )
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        planner=planner,
        clock=lambda: now,
    )
    await service.activate_cached_account()

    preparation = service.prepare_download(session.id)

    assert preparation.selected_count == 4
    assert len(preparation.preview.items) == 2
    assert preparation.duplicate_count == 1
    assert preparation.unavailable_count == 1
    assert len(preparation.preview_result_ids) == 2

    planner.commit_selected(preparation.preview)
    report = service.finalize_queue(session.id, joined_count=2)

    assert report.joined_count == 2
    assert report.duplicate_count == 1
    reopened = CatalogRepository(catalog.database)
    saved = {item.id: item for item in reopened.list_results("a1", session.id)}
    assert saved["valid-1"].queued is True
    assert saved["valid-2"].queued is True
    assert saved["duplicate"].queued is True
    assert saved["unavailable"].queued is False
    assert all(not saved[item_id].selected for item_id in ("valid-1", "valid-2", "duplicate"))


def test_reconcile_queue_marks_only_media_present_in_task_repository(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号"), now)
    session = catalog.begin_search(
        "search-1",
        "a1",
        "-1001",
        "资料群",
        make_query(now),
        now,
    )
    queued = make_saved_result(session.id, now, "queued", 11)
    untouched = make_saved_result(session.id, now, "untouched", 10)
    catalog.save_search_page(
        "a1",
        session.id,
        session.generation,
        [queued, untouched],
    )
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        planner=PlannerStub({("-1001", 11, "m11")}),
        clock=lambda: now,
    )
    service.account = AccountProfile("a1", "账号")

    snapshot = service.reconcile_queue(session.id)

    saved = {item.id: item for item in snapshot.results}
    assert saved["queued"].queued is True
    assert saved["queued"].selected is False
    assert saved["untouched"].queued is False
    assert saved["untouched"].selected is True


@pytest.mark.asyncio
async def test_prepare_download_rejects_all_skipped_selection(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    query = make_query(now)
    session = catalog.begin_search("search-1", "a1", "-1001", "资料群", query, now)
    unavailable = make_saved_result(
        session.id,
        now,
        "unavailable",
        9,
        available=False,
    )
    catalog.save_search_page("a1", session.id, session.generation, [unavailable])
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        planner=PlannerStub(),
        clock=lambda: now,
    )
    await service.activate_cached_account()

    with pytest.raises(NothingToQueueError) as caught:
        service.prepare_download(session.id)

    assert caught.value.selected_count == 1
    assert caught.value.unavailable_count == 1


@pytest.mark.asyncio
async def test_thumbnail_cache_hit_remote_fallback_and_history_cleanup(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.upsert_account(AccountProfile("a2", "账号二"), now)
    first = catalog.begin_search(
        "s1",
        "a1",
        "-1001",
        "资料群",
        make_query(now, "一"),
        now,
    )
    second = catalog.begin_search(
        "s2",
        "a1",
        "-1001",
        "资料群",
        make_query(now, "二"),
        now,
    )
    shared_key = "a1:-1001:7:m7"
    first_result = replace(
        make_saved_result(first.id, now, "r1", 7, selected=False),
        thumbnail_key=shared_key,
    )
    second_result = replace(
        make_saved_result(second.id, now, "r2", 7, selected=False),
        thumbnail_key=shared_key,
    )
    remote_result = make_saved_result(second.id, now, "remote", 8, selected=False)
    failed_result = make_saved_result(second.id, now, "failed", 9, selected=False)
    catalog.save_search_page("a1", first.id, first.generation, [first_result])
    catalog.save_search_page(
        "a1",
        second.id,
        second.generation,
        [second_result, remote_result, failed_result],
    )
    other = catalog.begin_search(
        "other",
        "a2",
        "-2001",
        "其他群",
        make_query(now, "其他"),
        now,
    )
    other_result = replace(
        make_saved_result(other.id, now, "other-result", 7, selected=False),
        account_id="a2",
        peer_ref="-2001",
        thumbnail_key="a2:-2001:7:m7",
    )
    catalog.save_search_page("a2", other.id, other.generation, [other_result])

    thumbnails = ThumbnailCache(tmp_path / "thumbs", max_total_bytes=1024)
    cached_path = thumbnails.put(shared_key, b"cached")
    other_path = thumbnails.put(other_result.thumbnail_key, b"other")
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.thumbnail_values = {
        8: b"remote",
        9: TransientNetworkError("network detail"),
    }
    service = ContentBrowserService(
        catalog,
        thumbnails,
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: now,
    )
    await service.activate_account()

    assert await service.load_thumbnail(first_result.id) == cached_path
    assert gateway.thumbnail_calls == []
    downloaded = await service.load_thumbnail(remote_result.id)
    assert downloaded is not None and downloaded.read_bytes() == b"remote"
    assert await service.load_thumbnail(failed_result.id) is None
    assert catalog.get_result("a1", failed_result.id).available is True

    assert service.delete_history(first.id) is None
    assert cached_path is not None and cached_path.exists()
    assert service.delete_history(second.id) is None
    assert cached_path.exists() is False
    assert other_path is not None and other_path.exists()

    gateway.profile = AccountProfile("a2", "账号二")
    await service.activate_account()
    assert service.clear_history() is None
    assert other_path.exists() is False


@pytest.mark.asyncio
async def test_direct_hits_emit_before_blocked_album_expansion(tmp_path: Path) -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    service = await prepared_online_service(tmp_path, now, gateway)
    album_started = asyncio.Event()
    release_album = asyncio.Event()
    batches = []

    direct = make_hit(20, now)
    grouped = make_hit(19, now, grouped_id=900)
    gateway.pages = [RemoteSearchPage((direct, grouped), None, True)]

    async def expand_album(*_args):
        album_started.set()
        await release_album.wait()
        return (grouped, make_hit(18, now, grouped_id=900))

    gateway.expand_album = expand_album
    operation = asyncio.create_task(
        service.start_search("-1001", make_query(now), on_results=batches.append)
    )
    await album_started.wait()

    assert batches
    assert batches[0].stable is False
    assert {item.media_id for item in batches[0].results} == {"m20", "m19"}
    provisional_ids = {item.media_id: item.id for item in batches[0].results}

    release_album.set()
    session, results = await operation
    assert {item.media_id for item in results} == {"m20", "m19", "m18"}
    assert next(item.id for item in results if item.media_id == "m20") == provisional_ids[
        "m20"
    ]
    assert batches[-1].stable is True
    assert batches[-1].search_id == session.id


@pytest.mark.asyncio
async def test_search_catalog_work_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    service = await prepared_online_service(tmp_path, now, gateway)
    gateway.pages = [RemoteSearchPage((make_hit(1, now),), None, True)]
    entered = threading.Event()
    release = threading.Event()
    original = service.catalog.commit_search_page

    def slow_commit(*args, **kwargs):
        entered.set()
        release.wait(timeout=0.30)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.catalog, "commit_search_page", slow_commit)
    operation = asyncio.create_task(
        service.start_search("-1001", make_query(now))
    )
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        heartbeat = 0
        for _ in range(10):
            heartbeat += 1
            await asyncio.sleep(0.01)
        assert heartbeat == 10
        assert operation.done() is False
    finally:
        release.set()
    await operation


@pytest.mark.asyncio
async def test_global_provisional_batches_are_cumulative(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    service = await prepared_online_service(tmp_path, now, gateway)
    gateway.all_pages = [
        RemoteSearchPage(
            (make_hit(20, now),),
            SearchCursor(19, 1, "-1001"),
            False,
        ),
        RemoteSearchPage((make_hit(18, now),), None, True),
    ]
    batches: list[SearchResultBatch] = []

    await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        make_query(now),
        scope=SearchScope.ALL_DIALOGS,
        on_results=batches.append,
    )

    provisional = [batch for batch in batches if not batch.stable]
    assert [len(batch.results) for batch in provisional] == [1, 2]
    assert {item.message_id for item in provisional[-1].results} == {20, 18}


@pytest.mark.asyncio
async def test_load_search_snapshot_runs_through_background_boundary(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    service = await prepared_online_service(tmp_path, now, gateway)
    session = service.catalog.begin_search(
        "search-1",
        "a1",
        "-1001",
        "资料群",
        make_query(now),
        now,
    )
    service.catalog.save_search_page(
        "a1",
        session.id,
        session.generation,
        [make_saved_result(session.id, now, "result-1", 1)],
    )
    calls = 0
    original = service._run_blocking

    async def counted(operation):
        nonlocal calls
        calls += 1
        return await original(operation)

    service._run_blocking = counted
    snapshot = await service.load_search_snapshot("search-1")
    assert snapshot.session.id == "search-1"
    assert calls == 1


async def thumbnail_service(tmp_path: Path):
    now = datetime(2026, 8, 21, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [RemoteSearchPage((make_hit(10, now),), None, True)]
    service = await prepared_online_service(tmp_path, now, gateway)
    _session, results = await service.start_search("-1001", make_query(now))
    return service, gateway, results[0].id


@pytest.mark.asyncio
async def test_thumbnail_catalog_lookup_runs_through_background_boundary(
    tmp_path: Path,
) -> None:
    service, gateway, result_id = await thumbnail_service(tmp_path)
    gateway.thumbnail_values[10] = b"image"
    calls = 0
    original = service._run_blocking

    async def counted(operation):
        nonlocal calls
        calls += 1
        return await original(operation)

    service._run_blocking = counted
    assert await service.load_thumbnail(result_id) is not None
    assert calls == 1


@pytest.mark.asyncio
async def test_thumbnail_requests_share_one_gateway_call(tmp_path: Path) -> None:
    service, gateway, result_id = await thumbnail_service(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def load_thumbnail(*_args):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return b"image"

    gateway.load_thumbnail = load_thumbnail
    first = asyncio.create_task(service.load_thumbnail(result_id))
    await started.wait()
    second = asyncio.create_task(service.load_thumbnail(result_id))
    release.set()
    assert await first == await second
    assert calls == 1


@pytest.mark.asyncio
async def test_thumbnail_failure_cooldown_skips_immediate_retry(tmp_path: Path) -> None:
    service, gateway, result_id = await thumbnail_service(tmp_path)
    calls = 0

    async def missing(*_args):
        nonlocal calls
        calls += 1
        return None

    gateway.load_thumbnail = missing
    assert await service.load_thumbnail(result_id) is None
    assert await service.load_thumbnail(result_id) is None
    assert calls == 1
