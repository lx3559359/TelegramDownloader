from datetime import UTC, datetime
from itertools import count
from pathlib import Path

import pytest

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    AccountProfile,
    ContentSearchQuery,
    ContentSourceKind,
    SearchScope,
)
from telegram_downloader.content_browser import (
    ContentBrowserService,
    NothingToQueueError,
)
from telegram_downloader.content_progress import SearchProgress
from telegram_downloader.domain import MediaKind, ScanFilters, SourceKind
from telegram_downloader.gateway import RemoteMedia, RemoteSearchHit, RemoteSearchPage
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.thumbnail_cache import ThumbnailCache


def id_factory():
    sequence = count(1)
    return lambda: f"id-{next(sequence)}"


def query(now: datetime) -> ContentSearchQuery:
    return ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 20),
    )


class AccountWideGateway:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.global_calls = 0

    async def account_profile(self) -> AccountProfile:
        return AccountProfile("a1", "账号")

    async def search_all_media_page(
        self,
        received_query,
        cursor,
        *,
        on_progress=None,
    ) -> RemoteSearchPage:
        assert cursor is None
        assert received_query.keyword == "安装"
        self.global_calls += 1
        values = (
            RemoteSearchHit(
                RemoteMedia(
                    "-1001",
                    "资料群",
                    10,
                    None,
                    "m10",
                    MediaKind.VIDEO,
                    "group.mp4",
                    12,
                    self.now,
                    source_kind=ContentSourceKind.GROUP,
                ),
                "群组安装资源",
                "-1001:10:m10",
            ),
            RemoteSearchHit(
                RemoteMedia(
                    "42",
                    "联系人",
                    11,
                    None,
                    "m11",
                    MediaKind.VIDEO,
                    "private.mp4",
                    13,
                    self.now,
                    source_kind=ContentSourceKind.PRIVATE,
                ),
                "私聊安装资源",
                "42:11:m11",
            ),
        )
        if on_progress is not None:
            on_progress(SearchProgress(2, 2, "正在整理结果"))
        return RemoteSearchPage(values, None, True)

    async def search_media_page(self, peer_ref, received_query, cursor, **kwargs):
        raise AssertionError("全账号搜索不应调用单会话接口")

    async def expand_album(self, peer_ref, message_id, grouped_id):
        return ()

    async def load_thumbnail(self, peer_ref, message_id, media_id):
        return None


@pytest.mark.asyncio
async def test_account_wide_search_persists_sources_and_commits_one_task(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    catalog = CatalogRepository(tmp_path / "data/database/catalog.sqlite3")
    tasks = TaskRepository(tmp_path / "data/database/tasks.sqlite3")
    catalog.initialize()
    tasks.initialize()
    gateway = AccountWideGateway(now)
    planner = TaskPlanner(
        gateway,
        tasks,
        tmp_path / "downloads",
        uuid_factory=id_factory(),
        clock=lambda: now,
    )
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "data/cache/thumbnails"),
        gateway=gateway,
        planner=planner,
        uuid_factory=iter(("search-1",)).__next__,
        clock=lambda: now,
    )
    await service.activate_account()

    session, results = await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        query(now),
        scope=SearchScope.ALL_DIALOGS,
    )
    service.select_all(session.id)
    preparation = service.prepare_download(session.id)
    committed = planner.commit_selected(preparation.preview)
    report = service.finalize_queue(session.id, len(committed.accepted_keys))

    assert gateway.global_calls == 1
    assert len(results) == 2
    assert report.joined_count == 2
    task = tasks.get_task(committed.task.id)
    items = tasks.list_items(task.id)
    assert task.source_kind is SourceKind.ACCOUNT_SEARCH
    assert task.display_title == "全部会话（搜索：安装）"
    assert {item.peer_ref for item in items} == {"-1001", "42"}
    assert {item.target_path.parts[-4] for item in items} == {"资料群", "联系人"}

    reopened = CatalogRepository(catalog.database)
    reopened.initialize()
    restored = reopened.get_session("a1", session.id)
    assert restored.scope is SearchScope.ALL_DIALOGS
    assert all(item.queued for item in reopened.list_results("a1", session.id))

    with pytest.raises(NothingToQueueError):
        service.prepare_download(session.id)
