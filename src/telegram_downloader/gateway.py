from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from telegram_downloader import __version__
from telegram_downloader.content import (
    AccountProfile,
    ContentDialog,
    ContentSearchQuery,
    ContentSourceKind,
    DialogKind,
    SearchCursor,
)
from telegram_downloader.content_progress import SearchProgress, SearchProgressReporter
from telegram_downloader.domain import MediaKind, ParsedLink, ScanFilters, SourceKind
from telegram_downloader.files import classify_media, sanitize_component
from telegram_downloader.settings import ProxySettings


class AuthState(StrEnum):
    READY = "ready"
    CODE_SENT = "code_sent"
    PASSWORD_REQUIRED = "password_required"


@dataclass(frozen=True, slots=True)
class RemoteMedia:
    peer_ref: str
    source_title: str
    message_id: int
    grouped_id: int | None
    media_id: str
    kind: MediaKind
    original_name: str
    expected_size: int | None
    message_date_utc: datetime
    source_kind: ContentSourceKind = ContentSourceKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class RemoteSearchHit:
    remote: RemoteMedia
    excerpt: str
    thumbnail_key: str


@dataclass(frozen=True, slots=True)
class RemoteSearchPage:
    items: tuple[RemoteSearchHit, ...]
    next_cursor: SearchCursor | None
    exhausted: bool


@dataclass(frozen=True, slots=True)
class RemoteMessage:
    message_id: int
    grouped_id: int | None
    message_date_utc: datetime
    text: str
    media: RemoteMedia | None


@dataclass(frozen=True, slots=True)
class QrLoginInfo:
    url: str
    expires_at: datetime
    valid_for_seconds: float


class GatewayError(RuntimeError):
    pass


class AuthorizationFailureReason(StrEnum):
    AUTH_KEY_DUPLICATED = "auth-key-duplicated"
    AUTH_KEY_INVALID = "auth-key-invalid"
    AUTH_KEY_UNREGISTERED = "auth-key-unregistered"
    SESSION_REVOKED = "session-revoked"
    NOT_AUTHORIZED = "not-authorized"
    UNKNOWN = "unknown"


class SessionExpiredError(GatewayError):
    def __init__(
        self,
        message: str = "Telegram 登录已失效，请重新扫码登录",
        *,
        reason: AuthorizationFailureReason = AuthorizationFailureReason.UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.reason = reason


class AccessDeniedError(GatewayError):
    pass


class EmptyMediaError(GatewayError):
    pass


class TransientNetworkError(GatewayError):
    pass


class MediaReferenceExpired(GatewayError):
    pass


class FloodWaitError(GatewayError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram 要求等待 {seconds} 秒")
        self.seconds = seconds


class _NoTelethonError(Exception):
    pass


class TelegramGateway(Protocol):
    async def connect(self) -> None: ...

    def is_connected(self) -> bool: ...

    async def request_code(self, phone: str) -> str: ...

    async def sign_in(
        self,
        phone: str,
        code: str,
        phone_code_hash: str,
    ) -> AuthState: ...

    async def check_password(self, password: str) -> AuthState: ...

    async def begin_qr_login(self) -> QrLoginInfo: ...

    async def wait_qr_login(self) -> AuthState: ...

    async def refresh_qr_login(self) -> QrLoginInfo: ...

    def export_session(self) -> str: ...

    def scan(
        self,
        source: ParsedLink,
        filters: ScanFilters,
    ) -> AsyncIterator[RemoteMedia]: ...

    def stream_media(
        self,
        peer_ref: str,
        message_id: int,
        offset: int,
    ) -> AsyncIterator[bytes]: ...

    async def test_connection(self) -> None: ...

    async def account_name(self) -> str | None: ...

    async def account_profile(self) -> AccountProfile: ...

    def iter_content_dialogs(
        self,
        account_id: str,
    ) -> AsyncIterator[ContentDialog]: ...

    async def search_media_page(
        self,
        peer_ref: str,
        query: ContentSearchQuery,
        cursor: SearchCursor | None,
        *,
        on_progress: Callable[[SearchProgress], None] | None = None,
    ) -> RemoteSearchPage: ...

    async def search_all_media_page(
        self,
        query: ContentSearchQuery,
        cursor: SearchCursor | None,
        *,
        on_progress: Callable[[SearchProgress], None] | None = None,
    ) -> RemoteSearchPage: ...

    async def latest_message_id(self, entity_ref: str) -> int: ...

    async def message_id_before(
        self,
        entity_ref: str,
        before_utc: datetime,
    ) -> int: ...

    async def recent_messages(
        self,
        entity_ref: str,
        *,
        limit: int,
    ) -> tuple[RemoteMessage, ...]: ...

    async def incremental_messages(
        self,
        entity_ref: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
    ) -> tuple[RemoteMessage, ...]: ...

    async def expand_album(
        self,
        peer_ref: str,
        message_id: int,
        grouped_id: int,
    ) -> tuple[RemoteSearchHit, ...]: ...

    async def load_thumbnail(
        self,
        peer_ref: str,
        message_id: int,
        media_id: str,
    ) -> bytes | None: ...

    async def disconnect(self) -> None: ...


def proxy_dict(settings: ProxySettings, password: str) -> dict[str, object] | None:
    if settings.kind == "none":
        return None
    return {
        "proxy_type": settings.kind,
        "addr": settings.host,
        "port": settings.port,
        "username": settings.username or None,
        "password": password or None,
        "rdns": True,
    }


class TelethonGateway:
    _ALBUM_RADIUS = 20
    _SEARCH_PAGE_SIZE = 100

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str = "",
        proxy: ProxySettings | None = None,
        proxy_password: str = "",
        *,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        from telethon import TelegramClient, errors, functions, types, utils
        from telethon.sessions import StringSession

        self._client = TelegramClient(
            StringSession(session),
            api_id,
            api_hash,
            proxy=proxy_dict(proxy or ProxySettings(), proxy_password),
            flood_sleep_threshold=0,
            device_model="TelegramDownloader Windows",
            app_version=__version__,
            system_lang_code="zh-hans",
            lang_code="zh-hans",
        )
        self._password_needed_error: type[BaseException] = errors.SessionPasswordNeededError
        self._flood_wait_error: type[BaseException] = errors.FloodWaitError
        self._authorization_error_reasons: dict[type[BaseException], AuthorizationFailureReason] = {
            errors.AuthKeyDuplicatedError: (AuthorizationFailureReason.AUTH_KEY_DUPLICATED),
            errors.AuthKeyInvalidError: AuthorizationFailureReason.AUTH_KEY_INVALID,
            errors.AuthKeyUnregisteredError: (AuthorizationFailureReason.AUTH_KEY_UNREGISTERED),
            errors.SessionRevokedError: AuthorizationFailureReason.SESSION_REVOKED,
        }
        self._authorization_errors = tuple(self._authorization_error_reasons)
        self._reference_expired_errors: tuple[type[BaseException], ...] = (
            errors.FileReferenceExpiredError,
        )
        self._access_errors: tuple[type[BaseException], ...] = (
            errors.ChannelPrivateError,
            errors.ChatAdminRequiredError,
            errors.InviteHashExpiredError,
            errors.InviteHashInvalidError,
            errors.MessageIdInvalidError,
            errors.UsernameInvalidError,
            errors.UsernameNotOccupiedError,
        )
        self._transient_errors: tuple[type[BaseException], ...] = (
            ConnectionError,
            OSError,
            TimeoutError,
        )
        self._check_invite_request = functions.messages.CheckChatInviteRequest
        self._peer_id_getter = utils.get_peer_id
        self._search_global_request_factory = functions.messages.SearchGlobalRequest
        self._input_peer_empty_factory = types.InputPeerEmpty
        self._input_messages_filter_empty_factory = types.InputMessagesFilterEmpty
        self._qr_login: object | None = None
        self._connected = False
        self._entity_cache: dict[str, object] = {}
        self._utc_now = utc_now or (lambda: datetime.now(UTC))

    @classmethod
    def from_client_for_test(
        cls,
        client: object,
        *,
        password_needed_error: type[BaseException] = _NoTelethonError,
        flood_wait_error: type[BaseException] = _NoTelethonError,
        authorization_errors: tuple[type[BaseException], ...] = (),
        authorization_error_reasons: Mapping[type[BaseException], AuthorizationFailureReason]
        | None = None,
        reference_expired_errors: tuple[type[BaseException], ...] = (),
        access_errors: tuple[type[BaseException], ...] = (),
        transient_errors: tuple[type[BaseException], ...] = (
            ConnectionError,
            OSError,
            TimeoutError,
        ),
        peer_id_getter=None,
        search_global_request_factory=None,
        input_peer_empty_factory=None,
        input_messages_filter_empty_factory=None,
        utc_now: Callable[[], datetime] | None = None,
        connected: bool = True,
    ) -> TelethonGateway:
        gateway = cls.__new__(cls)
        gateway._client = client
        gateway._password_needed_error = password_needed_error
        gateway._flood_wait_error = flood_wait_error
        gateway._authorization_error_reasons = dict(
            authorization_error_reasons
            or {
                error_type: AuthorizationFailureReason.UNKNOWN
                for error_type in authorization_errors
            }
        )
        gateway._authorization_errors = authorization_errors
        gateway._reference_expired_errors = reference_expired_errors
        gateway._access_errors = access_errors
        gateway._transient_errors = transient_errors
        gateway._check_invite_request = None
        gateway._peer_id_getter = peer_id_getter or (lambda entity: entity)
        gateway._search_global_request_factory = search_global_request_factory
        gateway._input_peer_empty_factory = input_peer_empty_factory
        gateway._input_messages_filter_empty_factory = input_messages_filter_empty_factory
        gateway._qr_login = None
        gateway._connected = connected
        gateway._entity_cache = {}
        gateway._utc_now = utc_now or (lambda: datetime.now(UTC))
        return gateway

    async def connect(self) -> None:
        try:
            await self._client.connect()
        except Exception as exc:
            self._raise_mapped(exc)
        self._connected = True

    def is_connected(self) -> bool:
        method = getattr(self._client, "is_connected", None)
        return bool(method()) if callable(method) else self._connected

    async def request_code(self, phone: str) -> str:
        try:
            sent = await self._client.send_code_request(phone)
        except Exception as exc:
            self._raise_mapped(exc)
        code_hash = getattr(sent, "phone_code_hash", "")
        if not code_hash:
            raise GatewayError("Telegram 未返回验证码会话")
        return str(code_hash)

    async def sign_in(
        self,
        phone: str,
        code: str,
        phone_code_hash: str,
    ) -> AuthState:
        try:
            await self._client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
        except self._password_needed_error:
            return AuthState.PASSWORD_REQUIRED
        except Exception as exc:
            self._raise_mapped(exc)
        return AuthState.READY

    async def check_password(self, password: str) -> AuthState:
        try:
            await self._client.sign_in(password=password)
        except Exception as exc:
            self._raise_mapped(exc)
        return AuthState.READY

    async def begin_qr_login(self) -> QrLoginInfo:
        if not self._connected:
            await self.connect()
        try:
            self._qr_login = await self._client.qr_login()
        except Exception as exc:
            self._raise_mapped(exc)
        return self._qr_info()

    async def wait_qr_login(self) -> AuthState:
        qr_login = self._require_qr_login()
        try:
            await qr_login.wait()
        except self._password_needed_error:
            self._qr_login = None
            return AuthState.PASSWORD_REQUIRED
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:
            self._raise_mapped(exc)
        self._qr_login = None
        return AuthState.READY

    async def refresh_qr_login(self) -> QrLoginInfo:
        qr_login = self._require_qr_login()
        try:
            await qr_login.recreate()
        except Exception as exc:
            self._raise_mapped(exc)
        return self._qr_info()

    def _require_qr_login(self):
        if self._qr_login is None:
            raise GatewayError("二维码登录会话尚未创建")
        return self._qr_login

    def _qr_info(self) -> QrLoginInfo:
        qr_login = self._require_qr_login()
        url = getattr(qr_login, "url", "")
        expires_at = self._utc_datetime(getattr(qr_login, "expires", None))
        if not isinstance(url, str) or not url.startswith("tg://login?token="):
            raise GatewayError("Telegram 未返回有效二维码")
        if expires_at is None:
            raise GatewayError("Telegram 未返回二维码过期时间")
        now = self._utc_datetime(self._utc_now())
        if now is None:
            raise GatewayError("本机时间无效，无法生成二维码")
        return QrLoginInfo(
            url,
            expires_at,
            (expires_at - now).total_seconds(),
        )

    def export_session(self) -> str:
        session = getattr(self._client, "session", None)
        if session is None or not hasattr(session, "save"):
            raise GatewayError("当前 Telegram 会话无法导出")
        value = session.save()
        if not isinstance(value, str) or not value:
            raise GatewayError("Telegram 会话导出失败")
        return value

    async def scan(
        self,
        source: ParsedLink,
        filters: ScanFilters,
    ) -> AsyncIterator[RemoteMedia]:
        if filters.item_limit < 1 or filters.date_from_utc > filters.date_to_utc:
            raise ValueError("扫描条件无效")
        entity = await self._resolve_entity(source.entity_ref)
        title = self._entity_title(entity, source.entity_ref)

        if source.kind is SourceKind.SINGLE_MESSAGE:
            async for item in self._scan_single(entity, source, title):
                yield item
            return

        matched = 0
        try:
            messages = self._client.iter_messages(entity)
            async for message in messages:
                message_date = self._utc_datetime(getattr(message, "date", None))
                if message_date is None:
                    continue
                if message_date > filters.date_to_utc:
                    continue
                if message_date < filters.date_from_utc:
                    break
                remote = self.remote_media_from_message(source.entity_ref, message, title)
                if remote is None or remote.kind not in filters.media_kinds:
                    continue
                yield remote
                matched += 1
                if matched >= filters.item_limit:
                    break
        except Exception as exc:
            self._raise_mapped(exc)

    async def _scan_single(
        self,
        entity: object,
        source: ParsedLink,
        title: str,
    ) -> AsyncIterator[RemoteMedia]:
        try:
            selected = await self._client.get_messages(entity, ids=source.message_id)
            if selected is None or getattr(selected, "media", None) is None:
                raise EmptyMediaError("该消息不含可下载媒体")
            grouped_id = getattr(selected, "grouped_id", None)
            if grouped_id is None:
                remote = self.remote_media_from_message(source.entity_ref, selected, title)
                if remote is None:
                    raise EmptyMediaError("该消息不含可下载媒体")
                yield remote
                return

            selected_id = int(selected.id)
            grouped = {selected_id: selected}
            messages = self._client.iter_messages(
                entity,
                min_id=max(0, selected_id - self._ALBUM_RADIUS - 1),
                max_id=selected_id + self._ALBUM_RADIUS + 1,
            )
            async for candidate in messages:
                if getattr(candidate, "grouped_id", None) == grouped_id:
                    grouped[int(candidate.id)] = candidate
            for message_id in sorted(grouped):
                remote = self.remote_media_from_message(
                    source.entity_ref,
                    grouped[message_id],
                    title,
                )
                if remote is not None:
                    yield remote
        except EmptyMediaError:
            raise
        except Exception as exc:
            self._raise_mapped(exc)

    async def stream_media(
        self,
        peer_ref: str,
        message_id: int,
        offset: int,
    ) -> AsyncIterator[bytes]:
        if offset < 0:
            raise ValueError("下载偏移不能为负数")
        try:
            entity = await self._resolve_entity(peer_ref)
            message = await self._client.get_messages(entity, ids=message_id)
            media = getattr(message, "media", None) if message is not None else None
            if media is None:
                raise EmptyMediaError("来源消息已不存在或不含媒体")
            async for chunk in self._client.iter_download(media, offset=offset):
                if chunk:
                    yield bytes(chunk)
        except EmptyMediaError:
            raise
        except Exception as exc:
            self._raise_mapped(exc)

    async def test_connection(self) -> None:
        try:
            await self._client.connect()
            if await self._client.get_me() is None:
                raise SessionExpiredError(reason=AuthorizationFailureReason.NOT_AUTHORIZED)
        except GatewayError:
            raise
        except Exception as exc:
            self._raise_mapped(exc)

    async def account_profile(self) -> AccountProfile:
        try:
            account = await self._client.get_me()
        except Exception as exc:
            self._raise_mapped(exc)
        account_id = getattr(account, "id", None) if account is not None else None
        if not isinstance(account_id, int):
            raise GatewayError("Telegram 账号尚未登录")
        return AccountProfile(str(account_id), self._account_display_name(account))

    async def account_name(self) -> str | None:
        profile = await self.account_profile()
        return profile.display_name

    async def iter_content_dialogs(
        self,
        account_id: str,
    ) -> AsyncIterator[ContentDialog]:
        seen: set[str] = set()
        try:
            for archived in (False, True):
                async for dialog in self._client.iter_dialogs(archived=archived):
                    is_group = bool(getattr(dialog, "is_group", False))
                    is_channel = bool(getattr(dialog, "is_channel", False))
                    if not (is_group or is_channel):
                        continue
                    entity = getattr(dialog, "entity", None)
                    if entity is None:
                        continue
                    peer_ref = str(self._peer_id_getter(entity))
                    if peer_ref in seen:
                        continue
                    seen.add(peer_ref)
                    title = str(
                        getattr(dialog, "name", "") or getattr(entity, "title", "") or peer_ref
                    )
                    yield ContentDialog(
                        account_id=account_id,
                        peer_ref=peer_ref,
                        title=title,
                        username=str(getattr(entity, "username", "") or ""),
                        kind=(DialogKind.GROUP if is_group else DialogKind.CHANNEL),
                        archived=archived,
                        available=True,
                        last_synced_at=datetime.now(UTC),
                    )
        except Exception as exc:
            self._raise_mapped(exc)

    async def search_media_page(
        self,
        peer_ref: str,
        query: ContentSearchQuery,
        cursor: SearchCursor | None,
        *,
        on_progress: Callable[[SearchProgress], None] | None = None,
    ) -> RemoteSearchPage:
        items: list[RemoteSearchHit] = []
        inspected = 0
        last_inspected_id: int | None = None
        reached_start_date = False
        reporter = SearchProgressReporter(on_progress) if on_progress else None
        try:
            entity = await self._resolve_entity(peer_ref)
            title = self._entity_title(entity, peer_ref)
            messages = self._client.iter_messages(
                entity,
                search=query.keyword,
                offset_id=cursor.offset_id if cursor else 0,
                limit=self._SEARCH_PAGE_SIZE,
            )
            async for message in messages:
                inspected += 1
                last_inspected_id = int(message.id)
                matched = False
                try:
                    message_date = self._utc_datetime(getattr(message, "date", None))
                    if message_date is None:
                        continue
                    if message_date > query.filters.date_to_utc:
                        continue
                    if message_date < query.filters.date_from_utc:
                        reached_start_date = True
                        break
                    remote = self.remote_media_from_message(peer_ref, message, title)
                    if remote is None or remote.kind not in query.filters.media_kinds:
                        continue
                    remote = replace(
                        remote,
                        source_kind=self._content_source_kind(entity),
                    )
                    items.append(self._search_hit(remote, message))
                    matched = True
                finally:
                    if reporter is not None:
                        reporter.record(matched=matched)
        except Exception as exc:
            self._raise_mapped(exc)

        if reporter is not None:
            reporter.finish("正在整理结果")

        exhausted = reached_start_date or inspected < self._SEARCH_PAGE_SIZE
        next_cursor = (
            None if exhausted or last_inspected_id is None else SearchCursor(last_inspected_id)
        )
        return RemoteSearchPage(tuple(items), next_cursor, exhausted)

    async def search_all_media_page(
        self,
        query: ContentSearchQuery,
        cursor: SearchCursor | None,
        *,
        on_progress: Callable[[SearchProgress], None] | None = None,
    ) -> RemoteSearchPage:
        if (
            self._search_global_request_factory is None
            or self._input_peer_empty_factory is None
            or self._input_messages_filter_empty_factory is None
        ):
            raise GatewayError("全账号搜索请求未配置")

        reporter = SearchProgressReporter(on_progress) if on_progress else None
        items: list[RemoteSearchHit] = []
        last_message_id: int | None = None
        last_peer_ref: str | None = None
        try:
            if cursor is not None and cursor.offset_peer_ref is not None:
                offset_entity = await self._resolve_entity(cursor.offset_peer_ref)
                get_input_entity = getattr(self._client, "get_input_entity", None)
                offset_peer = (
                    await get_input_entity(offset_entity)
                    if callable(get_input_entity)
                    else offset_entity
                )
            else:
                offset_peer = self._input_peer_empty_factory()
            response = await self._client(
                self._search_global_request_factory(
                    q=query.keyword,
                    filter=self._input_messages_filter_empty_factory(),
                    min_date=query.filters.date_from_utc,
                    max_date=query.filters.date_to_utc,
                    offset_rate=cursor.offset_rate if cursor else 0,
                    offset_peer=offset_peer,
                    offset_id=cursor.offset_id if cursor else 0,
                    limit=self._SEARCH_PAGE_SIZE,
                    broadcasts_only=None,
                    groups_only=None,
                    users_only=None,
                    folder_id=None,
                )
            )
            entities = {
                self._peer_id_getter(entity): entity
                for entity in (
                    *tuple(getattr(response, "users", ()) or ()),
                    *tuple(getattr(response, "chats", ()) or ()),
                )
            }
            for message in tuple(getattr(response, "messages", ()) or ()):
                matched = False
                try:
                    finish_init = getattr(message, "_finish_init", None)
                    if callable(finish_init):
                        finish_init(self._client, entities, None)
                    message_id = getattr(message, "id", None)
                    peer_id = getattr(message, "peer_id", None)
                    if not isinstance(message_id, int) or message_id <= 0 or peer_id is None:
                        continue
                    peer_key = self._peer_id_getter(peer_id)
                    peer_ref = str(peer_key)
                    last_message_id = message_id
                    last_peer_ref = peer_ref
                    entity = entities.get(peer_key) or getattr(message, "chat", None)
                    title = self._entity_title(entity, peer_ref)
                    message_date = self._utc_datetime(getattr(message, "date", None))
                    if message_date is None:
                        continue
                    if not (
                        query.filters.date_from_utc <= message_date <= query.filters.date_to_utc
                    ):
                        continue
                    remote = self.remote_media_from_message(peer_ref, message, title)
                    if remote is None or remote.kind not in query.filters.media_kinds:
                        continue
                    remote = replace(
                        remote,
                        source_kind=self._content_source_kind(entity),
                    )
                    items.append(self._search_hit(remote, message))
                    matched = True
                finally:
                    if reporter is not None:
                        reporter.record(matched=matched)
        except Exception as exc:
            self._raise_mapped(exc)

        if reporter is not None:
            reporter.finish("正在整理结果")

        next_rate = getattr(response, "next_rate", None)
        next_cursor = (
            SearchCursor(last_message_id, int(next_rate), last_peer_ref)
            if last_message_id is not None and last_peer_ref is not None and next_rate is not None
            else None
        )
        return RemoteSearchPage(tuple(items), next_cursor, next_cursor is None)

    async def latest_message_id(self, entity_ref: str) -> int:
        try:
            entity = await self._resolve_entity(entity_ref)
            async for message in self._client.iter_messages(entity, limit=1):
                message_id = getattr(message, "id", None)
                return int(message_id) if isinstance(message_id, int) else 0
            return 0
        except Exception as exc:
            self._raise_mapped(exc)

    async def message_id_before(
        self,
        entity_ref: str,
        before_utc: datetime,
    ) -> int:
        if before_utc.tzinfo is None:
            raise ValueError("历史边界必须包含时区")
        before_utc = before_utc.astimezone(UTC)
        try:
            entity = await self._resolve_entity(entity_ref)
            async for message in self._client.iter_messages(
                entity,
                offset_date=before_utc,
                limit=1,
            ):
                message_id = getattr(message, "id", None)
                return int(message_id) if isinstance(message_id, int) else 0
            return 0
        except Exception as exc:
            self._raise_mapped(exc)

    async def recent_messages(
        self,
        entity_ref: str,
        *,
        limit: int,
    ) -> tuple[RemoteMessage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("最近消息数量必须在 1 到 100 之间")

        found: dict[int, RemoteMessage] = {}
        try:
            entity = await self._resolve_entity(entity_ref)
            title = self._entity_title(entity, entity_ref)
            async for message in self._client.iter_messages(entity, limit=limit):
                remote_message = self._remote_message(entity_ref, title, message)
                if remote_message is not None:
                    found[remote_message.message_id] = remote_message
        except Exception as exc:
            self._raise_mapped(exc)
        return tuple(
            sorted(
                found.values(),
                key=lambda item: (item.message_id, item.message_date_utc),
            )
        )

    async def incremental_messages(
        self,
        entity_ref: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
    ) -> tuple[RemoteMessage, ...]:
        if after_id < 0 or through_id < after_id:
            raise ValueError("无效的增量消息边界")
        if not 1 <= limit <= 500:
            raise ValueError("增量消息数量必须在 1 到 500 之间")
        if after_id == through_id:
            return ()

        found: dict[int, RemoteMessage] = {}
        try:
            entity = await self._resolve_entity(entity_ref)
            title = self._entity_title(entity, entity_ref)
            messages = self._client.iter_messages(
                entity,
                min_id=after_id,
                max_id=through_id + 1,
                reverse=True,
                limit=limit,
            )
            async for message in messages:
                remote_message = self._remote_message(entity_ref, title, message)
                if remote_message is None:
                    continue
                message_id = remote_message.message_id
                if not after_id < message_id <= through_id:
                    continue
                found[message_id] = remote_message
        except Exception as exc:
            self._raise_mapped(exc)
        return tuple(found[message_id] for message_id in sorted(found))

    async def expand_album(
        self,
        peer_ref: str,
        message_id: int,
        grouped_id: int,
    ) -> tuple[RemoteSearchHit, ...]:
        grouped: dict[int, RemoteSearchHit] = {}
        try:
            entity = await self._resolve_entity(peer_ref)
            title = self._entity_title(entity, peer_ref)
            messages = self._client.iter_messages(
                entity,
                min_id=max(0, message_id - self._ALBUM_RADIUS - 1),
                max_id=message_id + self._ALBUM_RADIUS + 1,
            )
            async for message in messages:
                if getattr(message, "grouped_id", None) != grouped_id:
                    continue
                remote = self.remote_media_from_message(peer_ref, message, title)
                if remote is not None:
                    remote = replace(
                        remote,
                        source_kind=self._content_source_kind(entity),
                    )
                    grouped[remote.message_id] = self._search_hit(remote, message)
        except Exception as exc:
            self._raise_mapped(exc)
        return tuple(grouped[item_id] for item_id in sorted(grouped))

    async def load_thumbnail(
        self,
        peer_ref: str,
        message_id: int,
        media_id: str,
    ) -> bytes | None:
        try:
            entity = await self._resolve_entity(peer_ref)
            message = await self._client.get_messages(entity, ids=message_id)
            media = getattr(message, "media", None) if message is not None else None
            if media is None:
                return None
            title = self._entity_title(entity, peer_ref)
            remote = self.remote_media_from_message(peer_ref, message, title)
            if remote is None or remote.media_id != media_id:
                return None
            downloaded = await self._client.download_media(
                media,
                file=bytes,
                thumb=-1,
            )
            return downloaded if isinstance(downloaded, bytes) else None
        except Exception as exc:
            self._raise_mapped(exc)

    @classmethod
    def _search_hit(cls, remote: RemoteMedia, message: object) -> RemoteSearchHit:
        return RemoteSearchHit(
            remote=remote,
            excerpt=cls._message_excerpt(message),
            thumbnail_key=(f"{remote.peer_ref}:{remote.message_id}:{remote.media_id}"),
        )

    @staticmethod
    def _message_excerpt(message: object) -> str:
        raw = str(getattr(message, "message", "") or "")
        visible = "".join(char for char in raw if char.isprintable() or char in "\n\t")
        return " ".join(visible.split())[:500]

    @classmethod
    def _remote_message(
        cls,
        peer_ref: str,
        source_title: str,
        message: object,
    ) -> RemoteMessage | None:
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int) or message_id <= 0:
            return None
        message_date = cls._utc_datetime(getattr(message, "date", None))
        if message_date is None:
            return None
        grouped_id = getattr(message, "grouped_id", None)
        return RemoteMessage(
            message_id=message_id,
            grouped_id=int(grouped_id) if isinstance(grouped_id, int) else None,
            message_date_utc=message_date,
            text=cls._message_excerpt(message),
            media=cls.remote_media_from_message(peer_ref, message, source_title),
        )

    @staticmethod
    def _account_display_name(account: object) -> str:
        first = getattr(account, "first_name", "") or ""
        last = getattr(account, "last_name", "") or ""
        display = " ".join(part for part in (first, last) if part).strip()
        username = getattr(account, "username", "") or ""
        return display or (f"@{username}" if username else "已登录")

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        except Exception as exc:
            self._raise_mapped(exc)
        finally:
            self._connected = False

    async def _resolve_entity(self, entity_ref: str) -> object:
        if entity_ref in self._entity_cache:
            return self._entity_cache[entity_ref]
        try:
            if entity_ref.startswith("+"):
                if self._check_invite_request is None:
                    entity = await self._client.get_entity(entity_ref)
                else:
                    invitation = await self._client(self._check_invite_request(entity_ref[1:]))
                    entity = getattr(invitation, "chat", None)
                if entity is None:
                    raise AccessDeniedError("请先使用 Telegram 加入该邀请链接")
            else:
                if entity_ref.lstrip("-").isdigit():
                    entity = await self._resolve_private_entity(int(entity_ref))
                else:
                    entity = await self._client.get_entity(entity_ref)
        except AccessDeniedError:
            raise
        except Exception as exc:
            self._raise_mapped(exc)
        self._entity_cache[entity_ref] = entity
        return entity

    async def _resolve_private_entity(self, reference: int) -> object:
        try:
            return await self._client.get_entity(reference)
        except ValueError:
            async for dialog in self._client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if entity is not None and self._peer_id_getter(entity) == reference:
                    return entity
        raise AccessDeniedError("当前账号未加入该私有频道或群组")

    def _raise_mapped(self, error: Exception) -> None:
        for error_type, reason in self._authorization_error_reasons.items():
            if isinstance(error, error_type):
                raise SessionExpiredError(reason=reason) from error
        if isinstance(error, self._authorization_errors):
            raise SessionExpiredError() from error
        if isinstance(error, self._flood_wait_error):
            raise FloodWaitError(max(1, int(getattr(error, "seconds", 1)))) from error
        if isinstance(error, self._reference_expired_errors):
            raise MediaReferenceExpired("媒体引用已过期，需要刷新来源消息") from error
        if isinstance(error, (ValueError, *self._access_errors)):
            raise AccessDeniedError("链接无效、已失效或当前账号无权访问") from error
        if isinstance(error, self._transient_errors):
            raise TransientNetworkError("Telegram 网络连接失败") from error
        raise error

    @staticmethod
    def _entity_title(entity: object, fallback: str) -> str:
        title = getattr(entity, "title", None)
        if not title:
            first = getattr(entity, "first_name", "") or ""
            last = getattr(entity, "last_name", "") or ""
            title = " ".join(part for part in (first, last) if part)
        return sanitize_component(str(title or fallback))

    @staticmethod
    def _content_source_kind(entity: object) -> ContentSourceKind:
        if bool(getattr(entity, "is_self", False)):
            return ContentSourceKind.SAVED
        if bool(getattr(entity, "bot", False)):
            return ContentSourceKind.BOT
        if bool(getattr(entity, "megagroup", False)):
            return ContentSourceKind.GROUP
        class_name = type(entity).__name__
        if class_name == "Chat" or class_name.endswith("Chat"):
            return ContentSourceKind.GROUP
        if bool(getattr(entity, "broadcast", False)) or class_name == "Channel":
            return ContentSourceKind.CHANNEL
        if class_name == "User" or any(
            hasattr(entity, attribute) for attribute in ("first_name", "last_name")
        ):
            return ContentSourceKind.PRIVATE
        return ContentSourceKind.UNKNOWN

    @staticmethod
    def _utc_datetime(value: object) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def remote_media_from_message(
        peer_ref: str,
        message: object,
        source_title: str | None = None,
    ) -> RemoteMedia | None:
        if getattr(message, "media", None) is None:
            return None
        message_id = int(message.id)
        date = TelethonGateway._utc_datetime(getattr(message, "date", None))
        if date is None:
            return None

        file = getattr(message, "file", None)
        mime = getattr(file, "mime_type", None)
        kind = classify_media(
            mime,
            str(getattr(file, "name", "") or ""),
            bool(getattr(message, "photo", None)),
            bool(getattr(message, "voice", None)),
            bool(getattr(message, "video", None)),
        )
        name = getattr(file, "name", None)
        if not name:
            suffix = getattr(file, "ext", None) or mimetypes.guess_extension(mime or "") or ""
            name = f"{kind.value}_{message_id}{suffix}"
        name = sanitize_component(str(name))

        media_object = getattr(message, "document", None) or getattr(message, "photo", None)
        media_id = getattr(file, "id", None) or getattr(media_object, "id", None)
        if media_id is None:
            media_id = f"{message_id}:{kind.value}"
        size = getattr(file, "size", None)
        expected_size = size if isinstance(size, int) and size >= 0 else None
        return RemoteMedia(
            peer_ref=peer_ref,
            source_title=source_title or peer_ref,
            message_id=message_id,
            grouped_id=getattr(message, "grouped_id", None),
            media_id=str(media_id),
            kind=kind,
            original_name=name,
            expected_size=expected_size,
            message_date_utc=date,
        )
