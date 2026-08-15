from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import AccountProfile
from telegram_downloader.gateway import RemoteMedia, TelegramGateway
from telegram_downloader.planner import EmptyScanError, TaskPlanner
from telegram_downloader.subscriptions import (
    SubscriptionDraft,
    SubscriptionProgress,
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionRunReport,
    SubscriptionRunStatus,
    SubscriptionState,
)


class SubscriptionUnavailableError(RuntimeError):
    pass


class SubscriptionService:
    PAGE_LIMIT = 500

    def __init__(
        self,
        catalog: CatalogRepository,
        *,
        uuid_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.catalog = catalog
        self.uuid_factory = uuid_factory or (lambda: str(uuid4()))
        self.clock = clock or (lambda: datetime.now(UTC))
        self.gateway: TelegramGateway | None = None
        self.planner: TaskPlanner | None = None
        self.account: AccountProfile | None = None

    @property
    def online(self) -> bool:
        return self.gateway is not None and self.planner is not None

    def bind_online(self, gateway: TelegramGateway, planner: TaskPlanner) -> None:
        self.gateway = gateway
        self.planner = planner

    def go_offline(self) -> None:
        self.gateway = None
        self.planner = None

    def set_account(self, account: AccountProfile | None) -> None:
        self.account = account

    def list_rules(self) -> list[SubscriptionRule]:
        account = self._require_account()
        return self.catalog.list_subscriptions(account.account_id)

    def list_due_rules(self, now: datetime) -> list[SubscriptionRule]:
        account = self._require_account()
        return self.catalog.list_due_subscriptions(account.account_id, now)

    def get_rule(self, rule_id: str) -> SubscriptionRule:
        account = self._require_account()
        return self.catalog.get_subscription(account.account_id, rule_id)

    async def create_rule(self, draft: SubscriptionDraft) -> SubscriptionRule:
        account = self._require_account()
        gateway, _planner = self._require_online()
        dialog = self.catalog.get_dialog(account.account_id, draft.peer_ref)
        if not dialog.available:
            raise SubscriptionUnavailableError("该群组或频道当前不可用")
        now = self.clock()
        rule = SubscriptionRule(
            id=self.uuid_factory(),
            account_id=account.account_id,
            peer_ref=dialog.peer_ref,
            dialog_title=dialog.title,
            keyword=draft.keyword,
            media_kinds=draft.media_kinds,
            interval_minutes=draft.interval_minutes,
            enabled=True,
            state=SubscriptionState.BASELINING,
            last_message_id=None,
            next_run_at=None,
            last_run_at=None,
            last_error=None,
            failure_count=0,
            created_at=now,
            updated_at=now,
        )
        self.catalog.save_subscription(rule)
        baseline = await gateway.latest_message_id(dialog.peer_ref)
        ready = replace(
            rule,
            state=SubscriptionState.WAITING,
            last_message_id=baseline,
            next_run_at=now + timedelta(minutes=draft.interval_minutes),
            updated_at=self.clock(),
        )
        self.catalog.save_subscription(ready)
        return ready

    async def update_rule(
        self,
        rule_id: str,
        draft: SubscriptionDraft,
    ) -> SubscriptionRule:
        account = self._require_account()
        gateway, _planner = self._require_online()
        current = self.catalog.get_subscription(account.account_id, rule_id)
        dialog = self.catalog.get_dialog(account.account_id, draft.peer_ref)
        if not dialog.available:
            raise SubscriptionUnavailableError("该群组或频道当前不可用")
        reset = (
            current.peer_ref != draft.peer_ref
            or current.normalized_keyword != draft.normalized_keyword
        )
        baseline = (
            await gateway.latest_message_id(draft.peer_ref)
            if reset
            else current.last_message_id
        )
        now = self.clock()
        updated = replace(
            current,
            peer_ref=draft.peer_ref,
            dialog_title=dialog.title,
            keyword=draft.keyword,
            media_kinds=draft.media_kinds,
            interval_minutes=draft.interval_minutes,
            enabled=True,
            state=SubscriptionState.WAITING,
            last_message_id=baseline,
            next_run_at=now + timedelta(minutes=draft.interval_minutes),
            last_error=None,
            failure_count=0,
            updated_at=now,
        )
        self.catalog.save_subscription(updated)
        return updated

    def set_enabled(self, rule_id: str, enabled: bool) -> SubscriptionRule:
        current = self.get_rule(rule_id)
        now = self.clock()
        updated = replace(
            current,
            enabled=enabled,
            state=(SubscriptionState.WAITING if enabled else SubscriptionState.PAUSED),
            next_run_at=now if enabled else None,
            last_error=None if enabled else current.last_error,
            updated_at=now,
        )
        self.catalog.save_subscription(updated)
        return updated

    def delete_rule(self, rule_id: str) -> None:
        account = self._require_account()
        self.catalog.delete_subscription(account.account_id, rule_id)

    def update_runtime(
        self,
        rule_id: str,
        *,
        state: SubscriptionState,
        next_run_at: datetime | None,
        last_run_at: datetime | None,
        last_error: str | None,
        failure_count: int,
    ) -> None:
        account = self._require_account()
        self.catalog.update_subscription_runtime(
            account.account_id,
            rule_id,
            state=state,
            next_run_at=next_run_at,
            last_run_at=last_run_at,
            last_error=last_error,
            failure_count=failure_count,
            now=self.clock(),
        )

    async def run_rule(
        self,
        rule_id: str,
        *,
        on_progress: Callable[[SubscriptionProgress], None] | None = None,
    ) -> SubscriptionRunReport:
        account = self._require_account()
        gateway, planner = self._require_online()
        rule = self.catalog.get_subscription(account.account_id, rule_id)
        if not rule.enabled:
            raise SubscriptionUnavailableError("订阅规则已暂停")

        started_at = self.clock()
        inspected = 0
        matched = 0
        queued = 0
        duplicate = 0
        task_ids: tuple[str, ...] = ()
        last_processed_id = rule.last_message_id or 0
        has_more = False
        self.update_runtime(
            rule_id,
            state=SubscriptionState.RUNNING,
            next_run_at=None,
            last_run_at=started_at,
            last_error=None,
            failure_count=rule.failure_count,
        )
        self._progress(on_progress, rule_id, 0, 0, 0, 0, "正在读取新消息")
        try:
            snapshot_id = await gateway.latest_message_id(rule.peer_ref)
            if rule.last_message_id is None:
                last_processed_id = snapshot_id
                report = self._complete_run(
                    rule,
                    started_at,
                    last_processed_id,
                    inspected,
                    matched,
                    queued,
                    duplicate,
                    task_ids,
                    False,
                )
                self._progress(
                    on_progress,
                    rule_id,
                    0,
                    0,
                    0,
                    0,
                    "基线建立完成",
                )
                return report

            after_id = rule.last_message_id
            messages = await gateway.incremental_messages(
                rule.peer_ref,
                after_id=after_id,
                through_id=max(after_id, snapshot_id),
                limit=self.PAGE_LIMIT,
            )
            inspected = len(messages)
            remotes: list[RemoteMedia] = []
            expanded_groups: set[int] = set()
            for index, item in enumerate(messages, start=1):
                if rule.normalized_keyword not in self._normalize(item.text):
                    self._progress(
                        on_progress,
                        rule_id,
                        index,
                        len(remotes),
                        queued,
                        duplicate,
                        "正在筛选新消息",
                    )
                    continue
                if item.media is None:
                    continue
                if item.grouped_id is not None:
                    if item.grouped_id in expanded_groups:
                        continue
                    expanded_groups.add(item.grouped_id)
                    album = await gateway.expand_album(
                        rule.peer_ref,
                        item.message_id,
                        item.grouped_id,
                    )
                    remotes.extend(
                        hit.remote
                        for hit in album
                        if hit.remote.kind in rule.media_kinds
                    )
                elif item.media.kind in rule.media_kinds:
                    remotes.append(item.media)
                self._progress(
                    on_progress,
                    rule_id,
                    index,
                    len(remotes),
                    queued,
                    duplicate,
                    "正在筛选新消息",
                )

            unique = self._deduplicate(remotes)
            matched = len(unique)
            keys = {self._media_key(item) for item in unique}
            existing = planner.existing_media_keys(keys)
            duplicate = len(existing)
            pending = [item for item in unique if self._media_key(item) not in existing]
            if pending:
                preview = planner.plan_subscription(
                    rule.peer_ref,
                    rule.dialog_title,
                    rule.keyword,
                    pending,
                )
                try:
                    committed = planner.commit_selected(preview)
                except EmptyScanError:
                    duplicate += len(pending)
                else:
                    queued = len(committed.accepted_keys)
                    duplicate += committed.skipped_count
                    task_ids = (committed.task.id,)

            last_seen = messages[-1].message_id if messages else snapshot_id
            has_more = len(messages) >= self.PAGE_LIMIT and last_seen < snapshot_id
            last_processed_id = last_seen if has_more else snapshot_id
            report = self._complete_run(
                rule,
                started_at,
                last_processed_id,
                inspected,
                matched,
                queued,
                duplicate,
                task_ids,
                has_more,
            )
            self._progress(
                on_progress,
                rule_id,
                inspected,
                matched,
                queued,
                duplicate,
                "检查完成",
            )
            return report
        except asyncio.CancelledError:
            self._save_failed_run(
                rule,
                started_at,
                SubscriptionRunStatus.CANCELLED,
                inspected,
                matched,
                queued,
                duplicate,
                "CancelledError",
            )
            raise
        except Exception as error:
            self._save_failed_run(
                rule,
                started_at,
                SubscriptionRunStatus.FAILED,
                inspected,
                matched,
                queued,
                duplicate,
                type(error).__name__,
            )
            raise

    def _complete_run(
        self,
        rule: SubscriptionRule,
        started_at: datetime,
        last_processed_id: int,
        inspected: int,
        matched: int,
        queued: int,
        duplicate: int,
        task_ids: tuple[str, ...],
        has_more: bool,
    ) -> SubscriptionRunReport:
        finished_at = self.clock()
        self.catalog.advance_subscription(
            rule.account_id,
            rule.id,
            last_processed_id,
            finished_at,
        )
        run = SubscriptionRun(
            self.uuid_factory(),
            rule.id,
            rule.account_id,
            started_at,
            finished_at,
            SubscriptionRunStatus.COMPLETED,
            inspected,
            matched,
            queued,
            duplicate,
        )
        self.catalog.save_subscription_run(run)
        self.catalog.update_subscription_runtime(
            rule.account_id,
            rule.id,
            state=SubscriptionState.WAITING,
            next_run_at=(
                finished_at + timedelta(seconds=5)
                if has_more
                else finished_at + timedelta(minutes=rule.interval_minutes)
            ),
            last_run_at=finished_at,
            last_error=None,
            failure_count=0,
            now=finished_at,
        )
        return SubscriptionRunReport(run, task_ids, last_processed_id, has_more)

    def _save_failed_run(
        self,
        rule: SubscriptionRule,
        started_at: datetime,
        status: SubscriptionRunStatus,
        inspected: int,
        matched: int,
        queued: int,
        duplicate: int,
        error: str,
    ) -> None:
        finished_at = self.clock()
        self.catalog.save_subscription_run(
            SubscriptionRun(
                self.uuid_factory(),
                rule.id,
                rule.account_id,
                started_at,
                finished_at,
                status,
                inspected,
                matched,
                queued,
                duplicate,
                error,
            )
        )

    def _require_account(self) -> AccountProfile:
        if self.account is None:
            raise SubscriptionUnavailableError("尚未选择 Telegram 账号")
        return self.account

    def _require_online(self) -> tuple[TelegramGateway, TaskPlanner]:
        if self.gateway is None or self.planner is None:
            raise SubscriptionUnavailableError("Telegram 当前未连接")
        return self.gateway, self.planner

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _media_key(item: RemoteMedia) -> tuple[str, int, str]:
        return item.peer_ref, item.message_id, item.media_id

    @classmethod
    def _deduplicate(cls, values: list[RemoteMedia]) -> list[RemoteMedia]:
        found: dict[tuple[str, int, str], RemoteMedia] = {}
        for item in values:
            found.setdefault(cls._media_key(item), item)
        return sorted(
            found.values(),
            key=lambda item: (item.message_date_utc, item.message_id, item.media_id),
            reverse=True,
        )

    @staticmethod
    def _progress(
        callback: Callable[[SubscriptionProgress], None] | None,
        rule_id: str,
        inspected: int,
        matched: int,
        queued: int,
        duplicate: int,
        phase: str,
    ) -> None:
        if callback is not None:
            callback(
                SubscriptionProgress(
                    rule_id,
                    inspected,
                    matched,
                    queued,
                    duplicate,
                    phase,
                )
            )
