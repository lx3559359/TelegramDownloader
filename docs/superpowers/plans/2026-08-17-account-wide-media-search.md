# Account-Wide Telegram Media Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a default “全部会话” search scope that finds downloadable media across every Telegram cloud conversation accessible to the current account, with stable pagination, real source labels, persisted history, and one deduplicated download task.

**Architecture:** Keep the existing single-dialog path intact and add an explicit `ALL_DIALOGS` scope. Use Telethon's raw `messages.searchGlobal` request so the gateway can persist Telegram's composite pagination tuple; let `ContentBrowserService` fill 100-result application pages, expand albums per peer, and persist source metadata. Represent “全部会话” only as an in-memory UI choice, while SQLite stores the scope and versioned cursor and `TaskPlanner` archives each selected item under its real source title.

**Tech Stack:** Python 3.12, Telethon 1.44.0, SQLite WAL, PySide6 model/view, qasync, pytest, pytest-asyncio, pytest-qt, Ruff.

---

## File map

- `src/telegram_downloader/content.py`: search scope, source kind, global scope constants, composite cursor serialization, and backward-compatible search model fields.
- `src/telegram_downloader/domain.py`: account-search task source kind.
- `src/telegram_downloader/gateway.py`: `messages.searchGlobal` adapter, source identity mapping, and composite cursor production.
- `src/telegram_downloader/catalog.py`: schema v4 migration, scope/cursor persistence, source metadata persistence, and multi-peer validation.
- `src/telegram_downloader/content_browser.py`: scope dispatch, sparse-page filling, peer-scoped album expansion, stable cross-dialog sorting, and account-search queue preparation.
- `src/telegram_downloader/planner.py`: account-search task construction and per-item source archive paths.
- `src/telegram_downloader/ui/content_models.py`: virtual “全部会话” choice, history scope display, and result source column.
- `src/telegram_downloader/ui/content_browser.py`: default global selection and scope-aware search signal.
- `src/telegram_downloader/controller.py`: carry `SearchScope` through online recovery and search replacement.
- `src/telegram_downloader/app.py`: adapt the Qt signal into a typed scope and query.
- `tests/test_content.py`: scope and composite cursor contracts.
- `tests/test_catalog.py`, `tests/test_self_test.py`: v3-to-v4 migration, cursor/source round trips, account isolation, and reported schema version.
- `tests/test_gateway.py`: raw global request parameters, every source kind, next-page tuple, and global error mapping.
- `tests/test_content_browser.py`: global dispatch, cross-peer albums, sparse pages, limit, cursor stall, persistence, and partial results.
- `tests/test_planner.py`: one account-search task with per-source target directories.
- `tests/ui/test_content_models.py`: virtual choice filtering, history scope, source column, and tooltips.
- `tests/ui/test_content_browser.py`: default scope, signal payload, validation, and retained single-dialog flow.
- `tests/test_controller.py`, `tests/test_app.py`: typed scope forwarding and application signal wiring.
- `tests/test_account_wide_search_e2e.py`: real catalog/task repositories across search, selection, commit, restart, and deduplication.
- `README.md`: user-facing search scope, supported conversations, limits, privacy, and offline behavior.

### Task 1: Define backward-compatible account-search domain contracts

**Files:**
- Modify: `src/telegram_downloader/content.py:1-120`
- Modify: `src/telegram_downloader/domain.py:7-12`
- Modify: `src/telegram_downloader/gateway.py:30-42`
- Test: `tests/test_content.py`

- [ ] **Step 1: Write failing scope and cursor tests**

Extend `tests/test_content.py` with explicit global-scope constants, cursor JSON round trips, malformed payload rejection, and defaults that preserve old constructors.

```python
import json

from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    ContentSourceKind,
    SearchCursor,
    SearchScope,
)
from telegram_downloader.domain import SourceKind


def test_global_search_contract_has_stable_scope_and_task_values() -> None:
    assert SearchScope.ALL_DIALOGS.value == "all_dialogs"
    assert SearchScope.SINGLE_DIALOG.value == "single_dialog"
    assert ALL_DIALOGS_SCOPE_REF == "__all_dialogs__"
    assert ALL_DIALOGS_TITLE == "全部会话"
    assert ContentSourceKind.SAVED.value == "saved"
    assert SourceKind.ACCOUNT_SEARCH.value == "account_search"


def test_composite_search_cursor_json_round_trip() -> None:
    cursor = SearchCursor(
        offset_id=87,
        offset_rate=13,
        offset_peer_ref="-100123",
    )

    encoded = cursor.to_json()

    assert json.loads(encoded) == {
        "offsetId": 87,
        "offsetPeerRef": "-100123",
        "offsetRate": 13,
        "version": 1,
    }
    assert SearchCursor.from_json(encoded) == cursor
    assert SearchCursor(42).to_json() == (
        '{"offsetId":42,"offsetPeerRef":null,"offsetRate":0,"version":1}'
    )


@pytest.mark.parametrize(
    "payload",
    ["{}", '{"version":2}', '{"version":1,"offsetId":-1}'],
)
def test_composite_search_cursor_rejects_invalid_payload(payload: str) -> None:
    with pytest.raises(ValueError, match="游标"):
        SearchCursor.from_json(payload)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content.py -q
```

Expected: import and attribute failures for the new enums, constants, `ACCOUNT_SEARCH`, and cursor serialization methods.

- [ ] **Step 3: Add the domain types without breaking positional fixtures**

Add these declarations to `content.py`; append new dataclass fields after existing defaulted fields so current positional tests retain their meaning.

```python
ALL_DIALOGS_SCOPE_REF = "__all_dialogs__"
ALL_DIALOGS_TITLE = "全部会话"


class SearchScope(StrEnum):
    SINGLE_DIALOG = "single_dialog"
    ALL_DIALOGS = "all_dialogs"


class ContentSourceKind(StrEnum):
    GROUP = "group"
    CHANNEL = "channel"
    PRIVATE = "private"
    BOT = "bot"
    SAVED = "saved"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SearchCursor:
    offset_id: int = 0
    offset_rate: int = 0
    offset_peer_ref: str | None = None

    def __post_init__(self) -> None:
        if self.offset_id < 0 or self.offset_rate < 0:
            raise ValueError("搜索游标不能为负数")

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "offsetId": self.offset_id,
                "offsetRate": self.offset_rate,
                "offsetPeerRef": self.offset_peer_ref,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "SearchCursor":
        try:
            payload = json.loads(value)
            if payload.get("version") != 1:
                raise ValueError
            peer_ref = payload.get("offsetPeerRef")
            if peer_ref is not None and not isinstance(peer_ref, str):
                raise ValueError
            return cls(
                offset_id=int(payload["offsetId"]),
                offset_rate=int(payload["offsetRate"]),
                offset_peer_ref=peer_ref,
            )
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("搜索游标格式无效") from error
```

Append `scope: SearchScope = SearchScope.SINGLE_DIALOG` after `SearchSession.last_error`. Append `source_title: str = ""` and `source_kind: ContentSourceKind = ContentSourceKind.UNKNOWN` after `SearchResult.queued`. Add `ACCOUNT_SEARCH = "account_search"` to `domain.SourceKind`. Append `source_kind: ContentSourceKind = ContentSourceKind.UNKNOWN` to `gateway.RemoteMedia` and import the enum there.

- [ ] **Step 4: Run domain and existing search model tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content.py tests/ui/test_content_models.py tests/test_content_browser.py -q
```

Expected: all selected tests pass with existing single-dialog fixtures using default values.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/content.py src/telegram_downloader/domain.py src/telegram_downloader/gateway.py tests/test_content.py
git commit -m "feat: define account search domain contracts"
```

### Task 2: Migrate the catalog to scoped searches and composite cursors

**Files:**
- Modify: `src/telegram_downloader/catalog.py:1-190,270-480,910-965`
- Modify: `tests/test_catalog.py`
- Modify: `tests/test_self_test.py:1-35`

- [ ] **Step 1: Write failing v4 migration and global round-trip tests**

Add this helper that creates a v3 database and one selected legacy result:

```python
def create_v3_catalog_with_search(database: Path, now: datetime) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(catalog_module._SCHEMA_V1)
        connection.executescript(catalog_module._SCHEMA_V2_MIGRATION)
        connection.executescript(catalog_module._SCHEMA_V3_MIGRATION)
        connection.execute(
            "INSERT INTO accounts(account_id, display_name, last_used_at) "
            "VALUES(?, ?, ?)",
            ("a1", "旧账号", now.isoformat()),
        )
        connection.execute(
            "INSERT INTO dialogs(account_id, peer_ref, title, username, kind, "
            "archived, available, last_synced_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            ("a1", "-1001", "旧群组", "", "group", 0, 1, now.isoformat()),
        )
        connection.execute(
            "INSERT INTO search_sessions(id, account_id, peer_ref, dialog_title, "
            "keyword, normalized_keyword, date_from_utc, date_to_utc, media_kinds, "
            "item_limit, filters_fingerprint, status, generation, next_offset_id, "
            "exhausted, result_count, created_at, updated_at, last_error) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-search", "a1", "-1001", "旧群组", "安装", "安装",
                now.isoformat(), now.isoformat(), "video", 20, "fingerprint",
                "incomplete", 1, 7, 0, 1, now.isoformat(), now.isoformat(), None,
            ),
        )
        connection.execute(
            "INSERT INTO search_results(id, search_id, account_id, peer_ref, "
            "message_id, grouped_id, media_id, media_kind, original_name, "
            "expected_size, message_date_utc, excerpt, thumbnail_key, selected, "
            "available, queued, generation) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-result", "legacy-search", "a1", "-1001", 7, None,
                "m7", "video", "7.mp4", 12, now.isoformat(), "安装教程",
                "thumb-7", 1, 1, 0, 1,
            ),
        )
```

Then add these assertions:

```python
def test_catalog_migrates_v3_searches_to_v4_without_losing_state(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    database = tmp_path / "catalog.sqlite3"
    create_v3_catalog_with_search(database, now)

    repository = CatalogRepository(database)
    repository.initialize()

    assert repository.schema_version() == 4
    session = repository.get_session("a1", "legacy-search")
    saved = repository.list_results("a1", session.id)
    assert session.scope is SearchScope.SINGLE_DIALOG
    assert session.cursor == SearchCursor(7)
    assert saved[0].source_title == "旧群组"
    assert saved[0].source_kind is ContentSourceKind.GROUP
    assert saved[0].selected is True


def test_global_search_round_trips_composite_cursor_and_multiple_peers(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    repository.initialize()
    repository.upsert_account(AccountProfile("a1", "账号"), now)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset(MediaKind), 20),
    )
    session = repository.begin_search(
        "global-1",
        "a1",
        ALL_DIALOGS_SCOPE_REF,
        ALL_DIALOGS_TITLE,
        query,
        now,
        scope=SearchScope.ALL_DIALOGS,
    )
    first = replace(
        result(session.id, "a1", now),
        source_title="资料群",
        source_kind=ContentSourceKind.GROUP,
    )
    second = replace(
        result(session.id, "a1", now, result_id="private", message_id=8),
        peer_ref="42",
        source_title="联系人",
        source_kind=ContentSourceKind.PRIVATE,
    )
    cursor = SearchCursor(8, 19, "42")

    repository.save_search_page("a1", session.id, session.generation, [first, second])
    repository.finish_search(
        "a1", session.id, session.generation, cursor, False, now
    )

    restored = repository.get_session("a1", session.id)
    assert restored.scope is SearchScope.ALL_DIALOGS
    assert restored.cursor == cursor
    assert {item.peer_ref for item in repository.list_results("a1", session.id)} == {
        "-1001",
        "42",
    }
```

Also update the unknown-schema test to create `PRAGMA user_version=5`, and change self-test expectations from schema `3` to `4`.

- [ ] **Step 2: Run migration tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_catalog.py tests/test_self_test.py -q
```

Expected: schema remains v3, the new columns are absent, global multi-peer saving is rejected, and cursor/source fields do not round-trip.

- [ ] **Step 3: Add the transactional v4 migration and repository mapping**

Add `_SCHEMA_V4_MIGRATION`:

```python
_SCHEMA_V4_MIGRATION = """
ALTER TABLE search_sessions
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'single_dialog';
ALTER TABLE search_sessions
    ADD COLUMN cursor_json TEXT;
ALTER TABLE search_results
    ADD COLUMN source_title TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results
    ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'unknown';
UPDATE search_results
SET source_title = COALESCE(
    (SELECT dialog_title FROM search_sessions
     WHERE search_sessions.id = search_results.search_id),
    peer_ref
);
UPDATE search_results
SET source_kind = COALESCE(
    (SELECT kind FROM dialogs
     WHERE dialogs.account_id = search_results.account_id
       AND dialogs.peer_ref = search_results.peer_ref),
    'unknown'
);
PRAGMA user_version=4;
"""
```

Advance `initialize()` through version 4 and reject any final version other than 4. Extend `begin_search(search_id, account_id, peer_ref, dialog_title, query, now, *, scope=SearchScope.SINGLE_DIALOG)` to insert/update `scope` and clear both cursor fields on refresh. Extend `finish_search()` to write `cursor_json=cursor.to_json()` while still writing `next_offset_id` for single-dialog compatibility.

Read cursors with this exact fallback:

```python
cursor_json = row["cursor_json"]
legacy_offset = row["next_offset_id"]
cursor = (
    SearchCursor.from_json(str(cursor_json))
    if cursor_json is not None
    else SearchCursor(int(legacy_offset))
    if legacy_offset is not None
    else None
)
```

Include `source_title` and `source_kind` in result inserts, conflict updates, and `_result_from_row()`. Change `save_search_page()` to select the session scope and apply peer equality only to `SINGLE_DIALOG`:

```python
if (
    SearchScope(str(session["scope"])) is SearchScope.SINGLE_DIALOG
    and any(item.peer_ref != str(session["peer_ref"]) for item in results)
):
    raise ValueError("搜索结果不属于当前会话")
```

- [ ] **Step 4: Run catalog, subscription, and self-test regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_catalog.py tests/test_subscription_service.py tests/test_subscription_diagnostics.py tests/test_self_test.py -q
```

Expected: all selected tests pass and the reported catalog schema is 4.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/catalog.py tests/test_catalog.py tests/test_self_test.py
git commit -m "feat: persist scoped global search pages"
```

### Task 3: Implement Telethon global search and source mapping

**Files:**
- Modify: `src/telegram_downloader/gateway.py:107-196,212-300,526-620,800-875`
- Test: `tests/test_gateway.py`

- [ ] **Step 1: Write failing raw request and pagination tests**

Extend `TelethonGateway.from_client_for_test()` with injectable request factories in the test setup, then add a fake response containing group, channel, private user, bot, and self-user media messages.

```python
@pytest.mark.asyncio
async def test_global_search_uses_raw_composite_cursor_and_maps_sources() -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    entities = [
        SimpleNamespace(peer_ref=-1001, title="资料群", megagroup=True),
        SimpleNamespace(peer_ref=-1002, title="公告频道", broadcast=True),
        SimpleNamespace(peer_ref=42, first_name="联系人", bot=False, is_self=False),
        SimpleNamespace(peer_ref=43, first_name="机器人", bot=True, is_self=False),
        SimpleNamespace(peer_ref=44, first_name="我", bot=False, is_self=True),
    ]
    messages = []
    for message_id, entity in enumerate(entities, start=80):
        message = media_message(message_id, now)
        message.message = f"安装资源 {message_id}"
        message.peer_id = SimpleNamespace(peer_ref=entity.peer_ref)
        messages.append(message)

    class Client:
        def __init__(self) -> None:
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(
                messages=messages,
                users=entities[2:],
                chats=entities[:2],
                next_rate=31,
            )

        async def get_entity(self, value):
            return next(item for item in entities if item.peer_ref == int(value))

    def request_factory(**values):
        return SimpleNamespace(**values)

    def peer_id(value):
        return int(value.peer_ref)

    client = Client()
    gateway = TelethonGateway.from_client_for_test(
        client,
        peer_id_getter=peer_id,
        search_global_request_factory=request_factory,
        input_peer_empty_factory=lambda: SimpleNamespace(empty=True),
        input_messages_filter_empty_factory=lambda: SimpleNamespace(empty_filter=True),
    )
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now - timedelta(days=1), now, frozenset(MediaKind), 500),
    )

    page = await gateway.search_all_media_page(query, None)

    request = client.requests[0]
    assert request.q == "安装"
    assert request.min_date == query.filters.date_from_utc
    assert request.max_date == query.filters.date_to_utc
    assert request.offset_rate == 0
    assert request.offset_id == 0
    assert request.folder_id is None
    assert [item.remote.source_kind for item in page.items] == [
        ContentSourceKind.GROUP,
        ContentSourceKind.CHANNEL,
        ContentSourceKind.PRIVATE,
        ContentSourceKind.BOT,
        ContentSourceKind.SAVED,
    ]
    assert page.next_cursor == SearchCursor(84, 31, "44")
    assert page.exhausted is False


@pytest.mark.asyncio
async def test_global_search_restores_offset_peer_and_maps_access_error() -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    cursor = SearchCursor(87, 13, "-1001")

    class Client:
        def __init__(self) -> None:
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(messages=[], users=[], chats=[])

        async def get_entity(self, value):
            assert value == -1001
            return SimpleNamespace(peer_ref=-1001, title="资料群", megagroup=True)

    client = Client()
    gateway = TelethonGateway.from_client_for_test(
        client,
        peer_id_getter=lambda value: int(value.peer_ref),
        search_global_request_factory=lambda **values: SimpleNamespace(**values),
        input_peer_empty_factory=lambda: SimpleNamespace(empty=True),
        input_messages_filter_empty_factory=lambda: SimpleNamespace(empty_filter=True),
    )
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now - timedelta(days=1), now, frozenset(MediaKind), 500),
    )

    page = await gateway.search_all_media_page(query, cursor)

    request = client.requests[0]
    assert request.offset_id == 87
    assert request.offset_rate == 13
    assert request.offset_peer.title == "资料群"
    assert page.exhausted is True
    assert page.next_cursor is None
```

Add this failure case to preserve existing safe exception mapping:

```python
@pytest.mark.asyncio
async def test_global_search_maps_access_error_without_server_text() -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)

    class RawAccessError(Exception):
        pass

    class Client:
        async def __call__(self, request):
            raise RawAccessError("private server detail")

    gateway = TelethonGateway.from_client_for_test(
        Client(),
        access_errors=(RawAccessError,),
        search_global_request_factory=lambda **values: SimpleNamespace(**values),
        input_peer_empty_factory=lambda: SimpleNamespace(empty=True),
        input_messages_filter_empty_factory=lambda: SimpleNamespace(empty_filter=True),
    )

    with pytest.raises(AccessDeniedError) as caught:
        await gateway.search_all_media_page(make_search_query(now), None)

    assert "private server detail" not in str(caught.value)
```

- [ ] **Step 2: Run gateway tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_gateway.py -q
```

Expected: the protocol and gateway lack `search_all_media_page`, request factories, source classification, and composite pagination.

- [ ] **Step 3: Add the raw global request adapter**

Add this protocol method:

```python
async def search_all_media_page(
    self,
    query: ContentSearchQuery,
    cursor: SearchCursor | None,
    *,
    on_progress: Callable[[SearchProgress], None] | None = None,
) -> RemoteSearchPage: ...
```

In production `__init__`, retain `functions.messages.SearchGlobalRequest`, `types.InputPeerEmpty`, and `types.InputMessagesFilterEmpty` as factories. Accept optional replacements for all three in `from_client_for_test()`. Implement `search_all_media_page()` with one raw request per call:

```python
offset_peer = (
    await self._resolve_entity(cursor.offset_peer_ref)
    if cursor is not None and cursor.offset_peer_ref is not None
    else self._input_peer_empty_factory()
)
request = self._search_global_request_factory(
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
```

Build the entity map from `response.users` and `response.chats`, initialize real Telethon messages with `_finish_init()` when available, and use `_peer_id_getter(message.peer_id)` for the media key. Add `_content_source_kind(entity)` with this precedence: `is_self`, `bot`, `megagroup`/chat class, `broadcast`/channel class, private user, unknown. Copy the kind into `RemoteMedia`.

Use the same helper in existing `search_media_page()` and `expand_album()`: after `remote_media_from_message()` returns, apply `replace(remote, source_kind=self._content_source_kind(entity))` before building `RemoteSearchHit`. This gives newly refreshed single-dialog results and album members the same source metadata contract as global hits.

Construct the next cursor only when the response exposes `next_rate` and has a last non-empty message:

```python
next_rate = getattr(response, "next_rate", None)
next_cursor = (
    SearchCursor(
        offset_id=int(last_message.id),
        offset_rate=int(next_rate),
        offset_peer_ref=last_peer_ref,
    )
    if last_message is not None and next_rate is not None
    else None
)
return RemoteSearchPage(tuple(items), next_cursor, next_cursor is None)
```

Update `_resolve_entity()` so any signed decimal stable peer string is converted to `int`, while invite hashes and usernames keep their current handling. This permits restoration of private-user and bot offset peers.

- [ ] **Step 4: Run gateway and link/download regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_gateway.py tests/test_links.py tests/test_downloader.py -q
```

Expected: all selected tests pass; existing single-dialog search still calls `iter_messages(entity, search=...)`.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/gateway.py tests/test_gateway.py
git commit -m "feat: search all Telegram cloud conversations"
```

### Task 4: Dispatch global searches and isolate albums by peer

**Files:**
- Modify: `src/telegram_downloader/content_browser.py:130-370,573-668`
- Modify: `tests/test_content_browser.py:1-150,530-820`

- [ ] **Step 1: Write failing service tests for scope and peer-scoped albums**

Extend `FakeGateway` with `all_pages`, `all_search_cursors`, and `search_all_media_page()`. Key fake albums by `(peer_ref, grouped_id)`. Add:

```python
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
            SearchCursor(10, 7, "42"),
            False,
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


@pytest.mark.asyncio
async def test_global_albums_with_equal_grouped_id_expand_per_peer(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    gateway.all_pages = [RemoteSearchPage((
        make_hit(20, now, grouped_id=900, peer_ref="-1001"),
        make_hit(30, now, grouped_id=900, peer_ref="42"),
    ), None, True)]
    gateway.albums[("-1001", 900)] = (
        make_hit(20, now, grouped_id=900, peer_ref="-1001"),
        make_hit(19, now, grouped_id=900, peer_ref="-1001"),
    )
    gateway.albums[("42", 900)] = (
        make_hit(30, now, grouped_id=900, peer_ref="42"),
        make_hit(29, now, grouped_id=900, peer_ref="42"),
    )
    service = await prepared_online_service(tmp_path, now, gateway)

    _session, results = await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        make_query(now),
        scope=SearchScope.ALL_DIALOGS,
    )

    assert gateway.album_calls == [("-1001", 900), ("42", 900)]
    assert {(item.peer_ref, item.message_id) for item in results} == {
        ("-1001", 20), ("-1001", 19), ("42", 30), ("42", 29)
    }
```

- [ ] **Step 2: Run the service tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py -q
```

Expected: `start_search` cannot accept a scope, requires a catalog dialog, dispatches only the single-dialog method, and merges albums by bare `grouped_id`.

- [ ] **Step 3: Add scope dispatch and source-aware result conversion**

Change the service entry point to:

```python
async def start_search(
    self,
    peer_ref: str,
    query: ContentSearchQuery,
    *,
    scope: SearchScope = SearchScope.SINGLE_DIALOG,
    on_progress: Callable[[SearchProgress], None] | None = None,
) -> tuple[SearchSession, list[SearchResult]]:
```

For `ALL_DIALOGS`, force the reserved scope ref/title and skip `catalog.get_dialog()`. For `SINGLE_DIALOG`, keep the current availability check. Pass `scope=scope` to `catalog.begin_search()`.

In `_fetch_page()`, call `gateway.search_all_media_page()` for global sessions and the existing method otherwise. Change every album map/set key to `(hit.remote.peer_ref, grouped_id)`, and call `expand_album(peer_ref, message_id, grouped_id)` from that key.

Use this stable hit sort key:

```python
return (
    -hit.remote.message_date_utc.timestamp(),
    hit.remote.peer_ref,
    -hit.remote.message_id,
    hit.remote.media_id,
)
```

Copy `remote.source_title` and `remote.source_kind` into `SearchResult`, and copy them back into `RemoteMedia` in `_remote_from_result()`.

- [ ] **Step 4: Run content service and catalog regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py tests/test_catalog.py -q
```

Expected: all selected tests pass, including existing single-dialog album pagination.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/content_browser.py tests/test_content_browser.py
git commit -m "feat: dispatch account-wide content searches"
```

### Task 5: Fill sparse global pages and stop stalled pagination safely

**Files:**
- Modify: `src/telegram_downloader/content_browser.py:229-370`
- Modify: `tests/test_content_browser.py`

- [ ] **Step 1: Write failing sparse-page, total-limit, and stalled-cursor tests**

```python
@pytest.mark.asyncio
async def test_global_search_fills_one_application_page_from_sparse_server_pages(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    first_cursor = SearchCursor(900, 1, "-1001")
    second_cursor = SearchCursor(800, 2, "42")
    gateway.all_pages = [
        RemoteSearchPage(tuple(make_hit(value, now) for value in range(1, 11)), first_cursor, False),
        RemoteSearchPage(tuple(make_hit(value, now, peer_ref="42") for value in range(101, 191)), second_cursor, False),
    ]
    service = await prepared_online_service(tmp_path, now, gateway)

    session, results = await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        make_query(now, limit=150),
        scope=SearchScope.ALL_DIALOGS,
    )

    assert len(results) == 100
    assert gateway.all_search_cursors == [None, first_cursor]
    assert session.cursor == second_cursor
    assert session.status is SearchStatus.RUNNING


@pytest.mark.asyncio
async def test_global_result_limit_is_account_wide_and_cursor_stall_is_incomplete(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    stalled = SearchCursor(50, 9, "42")
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    gateway.all_pages = [
        RemoteSearchPage(tuple(make_hit(value, now) for value in range(1, 61)), stalled, False),
        RemoteSearchPage(tuple(make_hit(value, now, peer_ref="42") for value in range(101, 161)), stalled, False),
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
    assert len(service.list_results(saved.id)) == 60
```

Include a successful 101-item total-limit case across three peer sources and assert the second UI operation returns exactly 101 total results, never 101 per peer. Capture progress events and assert inspected counts never decrease across internal server pages.

```python
@pytest.mark.asyncio
async def test_global_limit_and_progress_are_account_wide(
    tmp_path: Path,
) -> None:
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
    first_events = []
    more_events = []

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
```

- [ ] **Step 2: Run the focused sparse-page tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py -q
```

Expected: the service returns after one sparse remote page, does not aggregate progress, and accepts a repeated global cursor.

- [ ] **Step 3: Refactor `_fetch_page` around an application-page loop**

Keep the existing acceptance and album-boundary rules, but loop global remote calls until one of these conditions is true: 100 new accepted results, account-wide `item_limit`, remote exhaustion, cancellation, exception, or repeated cursor.

Use an explicit remote-page dispatcher:

```python
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
```

Before requesting a new global page, preserve the last successful cursor in the catalog. After a response, reject a non-exhausted page whose `next_cursor` equals the request cursor:

```python
if (
    session.scope is SearchScope.ALL_DIALOGS
    and not page.exhausted
    and page.next_cursor == request_cursor
):
    raise GatewayError("Telegram 全局搜索分页未前进")
```

Wrap each gateway progress callback with operation-level offsets so `SearchProgress.inspected` and `matched` are cumulative across internal pages. Preserve the current exception path that calls `_finish_incomplete()` and re-raises.

- [ ] **Step 4: Run service, progress, and cancellation regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py tests/test_content_progress.py tests/test_controller.py -q
```

Expected: all selected tests pass; cancellation retains every page committed before the cancel.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/content_browser.py tests/test_content_browser.py
git commit -m "feat: fill sparse global search pages"
```

### Task 6: Build one account-search task with real per-source archive paths

**Files:**
- Modify: `src/telegram_downloader/planner.py:90-218`
- Modify: `src/telegram_downloader/content_browser.py:409-460`
- Test: `tests/test_planner.py:180-240`
- Test: `tests/test_content_browser.py:800-900`

- [ ] **Step 1: Write failing planner and service queue tests**

```python
def test_plan_account_search_uses_one_task_and_real_source_directories(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 20),
    )
    def remote_media(
        peer_ref: str,
        source_title: str,
        message_id: int,
    ) -> RemoteMedia:
        return RemoteMedia(
            peer_ref,
            source_title,
            message_id,
            None,
            f"m{message_id}",
            MediaKind.VIDEO,
            f"{message_id}.mp4",
            12,
            now,
        )

    selected = [
        remote_media("-1001", "资料群", 10),
        remote_media("42", "联系人", 11),
    ]
    planner = TaskPlanner(
        object(),
        TaskRepository(tmp_path / "tasks.sqlite3"),
        tmp_path / "downloads",
        uuid_factory=iter(("task", "item-1", "item-2")).__next__,
        clock=lambda: now,
    )
    planner.repository.initialize()

    preview = planner.plan_account_search(query, selected)

    assert preview.task.source_kind is SourceKind.ACCOUNT_SEARCH
    assert preview.task.source_ref == ALL_DIALOGS_SCOPE_REF
    assert preview.task.source_title == ALL_DIALOGS_TITLE
    assert preview.task.display_title == "全部会话（搜索：安装）"
    assert {item.peer_ref for item in preview.items} == {"-1001", "42"}
    assert {item.target_path.parts[-4] for item in preview.items} == {
        "资料群", "联系人"
    }
```

Add this content service test to assert `prepare_download()` calls `plan_account_search()` rather than `plan_selected()` and preserves both source titles:

```python
def test_prepare_global_download_uses_account_planner(tmp_path: Path) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号"), now)
    query = make_query(now)
    session = catalog.begin_search(
        "global-1", "a1", ALL_DIALOGS_SCOPE_REF, ALL_DIALOGS_TITLE,
        query, now, scope=SearchScope.ALL_DIALOGS,
    )
    values = [
        replace(
            make_saved_result(session.id, now, "r1", 10),
            peer_ref="-1001", source_title="资料群",
            source_kind=ContentSourceKind.GROUP,
        ),
        replace(
            make_saved_result(session.id, now, "r2", 11),
            peer_ref="42", source_title="联系人",
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
```

- [ ] **Step 2: Run planner/service tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_content_browser.py -q
```

Expected: `plan_account_search` is missing, global preparation uses the reserved scope title for every target path, and the account source kind is unavailable.

- [ ] **Step 3: Add account-task planning and per-item archive titles**

Add:

```python
def plan_account_search(
    self,
    query: ContentSearchQuery,
    selected: list[RemoteMedia],
) -> ScanPreview:
    return self._build_preview(
        source_kind=SourceKind.ACCOUNT_SEARCH,
        source_ref=ALL_DIALOGS_SCOPE_REF,
        source_title=ALL_DIALOGS_TITLE,
        source_url="account-search://all-dialogs",
        filters=query.filters,
        remote=selected,
        display_title=f"{ALL_DIALOGS_TITLE}（搜索：{query.keyword}）",
        empty_message="所选媒体已全部存在于下载队列",
        skip_existing=True,
    )
```

In `_build_preview()`, change only the first archive argument from task-level `source_title` to item-level `item.source_title`:

```python
target = archive_target(
    self.downloads,
    item.source_title,
    item.message_date_utc,
    item.kind,
    item.original_name,
)
```

In `ContentBrowserService.prepare_download()`, dispatch `planner.plan_account_search()` for `ALL_DIALOGS` and keep `plan_selected()` for `SINGLE_DIALOG`.

- [ ] **Step 4: Run planner, repository, and service regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_repository.py tests/test_content_browser.py -q
```

Expected: all selected tests pass; link scans and subscriptions retain their prior paths because every item already carries the same real source title.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/planner.py src/telegram_downloader/content_browser.py tests/test_planner.py tests/test_content_browser.py
git commit -m "feat: archive global results by real source"
```

### Task 7: Add the virtual all-dialogs choice and result source column

**Files:**
- Modify: `src/telegram_downloader/ui/content_models.py:1-270`
- Test: `tests/ui/test_content_models.py`

- [ ] **Step 1: Write failing Qt model tests**

```python
def test_dialog_model_keeps_all_dialogs_first_during_filtering() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    model = DialogListModel()
    model.set_dialogs(dialogs(now))

    first = model.choice_at(0)
    assert first.scope is SearchScope.ALL_DIALOGS
    assert first.peer_ref == ALL_DIALOGS_SCOPE_REF
    assert model.data(model.index(0, 0)) == ALL_DIALOGS_TITLE

    model.set_filter("docs")

    assert model.rowCount() == 2
    assert model.choice_at(0).scope is SearchScope.ALL_DIALOGS
    assert model.choice_at(1).dialog.username == "docs"


def test_result_model_displays_source_name_kind_and_peer_tooltip(qtbot) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    model = SearchResultTableModel()
    item = replace(
        search_results(now)[0],
        source_title="联系人",
        source_kind=ContentSourceKind.PRIVATE,
        peer_ref="42",
    )
    model.set_results([item])

    assert model.HEADERS == (
        "选择", "预览", "日期", "来源", "摘要", "类型", "大小", "状态"
    )
    assert model.data(model.index(0, 3)) == "联系人"
    tooltip = model.data(model.index(0, 3), Qt.ItemDataRole.ToolTipRole)
    assert "私聊" in tooltip
    assert "42" in tooltip


def test_history_model_labels_account_scope() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    model = SearchHistoryTableModel()
    model.set_sessions([
        replace(search_session(now), scope=SearchScope.ALL_DIALOGS,
                peer_ref=ALL_DIALOGS_SCOPE_REF, dialog_title=ALL_DIALOGS_TITLE)
    ])
    assert model.HEADERS[0] == "搜索范围"
    assert model.data(model.index(0, 0)) == "全部会话"
```

- [ ] **Step 2: Run Qt model tests and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_models.py -q
```

Expected: no virtual choice API exists and the result model still has seven columns without source data.

- [ ] **Step 3: Implement model-only presentation**

Add an in-memory choice type to `ui/content_models.py`:

```python
@dataclass(frozen=True, slots=True)
class DialogChoice:
    scope: SearchScope
    peer_ref: str
    title: str
    available: bool
    dialog: ContentDialog | None = None
```

Have `DialogListModel._filtered()` always prepend:

```python
DialogChoice(
    SearchScope.ALL_DIALOGS,
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    True,
)
```

Convert real dialogs into `SINGLE_DIALOG` choices after applying the existing title/username filter and sort. Replace `dialog_at()` with `choice_at()` and update its call sites in Task 8.

Rename the first history header from “群组/频道” to “搜索范围”. Add the source column at index 3 and shift excerpt/type/size/status to 4/5/6/7. Use Chinese source labels for tooltips: 群组、频道、私聊、机器人、收藏夹、未知来源. Keep checkbox and preview columns at 0 and 1 so selection and thumbnail behavior remain stable.

- [ ] **Step 4: Run model and preview regressions**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_models.py tests/ui/test_media_preview.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/ui/content_models.py tests/ui/test_content_models.py
git commit -m "feat: present global scope and result sources"
```

### Task 8: Wire the default global scope through page, app, and controller

**Files:**
- Modify: `src/telegram_downloader/ui/content_browser.py:45-70,100-340,430-540`
- Modify: `src/telegram_downloader/controller.py:790-955`
- Modify: `src/telegram_downloader/app.py:1-30,419-444`
- Test: `tests/ui/test_content_browser.py`
- Test: `tests/test_controller.py:1515-1628,2058-2160`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write failing page and forwarding tests**

Update the page signal expectation to seven values and add:

```python
def test_all_dialogs_is_selected_by_default_and_emits_global_scope(qtbot) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.keyword_input.setText("安装教程")

    assert page.dialog_model.choice_at(page.dialog_list.currentIndex().row()).scope \
        is SearchScope.ALL_DIALOGS
    assert page.current_dialog_label.text() == "全部会话"

    with qtbot.waitSignal(page.search_requested, timeout=500) as caught:
        qtbot.mouseClick(page.search_button, Qt.MouseButton.LeftButton)

    assert caught.args[0] == SearchScope.ALL_DIALOGS.value
    assert caught.args[1] == ALL_DIALOGS_SCOPE_REF
    assert caught.args[2] == "安装教程"


@pytest.mark.asyncio
async def test_controller_forwards_global_scope_to_content_service() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    session = SearchSession(
        "global-1", "a1", ALL_DIALOGS_SCOPE_REF, ALL_DIALOGS_TITLE,
        query, SearchStatus.COMPLETED, 1, None, True, 0, now, now,
        scope=SearchScope.ALL_DIALOGS,
    )
    calls = []

    class Browser:
        async def start_search(self, peer_ref, query, *, scope, on_progress=None):
            calls.append((scope, peer_ref, query.keyword))
            return session, []

        def list_sessions(self):
            return [session]

        def list_results(self, _search_id):
            return []

    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        content_browser=Browser(),
        window=ContentWindowFake(),
    )

    await controller.search_content(
        ALL_DIALOGS_SCOPE_REF,
        query,
        scope=SearchScope.ALL_DIALOGS,
    )

    assert calls == [(SearchScope.ALL_DIALOGS, ALL_DIALOGS_SCOPE_REF, "安装")]
```

Add this app wiring test, and retain a page test selecting a real row that asserts `SINGLE_DIALOG` plus its peer ref:

```python
def test_account_search_signal_reaches_controller_with_typed_scope(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)
    calls = []
    completed = asyncio.Event()

    async def record(peer_ref, query, *, scope):
        calls.append((scope, peer_ref, query.keyword))
        completed.set()

    controller.search_content = record

    async def emit_and_wait() -> None:
        controller.window.content_page.search_requested.emit(
            SearchScope.ALL_DIALOGS.value,
            ALL_DIALOGS_SCOPE_REF,
            "安装",
            date(2026, 8, 1),
            date(2026, 8, 17),
            frozenset({MediaKind.VIDEO}),
            500,
        )
        await asyncio.wait_for(completed.wait(), timeout=1)

    try:
        loop.run_until_complete(emit_and_wait())
        assert calls == [
            (SearchScope.ALL_DIALOGS, ALL_DIALOGS_SCOPE_REF, "安装")
        ]
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()
```

- [ ] **Step 2: Run page/controller/app tests and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py tests/test_controller.py tests/test_app.py -q
```

Expected: the first row is a real dialog, the signal lacks a scope value, and controller/service signatures cannot forward it.

- [ ] **Step 3: Implement default selection and typed forwarding**

Change the page signal to:

```python
search_requested = Signal(str, str, str, object, object, object, int)
```

After `set_dialogs()`, restore the previous peer when present; otherwise select model row 0. Replace direct `ContentDialog` access with `choice_at()`. `_emit_search()` must emit `choice.scope.value`, `choice.peer_ref`, keyword, dates, media kinds, and limit. Only reject `available=False` when the scope is `SINGLE_DIALOG`. Continue routing a `t.me` keyword before search validation.

Change controller entry to:

```python
async def search_content(
    self,
    peer_ref: str,
    query: ContentSearchQuery,
    *,
    scope: SearchScope = SearchScope.SINGLE_DIALOG,
) -> None:
```

Pass `scope=scope` into `content_browser.start_search()` while preserving online recovery, cancellation, session-expiry handling, and final spinner cleanup.

Change the qasync adapter to accept seven signal values and parse scope before constructing filters:

```python
@qasync.asyncSlot(str, str, str, object, object, object, int)
async def content_search_requested(
    scope_value: str,
    peer_ref: str,
    keyword: str,
    date_from: object,
    date_to: object,
    media_kinds: object,
    item_limit: int,
) -> None:
    scope = SearchScope(scope_value)
    filters = AppController.filters_from_dates(
        date_from,
        date_to,
        frozenset(media_kinds),
        item_limit,
        datetime_now_timezone(),
    )
    await controller.search_content(
        peer_ref,
        ContentSearchQuery(keyword, filters),
        scope=scope,
    )
```

Update table header sizing after the new source column: stretch excerpt column 4, keep preview column 1 fixed, and resize columns 0, 2, 3, 5, 6, 7 to contents. Rename the left panel heading to “搜索范围”, and update the subtitle and empty hints to explain that “全部会话” is available.

- [ ] **Step 4: Run all UI, controller, and application tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui tests/test_controller.py tests/test_app.py -q
```

Expected: all selected tests pass; single-dialog history restoration still works when a real row is selected.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/ui/content_browser.py src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/ui/test_content_browser.py tests/test_controller.py tests/test_app.py
git commit -m "feat: make all conversations the default search scope"
```

### Task 9: Prove the persisted end-to-end global download flow

**Files:**
- Create: `tests/test_account_wide_search_e2e.py`
- Modify: `README.md:12-28,91-100`

- [ ] **Step 1: Write the failing end-to-end test**

Build real `CatalogRepository`, `TaskRepository`, `ThumbnailCache`, `TaskPlanner`, and `ContentBrowserService` instances with this fake gateway and deterministic helpers:

```python
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
                    "-1001", "资料群", 10, None, "m10", MediaKind.VIDEO,
                    "group.mp4", 12, self.now,
                    source_kind=ContentSourceKind.GROUP,
                ),
                "群组安装资源",
                "-1001:10:m10",
            ),
            RemoteSearchHit(
                RemoteMedia(
                    "42", "联系人", 11, None, "m11", MediaKind.VIDEO,
                    "private.mp4", 13, self.now,
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
```

Use this test to cover search, selection, preview, commit, queue marking, restart, and deduplication:

```python
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
```

The fake gateway must implement `account_profile`, `search_all_media_page`, `expand_album`, and `load_thumbnail`, and return `RemoteMedia.source_title/source_kind` for both peers. It must not implement a successful single-dialog search; that proves dispatch went through the global method.

- [ ] **Step 2: Run the end-to-end test and verify PASS**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_account_wide_search_e2e.py -q
```

Expected: PASS.

- [ ] **Step 3: Update user documentation**

Document these user-visible facts in `README.md`:

```markdown
- “账号内容”默认选择“全部会话”，可在当前账号可访问的群组、频道、私聊、机器人、收藏夹和已归档会话中执行 Telegram 服务端关键词搜索。
- 全账号搜索按一个总结果上限分页，结果显示真实来源；仍可选择单个群组或频道进行原有范围搜索。
```

In “浏览账号内容并选择下载”, state that secret chats and inaccessible/deleted server history cannot be searched, only matched media summaries are stored, offline mode can view history but cannot start a search, and selected files are archived under their real source names.

- [ ] **Step 4: Run the feature regression set**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_content.py tests/test_catalog.py tests/test_gateway.py tests/test_content_browser.py tests/test_planner.py tests/ui/test_content_models.py tests/ui/test_content_browser.py tests/test_controller.py tests/test_app.py tests/test_account_wide_search_e2e.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add README.md tests/test_account_wide_search_e2e.py
git commit -m "test: verify account-wide search workflow"
```

### Task 10: Run full verification and record the implementation boundary

**Files:**
- Verify only: all files changed by Tasks 1-9

- [ ] **Step 1: Run the repository test and lint entry point**

Run:

```powershell
.\scripts\test.ps1
```

Expected: the complete pytest suite passes, followed by a clean Ruff check for `src` and `tests`.

- [ ] **Step 2: Run the portable self-test**

Run:

```powershell
.\.venv\Scripts\python.exe -m telegram_downloader --self-test
```

Expected: exit code 0; the report shows catalog schema version 4 and every writable path under the current application root.

- [ ] **Step 3: Inspect scope and privacy invariants**

Run:

```powershell
rg -n "ALL_DIALOGS|all_dialogs|ACCOUNT_SEARCH|source_title|cursor_json" src tests README.md
rg -n "api_hash|StringSession|proxy_password|message\.message" src/telegram_downloader/catalog.py src/telegram_downloader/logging.py
git diff --check HEAD~9..HEAD
git status --short
```

Expected: global-scope references are limited to the intended search path; catalog persistence contains no complete message body or credentials; diff check is clean; working tree is clean.

- [ ] **Step 4: Perform a real-account manual smoke test without publishing**

Use an existing local test account and verify:

1. “账号内容” opens with “全部会话” selected.
2. One keyword returns media from at least two of: group/channel, private/bot,收藏夹, archived conversation.
3. Source names/types are correct and “加载更多” adds no duplicates.
4. Cancelling or disconnecting preserves partial results as an incomplete history entry.
5. Selecting media from two sources creates one task and files target separate real-source directories.
6. Restart restores history, source labels, selections/queued state, and safe pagination state.
7. Selecting a real group or channel still performs the old scoped search.

Expected: every item succeeds or a sanitized Telegram limitation is recorded; no release artifact or remote update pointer is changed.

- [ ] **Step 5: Commit any verification-only documentation correction**

If the smoke test reveals a documentation-only mismatch, update `README.md`, rerun `scripts/test.ps1`, and commit:

```powershell
git add README.md
git commit -m "docs: clarify account-wide search behavior"
```

If no documentation correction is needed, do not create an empty commit. Stop before version bumps, packaging, branch merge, GitHub/ModelScope publication, or update-manifest changes; those require separate authorization.
