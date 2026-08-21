from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    AccountProfile,
    ContentDialog,
    ContentSearchQuery,
    SearchCursor,
    SearchResult,
    SearchScope,
    SearchSession,
    SearchStatus,
)
from telegram_downloader.content_progress import (
    DialogSyncProgress,
    SearchProgress,
    SearchResultBatch,
)
from telegram_downloader.gateway import (
    AccessDeniedError,
    FloodWaitError,
    GatewayError,
    MediaReferenceExpired,
    RemoteMedia,
    RemoteSearchHit,
    RemoteSearchPage,
    TelegramGateway,
    TransientNetworkError,
)
from telegram_downloader.planner import EmptyScanError, ScanPreview, TaskPlanner
from telegram_downloader.thumbnail_cache import ThumbnailCache


@dataclass(frozen=True, slots=True)
class DownloadPreparation:
    preview: ScanPreview
    selected_count: int
    preview_result_ids: tuple[str, ...]
    duplicate_count: int
    unavailable_count: int


@dataclass(frozen=True, slots=True)
class QueueReport:
    selected_count: int
    joined_count: int
    duplicate_count: int
    unavailable_count: int
    queued_result_ids: tuple[str, ...]


class NothingToQueueError(ValueError):
    def __init__(
        self,
        selected_count: int,
        duplicate_count: int,
        unavailable_count: int,
    ) -> None:
        super().__init__("所选媒体均已入队或当前不可用")
        self.selected_count = selected_count
        self.duplicate_count = duplicate_count
        self.unavailable_count = unavailable_count


class ContentBrowserService:
    def __init__(
        self,
        catalog: CatalogRepository,
        thumbnails: ThumbnailCache,
        *,
        gateway: TelegramGateway | None = None,
        planner: TaskPlanner | None = None,
        uuid_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        album_concurrency: int = 4,
        thumbnail_concurrency: int = 4,
        thumbnail_failure_cooldown: float = 30.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if album_concurrency <= 0 or thumbnail_concurrency <= 0:
            raise ValueError("内容查询并发数必须大于零")
        if thumbnail_failure_cooldown < 0:
            raise ValueError("缩略图失败冷却时间不能为负数")
        self.gateway = gateway
        self.catalog = catalog
        self.planner = planner
        self.thumbnails = thumbnails
        self.uuid_factory = uuid_factory or (lambda: str(uuid4()))
        self.clock = clock or (lambda: datetime.now(UTC))
        self.account: AccountProfile | None = None
        self._last_dialog_sync_at: datetime | None = None
        self._sync_lock = asyncio.Lock()
        self._search_lock = asyncio.Lock()
        self._album_semaphore = asyncio.Semaphore(album_concurrency)
        self._thumbnail_semaphore = asyncio.Semaphore(thumbnail_concurrency)
        self._thumbnail_failure_cooldown = thumbnail_failure_cooldown
        self._monotonic_clock = monotonic_clock
        self._thumbnail_inflight: dict[str, asyncio.Task[Path | None]] = {}
        self._thumbnail_failures: dict[str, float] = {}

    @property
    def online(self) -> bool:
        return self.gateway is not None and self.planner is not None

    def bind_online(
        self,
        gateway: TelegramGateway,
        planner: TaskPlanner,
    ) -> None:
        self.gateway = gateway
        self.planner = planner

    def go_offline(self) -> None:
        self.gateway = None
        self.planner = None

    async def activate_cached_account(
        self,
    ) -> tuple[AccountProfile | None, list[ContentDialog]]:
        self.account = self.catalog.most_recent_account()
        self._restore_dialog_sync_time()
        return self.account, self.list_dialogs()

    async def activate_account(
        self,
    ) -> tuple[AccountProfile, list[ContentDialog]]:
        gateway, _planner = self._require_online()
        profile = await gateway.account_profile()
        now = self.clock()
        self.catalog.upsert_account(profile, now)
        self.catalog.recover_interrupted_searches(profile.account_id, now)
        self.account = profile
        self._restore_dialog_sync_time()
        return profile, self.list_dialogs()

    def list_dialogs(
        self,
        *,
        include_unavailable: bool = False,
    ) -> list[ContentDialog]:
        if self.account is None:
            return []
        return self.catalog.list_dialogs(
            self.account.account_id,
            include_unavailable=include_unavailable,
        )

    def list_sessions(self) -> list[SearchSession]:
        if self.account is None:
            return []
        return self.catalog.list_sessions(self.account.account_id)

    def latest_session(self, peer_ref: str) -> SearchSession | None:
        return next(
            (item for item in self.list_sessions() if item.peer_ref == peer_ref),
            None,
        )

    def dialog_cache_stale(self, max_age: timedelta) -> bool:
        if max_age.total_seconds() < 0:
            raise ValueError("缓存有效期不能为负数")
        synced_at = self._last_dialog_sync_at
        return synced_at is None or self.clock() - synced_at > max_age

    def list_results(self, search_id: str) -> list[SearchResult]:
        account = self._require_account()
        return self.catalog.list_results(account.account_id, search_id)

    def get_result(self, result_id: str) -> SearchResult:
        account = self._require_account()
        return self.catalog.get_result(account.account_id, result_id)

    async def sync_dialogs(
        self,
        *,
        on_progress: Callable[[DialogSyncProgress], None] | None = None,
    ) -> list[ContentDialog]:
        async with self._sync_lock:
            gateway, _planner = self._require_online()
            account = self._require_account()
            now = self.clock()
            dialogs: list[ContentDialog] = []
            async for item in gateway.iter_content_dialogs(account.account_id):
                dialogs.append(
                    replace(
                        item,
                        account_id=account.account_id,
                        last_synced_at=now,
                    )
                )
                if on_progress is not None:
                    on_progress(DialogSyncProgress(len(dialogs)))
            if self.account != account:
                return self.list_dialogs()
            self.catalog.replace_dialogs(account.account_id, dialogs, now)
            self._last_dialog_sync_at = now
            return self.catalog.list_dialogs(account.account_id)

    async def start_search(
        self,
        peer_ref: str,
        query: ContentSearchQuery,
        *,
        scope: SearchScope = SearchScope.SINGLE_DIALOG,
        on_progress: Callable[[SearchProgress], None] | None = None,
        on_results: Callable[[SearchResultBatch], None] | None = None,
    ) -> tuple[SearchSession, list[SearchResult]]:
        async with self._search_lock:
            account = self._require_account()
            self._require_online()
            if scope is SearchScope.ALL_DIALOGS:
                search_peer_ref = ALL_DIALOGS_SCOPE_REF
                dialog_title = ALL_DIALOGS_TITLE
            else:
                dialog = self.catalog.get_dialog(account.account_id, peer_ref)
                if not dialog.available:
                    raise ValueError("该群组或频道当前不可用")
                search_peer_ref = peer_ref
                dialog_title = dialog.title
            session = self.catalog.begin_search(
                self.uuid_factory(),
                account.account_id,
                search_peer_ref,
                dialog_title,
                query,
                self.clock(),
                scope=scope,
            )
            return await self._fetch_page(
                session,
                on_progress=on_progress,
                on_results=on_results,
            )

    async def load_more(
        self,
        search_id: str,
        *,
        on_progress: Callable[[SearchProgress], None] | None = None,
        on_results: Callable[[SearchResultBatch], None] | None = None,
    ) -> tuple[SearchSession, list[SearchResult]]:
        async with self._search_lock:
            account = self._require_account()
            self._require_online()
            session = self.catalog.get_session(account.account_id, search_id)
            if session.exhausted:
                results = self.catalog.list_results(account.account_id, search_id)
                if on_results is not None:
                    on_results(
                        SearchResultBatch(
                            session.id,
                            session.generation,
                            tuple(results),
                            stable=True,
                        )
                    )
                return session, results
            return await self._fetch_page(
                session,
                on_progress=on_progress,
                on_results=on_results,
            )

    async def _fetch_page(
        self,
        session: SearchSession,
        *,
        on_progress: Callable[[SearchProgress], None] | None = None,
        on_results: Callable[[SearchResultBatch], None] | None = None,
    ) -> tuple[SearchSession, list[SearchResult]]:
        account = self._require_account()
        gateway, planner = self._require_online()
        operation_accepted = 0
        progress_inspected = 0
        progress_matched = 0
        request_cursor = session.cursor
        seen_cursors = {request_cursor}
        stable_results = self.catalog.list_results(account.account_id, session.id)
        result_ids = {
            (item.peer_ref, item.message_id, item.media_id): item.id
            for item in stable_results
        }

        def result_for(
            hit: RemoteSearchHit,
            *,
            queued: bool = False,
        ) -> SearchResult:
            key = self._media_key(hit.remote)
            result = self._result_from_hit(
                account.account_id,
                session,
                hit,
                queued=queued,
                result_id=result_ids.get(key),
            )
            result_ids.setdefault(key, result.id)
            return result

        try:
            while True:
                page_inspected = 0
                page_matched = 0

                def report_progress(
                    progress: SearchProgress,
                    inspected_offset: int = progress_inspected,
                    matched_offset: int = progress_matched,
                ) -> None:
                    nonlocal page_inspected, page_matched
                    page_inspected = max(page_inspected, progress.inspected)
                    page_matched = max(page_matched, progress.matched)
                    if on_progress is not None:
                        on_progress(
                            SearchProgress(
                                inspected_offset + page_inspected,
                                matched_offset + page_matched,
                                progress.phase,
                            )
                        )

                page = await self._search_remote_page(
                    session,
                    request_cursor,
                    on_progress=report_progress if on_progress is not None else None,
                )
                progress_inspected += page_inspected
                progress_matched += page_matched
                if session.scope is SearchScope.ALL_DIALOGS and not page.exhausted:
                    if page.next_cursor is None or page.next_cursor in seen_cursors:
                        raise GatewayError("Telegram 全局搜索分页未前进")
                    seen_cursors.add(page.next_cursor)

                if on_results is not None and page.items:
                    on_results(
                        SearchResultBatch(
                            session.id,
                            session.generation,
                            tuple(
                                result_for(hit)
                                for hit in self._deduplicate_hits(list(page.items))
                            ),
                            stable=False,
                        )
                    )

                expanded = list(page.items)
                group_triggers: dict[tuple[str, int], int] = {}
                for hit in page.items:
                    grouped_id = hit.remote.grouped_id
                    if grouped_id is None:
                        continue
                    group_triggers.setdefault(
                        (hit.remote.peer_ref, grouped_id),
                        hit.remote.message_id,
                    )

                async def expand(
                    trigger: tuple[tuple[str, int], int],
                ) -> tuple[RemoteSearchHit, ...]:
                    (peer_ref, grouped_id), message_id = trigger
                    async with self._album_semaphore:
                        return await gateway.expand_album(
                            peer_ref,
                            message_id,
                            grouped_id,
                        )

                album_tasks = [
                    asyncio.create_task(expand(item))
                    for item in group_triggers.items()
                ]
                album_values = await asyncio.gather(*album_tasks)
                for values in album_values:
                    expanded.extend(values)

                unique = self._deduplicate_hits(expanded)
                existing_keys = {
                    (item.peer_ref, item.message_id, item.media_id)
                    for item in stable_results
                }
                units = self._album_units(unique)
                remaining_total = max(
                    0,
                    session.query.filters.item_limit - len(stable_results),
                )
                remaining_page = min(
                    100 - operation_accepted,
                    remaining_total,
                )
                accepted: list[RemoteSearchHit] = []
                deferred = False
                deferred_cursor: SearchCursor | None = None
                skipped_album = False
                for unit in units:
                    new_items = [
                        hit
                        for hit in unit
                        if self._media_key(hit.remote) not in existing_keys
                    ]
                    if not new_items:
                        continue
                    is_album = new_items[0].remote.grouped_id is not None
                    if len(new_items) > remaining_total:
                        skipped_album = skipped_album or is_album
                        continue
                    if len(new_items) > remaining_page:
                        grouped_id = new_items[0].remote.grouped_id
                        trigger = (
                            group_triggers.get(
                                (new_items[0].remote.peer_ref, grouped_id),
                                new_items[0].remote.message_id,
                            )
                            if grouped_id is not None
                            else new_items[0].remote.message_id
                        )
                        deferred = True
                        deferred_cursor = (
                            request_cursor
                            if session.scope is SearchScope.ALL_DIALOGS
                            else SearchCursor(trigger + 1)
                        )
                        break
                    accepted.extend(new_items)
                    remaining_total -= len(new_items)
                    remaining_page -= len(new_items)
                    existing_keys.update(
                        self._media_key(hit.remote) for hit in new_items
                    )

                queued_keys = planner.existing_media_keys(
                    {self._media_key(hit.remote) for hit in accepted}
                )
                saved = [
                    result_for(
                        hit,
                        queued=self._media_key(hit.remote) in queued_keys,
                    )
                    for hit in accepted
                ]
                operation_accepted += len(accepted)
                projected_count = len(stable_results) + len(accepted)
                reached_limit = (
                    projected_count >= session.query.filters.item_limit
                )
                complete = reached_limit or skipped_album or (
                    not deferred and (page.exhausted or page.next_cursor is None)
                )
                cursor = (
                    None
                    if complete
                    else deferred_cursor
                    if deferred
                    else page.next_cursor
                )
                commit = self.catalog.commit_search_page(
                    account.account_id,
                    session.id,
                    session.generation,
                    saved,
                    cursor=cursor,
                    complete=complete,
                    finished_at=self.clock(),
                    status=(
                        SearchStatus.COMPLETED if complete else SearchStatus.RUNNING
                    ),
                    error="达到数量上限" if skipped_album else None,
                )
                session = commit.session
                stable_results = list(commit.results)
                reached_limit = (
                    commit.result_count >= session.query.filters.item_limit
                )
                if (
                    session.scope is SearchScope.SINGLE_DIALOG
                    or session.exhausted
                    or reached_limit
                    or operation_accepted >= 100
                    or deferred
                ):
                    break
                request_cursor = session.cursor
        except asyncio.CancelledError:
            current = self.catalog.get_session(account.account_id, session.id)
            self._finish_incomplete(current, "搜索已取消")
            raise
        except GatewayError as error:
            current = self.catalog.get_session(account.account_id, session.id)
            self._finish_incomplete(current, self._safe_gateway_error(error))
            raise
        current = session
        results = stable_results
        if on_results is not None:
            on_results(
                SearchResultBatch(
                    current.id,
                    current.generation,
                    tuple(results),
                    stable=True,
                )
            )
        return current, results

    async def _search_remote_page(
        self,
        session: SearchSession,
        cursor: SearchCursor | None,
        *,
        on_progress: Callable[[SearchProgress], None] | None,
    ) -> RemoteSearchPage:
        gateway, _planner = self._require_online()
        if session.scope is SearchScope.ALL_DIALOGS:
            return await gateway.search_all_media_page(
                session.query,
                cursor,
                on_progress=on_progress,
            )
        return await gateway.search_media_page(
            session.peer_ref,
            session.query,
            cursor,
            on_progress=on_progress,
        )

    def set_selected(
        self,
        search_id: str,
        result_id: str,
        selected: bool,
    ) -> list[SearchResult]:
        account = self._require_account()
        self.catalog.set_selected(
            account.account_id,
            search_id,
            result_id,
            selected,
        )
        return self.catalog.list_results(account.account_id, search_id)

    def select_all(self, search_id: str) -> list[SearchResult]:
        account = self._require_account()
        for item in self.catalog.list_results(account.account_id, search_id):
            if item.available and not item.queued and not item.selected:
                self.catalog.set_selected(
                    account.account_id,
                    search_id,
                    item.id,
                    True,
                )
        return self.catalog.list_results(account.account_id, search_id)

    def invert_selection(self, search_id: str) -> list[SearchResult]:
        account = self._require_account()
        for item in self.catalog.list_results(account.account_id, search_id):
            if item.available and not item.queued:
                self.catalog.set_selected(
                    account.account_id,
                    search_id,
                    item.id,
                    not item.selected,
                )
        return self.catalog.list_results(account.account_id, search_id)

    def prepare_download(self, search_id: str) -> DownloadPreparation:
        account = self._require_account()
        planner = self._require_planner()
        session = self.catalog.get_session(account.account_id, search_id)
        selected = self.catalog.list_results(
            account.account_id,
            search_id,
            selected_only=True,
        )
        unavailable = [item for item in selected if not item.available]
        queued = [item for item in selected if item.queued]
        candidates = [
            item for item in selected if item.available and not item.queued
        ]
        keys = {self._result_key(item) for item in candidates}
        existing = planner.existing_media_keys(keys)
        remaining = [
            item for item in candidates if self._result_key(item) not in existing
        ]
        initial_duplicates = len(queued) + len(candidates) - len(remaining)
        if not remaining:
            raise NothingToQueueError(
                len(selected),
                initial_duplicates,
                len(unavailable),
            )
        remote = [self._remote_from_result(session, item) for item in remaining]
        try:
            if session.scope is SearchScope.ALL_DIALOGS:
                preview = planner.plan_account_search(session.query, remote)
            else:
                preview = planner.plan_selected(
                    session.peer_ref,
                    session.dialog_title,
                    session.query,
                    remote,
                )
        except EmptyScanError as error:
            raise NothingToQueueError(
                len(selected),
                len(queued) + len(candidates),
                len(unavailable),
            ) from error
        preview_keys = {
            (item.peer_ref, item.message_id, item.media_id)
            for item in preview.items
        }
        result_ids = tuple(
            item.id for item in remaining if self._result_key(item) in preview_keys
        )
        duplicate_count = len(queued) + len(candidates) - len(preview_keys)
        return DownloadPreparation(
            preview,
            len(selected),
            result_ids,
            duplicate_count,
            len(unavailable),
        )

    def finalize_queue(self, search_id: str, joined_count: int) -> QueueReport:
        if joined_count < 0:
            raise ValueError("加入数量不能为负数")
        account = self._require_account()
        planner = self._require_planner()
        selected = self.catalog.list_results(
            account.account_id,
            search_id,
            selected_only=True,
        )
        available = [item for item in selected if item.available]
        existing = planner.existing_media_keys(
            {self._result_key(item) for item in available}
        )
        queued_ids = tuple(
            item.id for item in available if self._result_key(item) in existing
        )
        self.catalog.mark_queued(account.account_id, queued_ids)
        return QueueReport(
            selected_count=len(selected),
            joined_count=joined_count,
            duplicate_count=max(0, len(available) - joined_count),
            unavailable_count=len(selected) - len(available),
            queued_result_ids=queued_ids,
        )

    async def load_thumbnail(self, result_id: str) -> Path | None:
        account = self._require_account()
        result = self.catalog.get_result(account.account_id, result_id)
        key = result.thumbnail_key
        cached = self.thumbnails.get(key)
        if cached is not None:
            self._thumbnail_failures.pop(key, None)
            return cached
        now = self._monotonic_clock()
        failure_deadline = self._thumbnail_failures.get(key)
        if failure_deadline is not None:
            if now < failure_deadline:
                return None
            self._thumbnail_failures.pop(key, None)
        gateway = self.gateway
        if gateway is None:
            return None

        task = self._thumbnail_inflight.get(key)
        if task is None:
            async def load_once() -> Path | None:
                current = asyncio.current_task()
                try:
                    return await self._load_thumbnail_remote(
                        key,
                        result.peer_ref,
                        result.message_id,
                        result.media_id,
                        gateway,
                    )
                finally:
                    if self._thumbnail_inflight.get(key) is current:
                        self._thumbnail_inflight.pop(key, None)

            task = asyncio.create_task(load_once())
            self._thumbnail_inflight[key] = task
        return await asyncio.shield(task)

    async def _load_thumbnail_remote(
        self,
        key: str,
        peer_ref: str,
        message_id: int,
        media_id: str,
        gateway: TelegramGateway,
    ) -> Path | None:
        try:
            async with self._thumbnail_semaphore:
                content = await gateway.load_thumbnail(
                    peer_ref,
                    message_id,
                    media_id,
                )
            if not content:
                self._record_thumbnail_failure(key)
                return None
            cached = await asyncio.to_thread(self.thumbnails.put, key, content)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_thumbnail_failure(key)
            return None
        self._thumbnail_failures.pop(key, None)
        return cached

    def _record_thumbnail_failure(self, key: str) -> None:
        self._thumbnail_failures[key] = (
            self._monotonic_clock() + self._thumbnail_failure_cooldown
        )

    def delete_history(self, search_id: str) -> str | None:
        account = self._require_account()
        keys = self.catalog.list_thumbnail_keys(account.account_id, search_id)
        self.catalog.delete_session(account.account_id, search_id)
        return self._cleanup_unreferenced(account.account_id, keys)

    def clear_history(self) -> str | None:
        account = self._require_account()
        keys = self.catalog.list_thumbnail_keys(account.account_id)
        self.catalog.clear_history(account.account_id)
        return self._cleanup_unreferenced(account.account_id, keys)

    def _cleanup_unreferenced(
        self,
        account_id: str,
        keys: set[str],
    ) -> str | None:
        referenced = self.catalog.referenced_thumbnail_keys(account_id, keys)
        try:
            for key in keys - referenced:
                self.thumbnails.delete(key)
        except Exception:
            return "搜索记录已删除，但缩略图缓存清理失败"
        return None

    def _finish_incomplete(self, session: SearchSession, error: str) -> None:
        self.catalog.finish_search(
            session.account_id,
            session.id,
            session.generation,
            session.cursor,
            False,
            self.clock(),
            status=SearchStatus.INCOMPLETE,
            error=error,
        )

    def _restore_dialog_sync_time(self) -> None:
        dialogs = self.list_dialogs(include_unavailable=True)
        self._last_dialog_sync_at = max(
            (item.last_synced_at for item in dialogs),
            default=None,
        )

    def _require_account(self) -> AccountProfile:
        if self.account is None:
            raise ValueError("尚未选择 Telegram 账号")
        return self.account

    def _require_online(self) -> tuple[TelegramGateway, TaskPlanner]:
        if self.gateway is None or self.planner is None:
            raise ValueError("请先连接 Telegram 账号")
        return self.gateway, self.planner

    def _require_planner(self) -> TaskPlanner:
        if self.planner is None:
            raise ValueError("请先连接 Telegram 账号")
        return self.planner

    @staticmethod
    def _media_key(remote: RemoteMedia) -> tuple[str, int, str]:
        return remote.peer_ref, remote.message_id, remote.media_id

    @staticmethod
    def _result_key(result: SearchResult) -> tuple[str, int, str]:
        return result.peer_ref, result.message_id, result.media_id

    @staticmethod
    def _hit_sort_key(hit: RemoteSearchHit) -> tuple[float, str, int, str]:
        return (
            -hit.remote.message_date_utc.timestamp(),
            hit.remote.peer_ref,
            -hit.remote.message_id,
            hit.remote.media_id,
        )

    @classmethod
    def _deduplicate_hits(
        cls,
        hits: list[RemoteSearchHit],
    ) -> list[RemoteSearchHit]:
        unique: dict[tuple[str, int, str], RemoteSearchHit] = {}
        for hit in hits:
            unique.setdefault(cls._media_key(hit.remote), hit)
        return sorted(unique.values(), key=cls._hit_sort_key)

    @classmethod
    def _album_units(
        cls,
        hits: list[RemoteSearchHit],
    ) -> list[list[RemoteSearchHit]]:
        albums: dict[tuple[str, int], list[RemoteSearchHit]] = {}
        for hit in hits:
            grouped_id = hit.remote.grouped_id
            if grouped_id is not None:
                albums.setdefault((hit.remote.peer_ref, grouped_id), []).append(hit)
        units: list[list[RemoteSearchHit]] = []
        emitted: set[tuple[str, int]] = set()
        for hit in hits:
            grouped_id = hit.remote.grouped_id
            if grouped_id is None:
                units.append([hit])
            else:
                album_key = (hit.remote.peer_ref, grouped_id)
                if album_key in emitted:
                    continue
                emitted.add(album_key)
                units.append(sorted(albums[album_key], key=cls._hit_sort_key))
        return units

    @staticmethod
    def _result_from_hit(
        account_id: str,
        session: SearchSession,
        hit: RemoteSearchHit,
        *,
        queued: bool,
        result_id: str | None = None,
    ) -> SearchResult:
        remote = hit.remote
        key = f"{session.id}:{remote.peer_ref}:{remote.message_id}:{remote.media_id}"
        return SearchResult(
            id=result_id or str(uuid5(NAMESPACE_URL, key)),
            search_id=session.id,
            account_id=account_id,
            peer_ref=remote.peer_ref,
            message_id=remote.message_id,
            grouped_id=remote.grouped_id,
            media_id=remote.media_id,
            media_kind=remote.kind,
            original_name=remote.original_name,
            expected_size=remote.expected_size,
            message_date_utc=remote.message_date_utc,
            excerpt=hit.excerpt,
            thumbnail_key=(
                f"{account_id}:{remote.peer_ref}:"
                f"{remote.message_id}:{remote.media_id}"
            ),
            selected=False,
            available=True,
            queued=queued,
            source_title=remote.source_title,
            source_kind=remote.source_kind,
        )

    @staticmethod
    def _remote_from_result(
        session: SearchSession,
        result: SearchResult,
    ) -> RemoteMedia:
        return RemoteMedia(
            peer_ref=result.peer_ref,
            source_title=result.source_title or session.dialog_title,
            message_id=result.message_id,
            grouped_id=result.grouped_id,
            media_id=result.media_id,
            kind=result.media_kind,
            original_name=result.original_name,
            expected_size=result.expected_size,
            message_date_utc=result.message_date_utc,
            source_kind=result.source_kind,
        )

    @staticmethod
    def _safe_gateway_error(error: GatewayError) -> str:
        if isinstance(error, TransientNetworkError):
            return "Telegram 网络连接失败"
        if isinstance(error, AccessDeniedError):
            return "当前账号无权访问该群组或频道"
        if isinstance(error, FloodWaitError):
            return f"Telegram 请求需等待 {error.seconds} 秒"
        if isinstance(error, MediaReferenceExpired):
            return "媒体引用已过期"
        return f"Telegram 搜索失败（{type(error).__name__}）"
