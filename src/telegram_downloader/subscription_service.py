from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import AccountProfile
from telegram_downloader.gateway import RemoteMedia, RemoteMessage, TelegramGateway
from telegram_downloader.planner import EmptyScanError, TaskPlanner
from telegram_downloader.subscriptions import (
    SubscriptionDraft,
    SubscriptionProbeProgress,
    SubscriptionProbeReport,
    SubscriptionProbeSample,
    SubscriptionProgress,
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionRunReport,
    SubscriptionRunStatus,
    SubscriptionState,
)


class SubscriptionUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _MatchedCandidate:
    remote: RemoteMedia
    message_id: int
    message_date_utc: datetime
    excerpt: str


@dataclass(frozen=True, slots=True)
class _MatchResult:
    inspected: int
    keyword_hits: int
    candidates: tuple[_MatchedCandidate, ...]


@dataclass(frozen=True, slots=True)
class _MatchStep:
    inspected: int
    keyword_hits: int
    matched: int
    phase: str


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

    def latest_runs(self) -> dict[str, SubscriptionRun]:
        account = self._require_account()
        return self.catalog.latest_subscription_runs(account.account_id)

    def snapshot(
        self,
    ) -> tuple[
        tuple[SubscriptionRule, ...],
        tuple[tuple[str, SubscriptionRun], ...],
    ]:
        account = self._require_account()
        rules = tuple(self.catalog.list_subscriptions(account.account_id))
        latest_runs = tuple(self.catalog.latest_subscription_runs(account.account_id).items())
        return rules, latest_runs

    def list_runs(
        self,
        rule_id: str,
        *,
        limit: int = 20,
    ) -> list[SubscriptionRun]:
        if not 1 <= limit <= 100:
            raise ValueError("订阅运行记录数量必须在 1 到 100 之间")
        account = self._require_account()
        self.catalog.get_subscription(account.account_id, rule_id)
        return self.catalog.list_subscription_runs(account.account_id, rule_id)[:limit]

    def resume_after_connection(self) -> int:
        account = self._require_account()
        return self.catalog.resume_connection_blocked_subscriptions(
            account.account_id,
            self.clock(),
        )

    def get_rule(self, rule_id: str) -> SubscriptionRule:
        account = self._require_account()
        return self.catalog.get_subscription(account.account_id, rule_id)

    async def create_rule(self, draft: SubscriptionDraft) -> SubscriptionRule:
        account = self._require_account()
        self._require_online()
        dialog = self.catalog.get_dialog(account.account_id, draft.peer_ref)
        if not dialog.available:
            raise SubscriptionUnavailableError("该群组或频道当前不可用")
        now = self.clock()
        rule = SubscriptionRule(
            id=self.uuid_factory(),
            account_id=account.account_id,
            peer_ref=dialog.peer_ref,
            dialog_title=dialog.title,
            criteria=draft.criteria,
            media_kinds=draft.media_kinds,
            interval_minutes=draft.interval_minutes,
            history_days=draft.history_days,
            enabled=True,
            state=SubscriptionState.BASELINING,
            last_message_id=None,
            backfill_from_utc=self._history_cutoff(draft.history_days, now),
            backfill_through_id=None,
            next_run_at=None,
            last_run_at=None,
            last_error=None,
            failure_count=0,
            created_at=now,
            updated_at=now,
        )
        self.catalog.save_subscription(rule)
        try:
            return await self._establish_baseline(rule)
        except Exception as error:
            failed_at = self.clock()
            self.catalog.save_subscription(
                replace(
                    rule,
                    state=SubscriptionState.FAILED,
                    next_run_at=failed_at,
                    last_error=f"自动建立基线失败（{type(error).__name__}）",
                    failure_count=1,
                    updated_at=failed_at,
                )
            )
            raise

    async def update_rule(
        self,
        rule_id: str,
        draft: SubscriptionDraft,
    ) -> SubscriptionRule:
        account = self._require_account()
        self._require_online()
        current = self.catalog.get_subscription(account.account_id, rule_id)
        dialog = self.catalog.get_dialog(account.account_id, draft.peer_ref)
        if not dialog.available:
            raise SubscriptionUnavailableError("该群组或频道当前不可用")
        now = self.clock()
        enabled = current.enabled
        reset = self._requires_rebaseline(current, draft)
        pending = replace(
            current,
            peer_ref=draft.peer_ref,
            dialog_title=dialog.title,
            criteria=draft.criteria,
            media_kinds=draft.media_kinds,
            interval_minutes=draft.interval_minutes,
            history_days=draft.history_days,
            enabled=enabled,
            state=(SubscriptionState.BASELINING if reset else current.state),
            last_message_id=(None if reset else current.last_message_id),
            backfill_from_utc=(
                self._history_cutoff(draft.history_days, now)
                if reset
                else current.backfill_from_utc
            ),
            backfill_through_id=(None if reset else current.backfill_through_id),
            next_run_at=(
                None if reset or not enabled else now + timedelta(minutes=draft.interval_minutes)
            ),
            last_error=None,
            failure_count=0,
            updated_at=now,
        )
        if not reset:
            stable = replace(
                pending,
                state=(SubscriptionState.WAITING if enabled else SubscriptionState.PAUSED),
            )
            self.catalog.save_subscription(stable)
            return stable
        self.catalog.save_subscription(pending)
        try:
            return await self._establish_baseline(pending)
        except Exception:
            failed_at = self.clock()
            self.catalog.save_subscription(
                replace(
                    pending,
                    state=(SubscriptionState.FAILED if enabled else SubscriptionState.PAUSED),
                    next_run_at=failed_at if enabled else None,
                    last_error="自动建立基线失败",
                    failure_count=pending.failure_count + 1,
                    updated_at=failed_at,
                )
            )
            raise

    async def _establish_baseline(
        self,
        rule: SubscriptionRule,
        *,
        running: bool = False,
    ) -> SubscriptionRule:
        gateway, _planner = self._require_online()
        snapshot_id = await gateway.latest_message_id(rule.peer_ref)
        now = self.clock()
        backfill_from = rule.backfill_from_utc
        backfill_through: int | None = None
        baseline = snapshot_id
        if rule.history_days > 0:
            backfill_from = backfill_from or self._history_cutoff(
                rule.history_days,
                now,
            )
            assert backfill_from is not None
            boundary_id = await gateway.message_id_before(
                rule.peer_ref,
                backfill_from,
            )
            baseline = min(boundary_id, snapshot_id)
            if baseline < snapshot_id:
                backfill_through = snapshot_id
            else:
                backfill_from = None
        state = (
            SubscriptionState.RUNNING
            if running
            else (SubscriptionState.WAITING if rule.enabled else SubscriptionState.PAUSED)
        )
        next_run_at = None
        if not running and rule.enabled:
            next_run_at = (
                now
                if backfill_through is not None
                else now + timedelta(minutes=rule.interval_minutes)
            )
        ready = replace(
            rule,
            state=state,
            last_message_id=baseline,
            backfill_from_utc=backfill_from,
            backfill_through_id=backfill_through,
            next_run_at=next_run_at,
            last_error=None,
            updated_at=now,
        )
        self.catalog.save_subscription(ready)
        return ready

    @staticmethod
    def _history_cutoff(history_days: int, now: datetime) -> datetime | None:
        return now - timedelta(days=history_days) if history_days else None

    @staticmethod
    def _requires_rebaseline(
        current: SubscriptionRule,
        draft: SubscriptionDraft,
    ) -> bool:
        return (
            current.peer_ref != draft.peer_ref
            or current.normalized_keyword != draft.matcher_fingerprint
            or current.media_kinds != draft.media_kinds
            or current.history_days != draft.history_days
        )

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

    async def probe_rule(
        self,
        rule_id: str,
        *,
        on_progress: Callable[[SubscriptionProbeProgress], None] | None = None,
    ) -> SubscriptionProbeReport:
        rule = self.get_rule(rule_id)
        gateway, planner = self._require_online()
        self._probe_progress(
            on_progress,
            rule.id,
            0,
            0,
            0,
            "正在读取最近消息",
        )
        messages = await gateway.recent_messages(rule.peer_ref, limit=100)

        def emit(step: _MatchStep) -> None:
            self._probe_progress(
                on_progress,
                rule.id,
                step.inspected,
                step.keyword_hits,
                step.matched,
                step.phase,
            )

        matched = await self._match_messages(rule, messages, emit)
        keys = {self._media_key(item.remote) for item in matched.candidates}
        existing = planner.existing_media_keys(keys)
        samples = tuple(
            SubscriptionProbeSample(
                item.message_id,
                item.message_date_utc,
                item.remote.kind,
                item.remote.original_name,
                item.remote.expected_size,
                self._media_key(item.remote) in existing,
                item.excerpt[:80],
            )
            for item in matched.candidates[:20]
        )
        self._probe_progress(
            on_progress,
            rule.id,
            matched.inspected,
            matched.keyword_hits,
            len(matched.candidates),
            "测试完成",
        )
        return SubscriptionProbeReport(
            rule.id,
            matched.inspected,
            matched.keyword_hits,
            len(matched.candidates),
            len(existing),
            samples,
            self.clock(),
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
        keyword_hits = 0
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
        self._progress(on_progress, rule_id, 0, 0, 0, 0, 0, "正在读取新消息")
        try:
            if rule.last_message_id is None:
                rule = await self._establish_baseline(rule, running=True)
                last_processed_id = rule.last_message_id or 0
                if rule.backfill_through_id is None:
                    report = self._complete_run(
                        rule,
                        started_at,
                        last_processed_id,
                        inspected,
                        keyword_hits,
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
                        0,
                        "基线建立完成",
                    )
                    return report

            snapshot_id = (
                rule.backfill_through_id
                if rule.backfill_through_id is not None
                else await gateway.latest_message_id(rule.peer_ref)
            )
            after_id = rule.last_message_id
            assert after_id is not None
            messages = await gateway.incremental_messages(
                rule.peer_ref,
                after_id=after_id,
                through_id=max(after_id, snapshot_id),
                limit=self.PAGE_LIMIT,
            )

            def emit(step: _MatchStep) -> None:
                nonlocal inspected, keyword_hits, matched
                inspected = step.inspected
                keyword_hits = step.keyword_hits
                matched = step.matched
                self._progress(
                    on_progress,
                    rule_id,
                    step.inspected,
                    step.keyword_hits,
                    step.matched,
                    queued,
                    duplicate,
                    step.phase,
                )

            match = await self._match_messages(rule, messages, emit)
            inspected = match.inspected
            keyword_hits = match.keyword_hits
            matched = len(match.candidates)
            unique = [item.remote for item in match.candidates]
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
                keyword_hits,
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
                keyword_hits,
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
                keyword_hits,
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
                keyword_hits,
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
        keyword_hits: int,
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
            complete_backfill=(rule.backfill_through_id is not None and not has_more),
        )
        run = SubscriptionRun(
            self.uuid_factory(),
            rule.id,
            rule.account_id,
            started_at,
            finished_at,
            SubscriptionRunStatus.COMPLETED,
            inspected,
            keyword_hits,
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
        keyword_hits: int,
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
                keyword_hits,
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
    def _media_key(item: RemoteMedia) -> tuple[str, int, str]:
        return item.peer_ref, item.message_id, item.media_id

    async def _match_messages(
        self,
        rule: SubscriptionRule,
        messages: tuple[RemoteMessage, ...],
        on_step: Callable[[_MatchStep], None] | None = None,
    ) -> _MatchResult:
        gateway, _planner = self._require_online()
        keyword_hits = 0
        candidates: dict[tuple[str, int, str], _MatchedCandidate] = {}
        expanded_groups: set[int] = set()
        phase = "正在筛选消息"

        def add_candidate(remote: RemoteMedia, excerpt: str) -> None:
            candidates.setdefault(
                self._media_key(remote),
                _MatchedCandidate(
                    remote,
                    remote.message_id,
                    remote.message_date_utc,
                    excerpt,
                ),
            )

        for index, item in enumerate(messages, start=1):
            if rule.criteria.matches(item.text):
                keyword_hits += 1
                if item.media is not None and item.grouped_id is not None:
                    if item.grouped_id not in expanded_groups:
                        expanded_groups.add(item.grouped_id)
                        album = await gateway.expand_album(
                            rule.peer_ref,
                            item.message_id,
                            item.grouped_id,
                        )
                        for hit in album:
                            if hit.remote.kind in rule.media_kinds:
                                add_candidate(hit.remote, hit.excerpt or item.text)
                elif item.media is not None and item.media.kind in rule.media_kinds:
                    add_candidate(item.media, item.text)
            if on_step is not None:
                on_step(_MatchStep(index, keyword_hits, len(candidates), phase))

        ordered = tuple(
            sorted(
                candidates.values(),
                key=lambda item: (
                    item.message_date_utc,
                    item.message_id,
                    item.remote.media_id,
                ),
                reverse=True,
            )
        )
        return _MatchResult(len(messages), keyword_hits, ordered)

    @staticmethod
    def _progress(
        callback: Callable[[SubscriptionProgress], None] | None,
        rule_id: str,
        inspected: int,
        keyword_hits: int,
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
                    keyword_hits,
                    matched,
                    queued,
                    duplicate,
                    phase,
                )
            )

    @staticmethod
    def _probe_progress(
        callback: Callable[[SubscriptionProbeProgress], None] | None,
        rule_id: str,
        inspected: int,
        keyword_hits: int,
        matched: int,
        phase: str,
    ) -> None:
        if callback is not None:
            callback(
                SubscriptionProbeProgress(
                    rule_id,
                    inspected,
                    keyword_hits,
                    matched,
                    phase,
                )
            )
