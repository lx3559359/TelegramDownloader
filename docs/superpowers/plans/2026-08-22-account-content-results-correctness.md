# Account Content Results Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair account-content result layout, checked-state rendering, album media filtering, and video thumbnail handling without changing search pagination or download semantics.

**Architecture:** Keep search policy in `ContentBrowserService`, persistence cleanup in `CatalogRepository`, Telegram thumbnail selection in `TelethonGateway`, and rendering in focused Qt model/delegate/widget classes. Every persistence path filters album members before commit, old incompatible rows are reconciled when history is opened, and thumbnail loading returns only bounded raster image bytes.

**Tech Stack:** Python 3.13, PySide6, Telethon 1.44, SQLite, pytest, pytest-qt, pytest-asyncio, Ruff.

---

## File map

- Modify `src/telegram_downloader/content_browser.py`: filter expanded album members, reconcile legacy result rows, and pass the cache item limit to thumbnail loading.
- Modify `src/telegram_downloader/catalog.py`: delete media kinds that do not belong to a stored query and update `result_count` transactionally.
- Modify `src/telegram_downloader/gateway.py`: expose the thumbnail byte limit and select only bounded static raster thumbnails.
- Modify `src/telegram_downloader/ui/content_models.py`: collapse source and excerpt into one logical content column, split date/time, and draw meaningful fallback icons.
- Create `src/telegram_downloader/ui/content_result_delegate.py`: paint wrapped source titles and one-line excerpts, with cached row-height calculation.
- Create `src/telegram_downloader/ui/tick_checkbox.py`: draw the white check mark over the themed checkbox indicator.
- Modify `src/telegram_downloader/ui/content_browser.py`: install the new delegate/widget and configure responsive result columns.
- Modify `tests/test_content_browser.py`: mixed-album filtering, history reconciliation, and thumbnail limit propagation.
- Modify `tests/test_catalog.py`: account-scoped legacy result pruning and count repair.
- Modify `tests/test_gateway.py`: static thumbnail candidate selection, size fallback, and raster validation.
- Modify `tests/test_account_wide_search_e2e.py`: accept the new gateway thumbnail signature.
- Modify `tests/ui/test_content_models.py`: new column contract and video fallback icon.
- Create `tests/ui/test_content_result_delegate.py`: wrapped-title height and content painting contract.
- Create `tests/ui/test_tick_checkbox.py`: checked indicator contains a visible white tick.
- Modify `tests/ui/test_content_browser.py`: new widths, stretch column, delegate, and no horizontal squeeze at minimum size.

### Task 1: Enforce media kinds after album expansion

**Files:**
- Modify: `tests/test_content_browser.py`
- Modify: `src/telegram_downloader/content_browser.py:404-447`

- [ ] **Step 1: Extend the search-hit fixture to create mixed media**

Change `make_hit` in `tests/test_content_browser.py` to accept a media kind and derive a matching name:

```python
def make_hit(
    message_id: int,
    now: datetime,
    *,
    grouped_id: int | None = None,
    peer_ref: str = "-1001",
    source_title: str = "资料群",
    source_kind: ContentSourceKind = ContentSourceKind.GROUP,
    media_kind: MediaKind = MediaKind.VIDEO,
) -> RemoteSearchHit:
    suffix = "jpg" if media_kind is MediaKind.PHOTO else "mp4"
    remote = RemoteMedia(
        peer_ref,
        source_title,
        message_id,
        grouped_id,
        f"m{message_id}",
        media_kind,
        f"{message_id}.{suffix}",
        10,
        now,
        source_kind,
    )
    return RemoteSearchHit(
        remote,
        f"摘要 {message_id}",
        f"{peer_ref}:{message_id}:m{message_id}",
    )
```

- [ ] **Step 2: Write the failing mixed-album regression test**

Add a parameterized test that exercises both single-dialog and global searches:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "peer_ref"),
    [
        (SearchScope.SINGLE_DIALOG, "-1001"),
        (SearchScope.ALL_DIALOGS, ALL_DIALOGS_SCOPE_REF),
    ],
)
async def test_video_search_discards_photos_added_by_album_expansion(
    tmp_path: Path,
    scope: SearchScope,
    peer_ref: str,
) -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    trigger = make_hit(20, now, grouped_id=900)
    page = RemoteSearchPage((trigger,), None, True)
    if scope is SearchScope.ALL_DIALOGS:
        gateway.all_pages = [page]
    else:
        gateway.pages = [page]
    gateway.albums[("-1001", 900)] = (
        trigger,
        make_hit(19, now, grouped_id=900, media_kind=MediaKind.PHOTO),
        make_hit(18, now, grouped_id=900),
    )
    service = await prepared_online_service(tmp_path, now, gateway)
    batches = []

    session, results = await service.start_search(
        peer_ref,
        make_query(now),
        scope=scope,
        on_results=batches.append,
    )

    assert [item.message_id for item in results] == [20, 18]
    assert all(item.media_kind is MediaKind.VIDEO for item in results)
    assert all(
        item.media_kind is MediaKind.VIDEO
        for batch in batches
        for item in batch.results
    )
    assert all(
        item.media_kind is MediaKind.VIDEO
        for item in service.catalog.list_results("a1", session.id)
    )
```

- [ ] **Step 3: Run the regression test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py::test_video_search_discards_photos_added_by_album_expansion -q
```

Expected: both cases fail because message `19` is present as `MediaKind.PHOTO` in the stable batch and persisted results.

- [ ] **Step 4: Filter the merged result stream before deduplication**

In `ContentBrowserService._fetch_page`, immediately after extending `expanded` with album values and before `_deduplicate_hits`, add the explicit session invariant:

```python
                allowed_kinds = session.query.filters.media_kinds
                expanded = [
                    hit for hit in expanded if hit.remote.kind in allowed_kinds
                ]
                unique = self._deduplicate_hits(expanded)
```

Do not pass media kinds into `expand_album`; the service owns query policy and the gateway continues to return the complete album.

- [ ] **Step 5: Run the focused service tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py::test_video_search_discards_photos_added_by_album_expansion tests/test_content_browser.py::test_search_pages_expand_albums_deduplicate_and_persist_cursor tests/test_content_browser.py::test_global_albums_with_equal_grouped_id_expand_per_peer -q
```

Expected: `4 passed`.

- [ ] **Step 6: Commit the filtering fix**

```powershell
git add -- tests/test_content_browser.py src/telegram_downloader/content_browser.py
git commit -m "fix: preserve media filters during album expansion"
```

### Task 2: Reconcile incompatible legacy search results

**Files:**
- Modify: `tests/test_catalog.py`
- Modify: `tests/test_content_browser.py`
- Modify: `src/telegram_downloader/catalog.py:623-656`
- Modify: `src/telegram_downloader/content_browser.py:194-197`

- [ ] **Step 1: Write a failing repository reconciliation test**

Add this test to `tests/test_catalog.py`, using the existing `result` fixture helper:

```python
def test_prune_results_outside_stored_media_kinds_is_account_scoped(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    repository.initialize()
    for account_id in ("a1", "a2"):
        repository.upsert_account(AccountProfile(account_id, account_id), now)
    video_only = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 20),
    )
    first = repository.begin_search(
        "s1", "a1", "-1001", "资料群", video_only, now
    )
    second = repository.begin_search(
        "s2", "a2", "-1001", "资料群", video_only, now
    )
    for account_id, session in (("a1", first), ("a2", second)):
        video = replace(
            result(session.id, account_id, now, result_id=f"{account_id}-v"),
            media_kind=MediaKind.VIDEO,
            original_name="video.mp4",
        )
        photo = replace(
            result(
                session.id,
                account_id,
                now,
                result_id=f"{account_id}-p",
                message_id=9,
            ),
            media_kind=MediaKind.PHOTO,
            original_name="photo.jpg",
        )
        repository.save_search_page(
            account_id, session.id, session.generation, [video, photo]
        )
        repository.finish_search(
            account_id,
            session.id,
            session.generation,
            None,
            True,
            now,
        )

    removed = repository.prune_results_by_media_kinds(
        "a1", "s1", frozenset({MediaKind.VIDEO})
    )

    assert removed == 1
    assert [item.media_kind for item in repository.list_results("a1", "s1")] == [
        MediaKind.VIDEO
    ]
    assert repository.get_session("a1", "s1").result_count == 1
    assert len(repository.list_results("a2", "s2")) == 2
    assert repository.get_session("a2", "s2").result_count == 2
```

- [ ] **Step 2: Run the repository test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_catalog.py::test_prune_results_outside_stored_media_kinds_is_account_scoped -q
```

Expected: FAIL with `AttributeError: 'CatalogRepository' object has no attribute 'prune_results_by_media_kinds'`.

- [ ] **Step 3: Implement transactional account-scoped pruning**

Add this method immediately after `list_results` in `CatalogRepository`:

```python
    def prune_results_by_media_kinds(
        self,
        account_id: str,
        search_id: str,
        media_kinds: frozenset[MediaKind],
    ) -> int:
        if not media_kinds:
            raise ValueError("搜索记录必须保留至少一种媒体类型")
        allowed = tuple(sorted(kind.value for kind in media_kinds))
        placeholders = ", ".join("?" for _ in allowed)
        with self._connection() as connection:
            session = connection.execute(
                "SELECT generation FROM search_sessions "
                "WHERE account_id=? AND id=?",
                (account_id, search_id),
            ).fetchone()
            if session is None:
                raise KeyError(search_id)
            generation = int(session["generation"])
            removed = connection.execute(
                "DELETE FROM search_results WHERE account_id=? "
                "AND search_id=? AND generation=? "
                f"AND media_kind NOT IN ({placeholders})",
                (account_id, search_id, generation, *allowed),
            ).rowcount
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM search_results "
                    "WHERE account_id=? AND search_id=? AND generation=?",
                    (account_id, search_id, generation),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE search_sessions SET result_count=? "
                "WHERE account_id=? AND id=? AND generation=?",
                (count, account_id, search_id, generation),
            )
        return max(0, removed)
```

Import `MediaKind` from `telegram_downloader.domain` at the top of `catalog.py` if it is not already imported.

- [ ] **Step 4: Run the repository test and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_catalog.py::test_prune_results_outside_stored_media_kinds_is_account_scoped -q
```

Expected: `1 passed`.

- [ ] **Step 5: Write a failing service-level history test**

Add to `tests/test_content_browser.py`:

```python
@pytest.mark.asyncio
async def test_opening_legacy_video_history_removes_saved_photo_rows(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    service = await prepared_online_service(tmp_path, now, gateway)
    session = service.catalog.begin_search(
        "legacy",
        "a1",
        "-1001",
        "资料群",
        make_query(now),
        now,
    )
    video = service._result_from_hit(
        "a1", session, make_hit(2, now), queued=False
    )
    photo = service._result_from_hit(
        "a1",
        session,
        make_hit(1, now, media_kind=MediaKind.PHOTO),
        queued=False,
    )
    service.catalog.save_search_page(
        "a1", session.id, session.generation, [video, photo]
    )
    service.catalog.finish_search(
        "a1", session.id, session.generation, None, True, now
    )

    visible = service.list_results(session.id)

    assert [item.media_kind for item in visible] == [MediaKind.VIDEO]
    assert service.catalog.get_session("a1", session.id).result_count == 1
```

- [ ] **Step 6: Run the service history test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py::test_opening_legacy_video_history_removes_saved_photo_rows -q
```

Expected: FAIL because the photo remains visible and `result_count` remains `2`.

- [ ] **Step 7: Reconcile rows before returning stored history**

Replace `ContentBrowserService.list_results` with:

```python
    def list_results(self, search_id: str) -> list[SearchResult]:
        account = self._require_account()
        session = self.catalog.get_session(account.account_id, search_id)
        self.catalog.prune_results_by_media_kinds(
            account.account_id,
            search_id,
            session.query.filters.media_kinds,
        )
        return self.catalog.list_results(account.account_id, search_id)
```

At the start of `_fetch_page`, replace the direct `catalog.list_results` call used for `stable_results` with `self.list_results(session.id)`. In the exhausted branch of `load_more`, also replace the direct catalog read with `self.list_results(search_id)`. This makes “open history” and “continue history” share the same invariant.

- [ ] **Step 8: Run catalog and content-service tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_catalog.py tests/test_content_browser.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit legacy reconciliation**

```powershell
git add -- tests/test_catalog.py tests/test_content_browser.py src/telegram_downloader/catalog.py src/telegram_downloader/content_browser.py
git commit -m "fix: reconcile saved search media kinds"
```

### Task 3: Load bounded static video thumbnails

**Files:**
- Modify: `tests/test_gateway.py`
- Modify: `tests/test_content_browser.py`
- Modify: `tests/test_account_wide_search_e2e.py`
- Modify: `src/telegram_downloader/gateway.py:211-220,889-909`
- Modify: `src/telegram_downloader/content_browser.py:764-778`

- [ ] **Step 1: Write failing thumbnail candidate tests**

Add these small test-only classes and test to `tests/test_gateway.py`:

```python
class PhotoSize:
    def __init__(self, kind: str, size: int) -> None:
        self.type = kind
        self.size = size


class VideoSize:
    def __init__(self, kind: str, size: int) -> None:
        self.type = kind
        self.size = size


@pytest.mark.asyncio
async def test_video_thumbnail_skips_video_and_oversized_static_candidates() -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    large = PhotoSize("x", 400)
    small = PhotoSize("m", 40)
    animated = VideoSize("v", 800)
    document = SimpleNamespace(thumbs=(small, animated, large))
    message = media_message(50, now)
    message.media = SimpleNamespace(document=document)
    calls: list[str] = []

    class Client:
        async def get_entity(self, entity):
            assert entity == -1001
            return SimpleNamespace(title="资料群")

        async def get_messages(self, entity, ids):
            assert ids == 50
            return message

        async def download_media(self, media, *, file, thumb):
            assert media is message.media
            assert file is bytes
            calls.append(thumb)
            if thumb == "x":
                return b"\x89PNG\r\n\x1a\n" + (b"x" * 80)
            if thumb == "m":
                return b"\xff\xd8\xff\xe0small-jpeg"
            return b"not-an-image"

    gateway = TelethonGateway.from_client_for_test(Client())

    content = await gateway.load_thumbnail(
        "-1001", 50, "m50", max_bytes=32
    )

    assert content == b"\xff\xd8\xff\xe0small-jpeg"
    assert calls == ["x", "m"]
```

Also change the existing `test_load_thumbnail_validates_current_media_and_returns_only_bytes` fixture so each message media contains `document=SimpleNamespace(thumbs=(PhotoSize("m", 20),))`, its client returns `b"\xff\xd8\xff\xe0jpeg"`, and calls include `max_bytes=256 * 1024`. Update every other direct `gateway.load_thumbnail` call in this test file, including the access-error test, to pass the same keyword argument.

- [ ] **Step 2: Run the gateway tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_gateway.py::test_video_thumbnail_skips_video_and_oversized_static_candidates tests/test_gateway.py::test_load_thumbnail_validates_current_media_and_returns_only_bytes -q
```

Expected: FAIL because `load_thumbnail` does not accept `max_bytes` and still requests `thumb=-1`.

- [ ] **Step 3: Extend the gateway interface and implement static candidate selection**

Change the protocol and concrete method signature to:

```python
    async def load_thumbnail(
        self,
        peer_ref: str,
        message_id: int,
        media_id: str,
        *,
        max_bytes: int,
    ) -> bytes | None: ...
```

Add these helpers to `TelethonGateway` immediately before `load_thumbnail`:

```python
    @staticmethod
    def _static_thumbnail_types(media: object) -> tuple[str, ...]:
        root = getattr(media, "document", None) or getattr(media, "photo", None)
        values = (
            getattr(root, "thumbs", None)
            or getattr(root, "sizes", None)
            or ()
        )
        excluded = {"VideoSize", "PhotoSizeEmpty", "PhotoPathSize"}

        def score(candidate: object) -> int:
            width = getattr(candidate, "w", 0)
            height = getattr(candidate, "h", 0)
            if isinstance(width, int) and isinstance(height, int) and width and height:
                return width * height
            sizes = getattr(candidate, "sizes", ()) or ()
            if sizes:
                return max(int(value) for value in sizes)
            size = getattr(candidate, "size", 0)
            if isinstance(size, int):
                return size
            content = getattr(candidate, "bytes", b"")
            return len(content) if isinstance(content, bytes) else 0

        candidates = [
            item
            for item in values
            if type(item).__name__ not in excluded
            and isinstance(getattr(item, "type", None), str)
            and getattr(item, "type")
        ]
        candidates.sort(key=score, reverse=True)
        return tuple(dict.fromkeys(str(item.type) for item in candidates))

    @staticmethod
    def _is_raster_thumbnail(content: bytes) -> bool:
        return (
            content.startswith(b"\xff\xd8\xff")
            or content.startswith(b"\x89PNG\r\n\x1a\n")
            or (
                len(content) >= 12
                and content.startswith(b"RIFF")
                and content[8:12] == b"WEBP"
            )
        )
```

Replace the single `download_media(..., thumb=-1)` call with:

```python
            if max_bytes < 1:
                raise ValueError("缩略图字节上限必须大于零")
            for thumb_type in self._static_thumbnail_types(media):
                downloaded = await self._client.download_media(
                    media,
                    file=bytes,
                    thumb=thumb_type,
                )
                if (
                    isinstance(downloaded, bytes)
                    and len(downloaded) <= max_bytes
                    and self._is_raster_thumbnail(downloaded)
                ):
                    return downloaded
            return None
```

Keep entity resolution, current-message lookup, media-ID validation, and exception mapping around this block unchanged.

- [ ] **Step 4: Run gateway tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_gateway.py -q
```

Expected: all gateway tests pass.

- [ ] **Step 5: Write a failing service limit-propagation assertion**

Change `FakeGateway.load_thumbnail` in `tests/test_content_browser.py` to record the limit:

```python
        self.thumbnail_limits: list[int] = []

    async def load_thumbnail(
        self, peer_ref, message_id, media_id, *, max_bytes
    ):
        self.thumbnail_calls.append(message_id)
        self.thumbnail_limits.append(max_bytes)
        value = self.thumbnail_values.get(message_id)
        if isinstance(value, BaseException):
            raise value
        return value
```

In `test_thumbnail_cache_hit_remote_fallback_and_history_cleanup`, use JPEG-signature bytes for successful remote thumbnails and assert:

```python
    assert gateway.thumbnail_limits == [thumbnails.max_item_bytes, thumbnails.max_item_bytes]
```

The two values correspond to the successful remote request and the explicit failed request; the cache hit must not add a limit entry.

- [ ] **Step 6: Run the service thumbnail test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py::test_thumbnail_cache_hit_remote_fallback_and_history_cleanup -q
```

Expected: FAIL because the service does not pass `max_bytes`.

- [ ] **Step 7: Pass the cache item limit and update gateway fakes**

Change the service call in `_load_thumbnail_remote` to:

```python
                content = await gateway.load_thumbnail(
                    peer_ref,
                    message_id,
                    media_id,
                    max_bytes=self.thumbnails.max_item_bytes,
                )
```

Update the concurrency fake in `tests/test_content_browser.py` to preserve its synchronization while accepting the limit:

```python
        async def load_thumbnail(
            self, _peer_ref, _message_id, _media_id, *, max_bytes
        ):
            assert max_bytes > 0
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 4:
                reached_four.set()
            await release.wait()
            self.active -= 1
            return b"\xff\xd8\xff\xe0thumbnail"
```

Update the two monkeypatched functions near `thumbnail_service` to accept arbitrary keywords:

```python
    async def load_thumbnail(*_args, **kwargs):
        nonlocal calls
        assert kwargs["max_bytes"] > 0
        calls += 1
        started.set()
        await release.wait()
        return b"\xff\xd8\xff\xe0image"

    async def missing(*_args, **kwargs):
        nonlocal calls
        assert kwargs["max_bytes"] > 0
        calls += 1
        return None
```

Finally, change the account-wide E2E fake to:

```python
    async def load_thumbnail(
        self, peer_ref, message_id, media_id, *, max_bytes
    ):
        assert max_bytes > 0
        return None
```

- [ ] **Step 8: Run all thumbnail-related tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_gateway.py tests/test_content_browser.py tests/test_account_wide_search_e2e.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit static thumbnail loading**

```powershell
git add -- tests/test_gateway.py tests/test_content_browser.py tests/test_account_wide_search_e2e.py src/telegram_downloader/gateway.py src/telegram_downloader/content_browser.py
git commit -m "fix: load static Telegram video thumbnails"
```

### Task 4: Collapse result content and improve fallback icons

**Files:**
- Modify: `tests/ui/test_content_models.py`
- Modify: `src/telegram_downloader/ui/content_models.py:241-321,447-455`

- [ ] **Step 1: Write the failing result-model contract test**

Update the result-model assertions in `tests/ui/test_content_models.py` to this seven-column contract:

```python
    assert model.HEADERS == (
        "选择",
        "预览",
        "内容",
        "日期",
        "类型",
        "大小",
        "状态",
    )
    assert model.data(model.index(0, 2), CONTENT_TITLE_ROLE) == "联系人"
    assert model.data(model.index(0, 2), CONTENT_EXCERPT_ROLE) == results[0].excerpt
    assert model.data(model.index(0, 3)) == "2026-08-15\n13:17"
    assert model.data(model.index(1, 5)) == "未知"
    assert model.data(model.index(3, 6)) == "已入队"
```

Import `CONTENT_EXCERPT_ROLE` and `CONTENT_TITLE_ROLE` from `telegram_downloader.ui.content_models`. Replace old tooltip column assertions with one assertion that column 2 contains the complete source, excerpt, peer reference, and original name.

- [ ] **Step 2: Write a failing video fallback-icon test**

Add:

```python
def test_video_fallback_icon_contains_a_visible_play_mark(qtbot) -> None:
    now = datetime(2026, 8, 15, 13, 17, tzinfo=UTC)
    model = SearchResultTableModel()
    model.set_results([result(now, "video", 1)])

    icon = model.data(model.index(0, 1), Qt.ItemDataRole.DecorationRole)
    image = icon.pixmap(88, 60).toImage()
    light_pixels = sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).lightness() >= 220
    )

    assert icon.isNull() is False
    assert light_pixels >= 20
```

- [ ] **Step 3: Run model tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_models.py -q
```

Expected: FAIL because the model still has eight columns, custom roles are missing, and the video fallback is a solid block.

- [ ] **Step 4: Add content roles and the seven-column mapping**

Add near the top of `content_models.py`:

```python
CONTENT_TITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
CONTENT_EXCERPT_ROLE = int(Qt.ItemDataRole.UserRole) + 2
```

Change `SearchResultTableModel.HEADERS` and the relevant `data` branches to:

```python
    HEADERS = ("选择", "预览", "内容", "日期", "类型", "大小", "状态")

        if role == CONTENT_TITLE_ROLE and index.column() == 2:
            return result.source_title or result.peer_ref
        if role == CONTENT_EXCERPT_ROLE and index.column() == 2:
            return result.excerpt
        if role == Qt.ItemDataRole.ToolTipRole:
            if index.column() == 2:
                source_title = result.source_title or result.peer_ref
                return (
                    f"{_SOURCE_LABELS[result.source_kind]}：{source_title}"
                    f"\n会话标识：{result.peer_ref}"
                    f"\n\n{result.excerpt}"
                    f"\n\n文件：{result.original_name}"
                )
            return result.original_name
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            "",
            "",
            "",
            result.message_date_utc.strftime("%Y-%m-%d\n%H:%M"),
            _MEDIA_LABELS[result.media_kind],
            self._format_bytes(result.expected_size),
            self._status_text(result),
        )
```

Check-state and decoration columns remain `0` and `1`.

- [ ] **Step 5: Draw a media card and video play triangle**

Add `QPainter`, `QPainterPath`, and `QPen` imports from `PySide6.QtGui`. Replace `_fallback_icon` with:

```python
    def _fallback_icon(self, kind: MediaKind) -> QIcon:
        icon = self._fallback_icons.get(kind)
        if icon is not None:
            return icon
        pixmap = QPixmap(88, 60)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#CBD5E1"), 1))
        painter.setBrush(QColor(_MEDIA_COLORS[kind]))
        painter.drawRoundedRect(1, 1, 86, 58, 7, 7)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FFFFFF"))
        if kind is MediaKind.VIDEO:
            play = QPainterPath()
            play.moveTo(36, 19)
            play.lineTo(36, 41)
            play.lineTo(56, 30)
            play.closeSubpath()
            painter.drawPath(play)
        else:
            painter.setPen(QColor("#FFFFFF"))
            font = painter.font()
            font.setBold(True)
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(
                pixmap.rect(),
                Qt.AlignmentFlag.AlignCenter,
                _MEDIA_LABELS[kind],
            )
        painter.end()
        icon = QIcon(pixmap)
        self._fallback_icons[kind] = icon
        return icon
```

- [ ] **Step 6: Run model tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_models.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the result-model contract**

```powershell
git add -- tests/ui/test_content_models.py src/telegram_downloader/ui/content_models.py
git commit -m "feat: clarify account content result cells"
```

### Task 5: Add a responsive wrapped-content delegate

**Files:**
- Create: `tests/ui/test_content_result_delegate.py`
- Modify: `tests/ui/test_content_browser.py`
- Create: `src/telegram_downloader/ui/content_result_delegate.py`
- Modify: `src/telegram_downloader/ui/content_browser.py:289-319`

- [ ] **Step 1: Write the failing delegate height test**

Create `tests/ui/test_content_result_delegate.py`:

```python
from dataclasses import replace
from datetime import UTC, datetime

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QStyleOptionViewItem, QTableView

from telegram_downloader.ui.content_models import SearchResultTableModel
from telegram_downloader.ui.content_result_delegate import ResultContentDelegate
from tests.ui.test_content_browser import result


def test_long_source_title_increases_content_row_height(qtbot) -> None:
    now = datetime(2026, 8, 22, 13, 17, tzinfo=UTC)
    model = SearchResultTableModel()
    model.set_results(
        [
            replace(
                result(now, "long", 1),
                source_title="这是一个需要完整换行显示的很长Telegram会话标题" * 3,
                excerpt="摘要保持单行并在空间不足时省略",
            )
        ]
    )
    table = QTableView()
    qtbot.addWidget(table)
    table.setModel(model)
    delegate = ResultContentDelegate(table)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 220, 78)

    size = delegate.sizeHint(option, model.index(0, 2))

    assert size.height() > 78
```

- [ ] **Step 2: Update the page layout test to the desired column contract**

In `tests/ui/test_content_browser.py`, change fixed widths to:

```python
    fixed_widths = {
        0: 44,
        1: 96,
        3: 96,
        4: 56,
        5: 82,
        6: 68,
    }
```

Assert column `2` uses `QHeaderView.ResizeMode.Stretch`, `page.content_delegate` is a `ResultContentDelegate`, and the long-title minimum-size test checks `columnWidth(2) >= 180` with no horizontal scrollbar.

- [ ] **Step 3: Run the new UI tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_result_delegate.py tests/ui/test_content_browser.py::test_page_contains_content_browser_controls tests/ui/test_content_browser.py::test_result_columns_do_not_squeeze_fixed_text_at_minimum_size -q
```

Expected: collection fails because `content_result_delegate` does not exist, or layout assertions fail against the old columns.

- [ ] **Step 4: Implement the content delegate**

Create `src/telegram_downloader/ui/content_result_delegate.py`:

```python
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from telegram_downloader.ui.content_models import (
    CONTENT_EXCERPT_ROLE,
    CONTENT_TITLE_ROLE,
)


class ResultContentDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._height_cache: dict[tuple[str, int, str], int] = {}

    def clear_size_cache(self, *_args) -> None:
        self._height_cache.clear()

    @staticmethod
    def _title_font(base: QFont) -> QFont:
        font = QFont(base)
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def _title_height(
        self,
        index: QModelIndex,
        option: QStyleOptionViewItem,
        width: int,
    ) -> int:
        title = str(index.data(CONTENT_TITLE_ROLE) or "")
        result_id = str(index.data(Qt.ItemDataRole.UserRole) or index.row())
        key = (result_id, width, title)
        cached = self._height_cache.get(key)
        if cached is not None:
            return cached
        metrics = QFontMetrics(self._title_font(option.font))
        bounds = metrics.boundingRect(
            QRect(0, 0, max(1, width), 10_000),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            | int(Qt.TextFlag.TextWordWrap),
            title,
        )
        height = max(metrics.height(), bounds.height())
        self._height_cache[key] = height
        return height

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> QSize:
        width = max(160, option.rect.width() - 20)
        title_height = self._title_height(index, option, width)
        excerpt_height = QFontMetrics(option.font).height()
        return QSize(option.rect.width(), max(78, title_height + excerpt_height + 18))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        base.text = ""
        style = base.widget.style() if base.widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            base,
            painter,
            base.widget,
        )
        content = base.rect.adjusted(10, 6, -10, -6)
        title = str(index.data(CONTENT_TITLE_ROLE) or "")
        excerpt = str(index.data(CONTENT_EXCERPT_ROLE) or "")
        title_font = self._title_font(base.font)
        title_height = self._title_height(index, base, content.width())
        title_rect = QRect(
            content.left(), content.top(), content.width(), title_height
        )
        selected = bool(base.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setClipRect(base.rect)
        painter.setFont(title_font)
        painter.setPen(
            base.palette.highlightedText().color()
            if selected
            else base.palette.text().color()
        )
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            | int(Qt.TextFlag.TextWordWrap),
            title,
        )
        excerpt_metrics = QFontMetrics(base.font)
        excerpt_text = excerpt_metrics.elidedText(
            excerpt,
            Qt.TextElideMode.ElideRight,
            content.width(),
        )
        painter.setFont(base.font)
        painter.setPen(
            base.palette.highlightedText().color()
            if selected
            else QColor("#64748B")
        )
        painter.drawText(
            QRect(
                content.left(),
                title_rect.bottom() + 4,
                content.width(),
                excerpt_metrics.height(),
            ),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            excerpt_text,
        )
        painter.restore()
```

- [ ] **Step 5: Install the delegate and responsive widths**

In `ui/content_browser.py`, remove the `QCheckBox` import only after Task 6 replaces it. For this task, import `ResultContentDelegate`, then change the result-table configuration to:

```python
        self.result_table.setIconSize(QSize(88, 60))
        self.result_table.verticalHeader().setDefaultSectionSize(78)
        self.result_table.verticalHeader().setMinimumSectionSize(78)
        self.result_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.result_table.setWordWrap(False)
        self.result_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.content_delegate = ResultContentDelegate(self.result_table)
        self.result_table.setItemDelegateForColumn(2, self.content_delegate)
        self.result_model.modelReset.connect(self.content_delegate.clear_size_cache)
        self.result_model.rowsInserted.connect(self.content_delegate.clear_size_cache)
        self.result_model.rowsRemoved.connect(self.content_delegate.clear_size_cache)
        self.result_model.dataChanged.connect(self.content_delegate.clear_size_cache)
        result_header = self.result_table.horizontalHeader()
        result_header.setMinimumSectionSize(40)
        for column, width in {
            0: 44,
            1: 96,
            3: 96,
            4: 56,
            5: 82,
            6: 68,
        }.items():
            result_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.result_table.setColumnWidth(column, width)
        result_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
```

Keep `FullCellCheckDelegate` on column `0` and preview handling on column `1`.

- [ ] **Step 6: Run the focused UI tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_result_delegate.py tests/ui/test_content_browser.py tests/ui/test_content_models.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the responsive result layout**

```powershell
git add -- tests/ui/test_content_result_delegate.py tests/ui/test_content_browser.py src/telegram_downloader/ui/content_result_delegate.py src/telegram_downloader/ui/content_browser.py
git commit -m "fix: make account result content responsive"
```

### Task 6: Draw check marks in media filter options

**Files:**
- Create: `tests/ui/test_tick_checkbox.py`
- Create: `src/telegram_downloader/ui/tick_checkbox.py`
- Modify: `src/telegram_downloader/ui/content_browser.py:8-32,251-257`

- [ ] **Step 1: Write the failing rendered-indicator test**

Create `tests/ui/test_tick_checkbox.py`:

```python
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QStyle, QStyleOptionButton

from telegram_downloader.ui.theme import APP_STYLESHEET
from telegram_downloader.ui.tick_checkbox import TickCheckBox


def _indicator_colors(check: TickCheckBox) -> tuple[int, int]:
    option = QStyleOptionButton()
    check.initStyleOption(option)
    rect = check.style().subElementRect(
        QStyle.SubElement.SE_CheckBoxIndicator,
        option,
        check,
    )
    image = check.grab().toImage()
    inner = rect.adjusted(2, 2, -2, -2)
    light = sum(
        1
        for y in range(inner.top(), inner.bottom() + 1)
        for x in range(inner.left(), inner.right() + 1)
        if image.pixelColor(x, y).lightness() >= 220
    )
    accent = sum(
        1
        for y in range(inner.top(), inner.bottom() + 1)
        for x in range(inner.left(), inner.right() + 1)
        if image.pixelColor(x, y).blue() >= 140
        and image.pixelColor(x, y).red() <= 60
    )
    return light, accent


def test_checked_media_option_draws_a_white_tick(qtbot) -> None:
    check = TickCheckBox("视频")
    check.setStyleSheet(APP_STYLESHEET)
    check.resize(90, 32)
    qtbot.addWidget(check)
    check.show()
    qtbot.wait(20)

    check.setChecked(True)
    check.repaint()
    light, accent = _indicator_colors(check)

    assert check.checkState() == Qt.CheckState.Checked
    assert light >= 6
    assert accent >= 20
```

- [ ] **Step 2: Run the widget test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_tick_checkbox.py -q
```

Expected: collection fails because `telegram_downloader.ui.tick_checkbox` does not exist.

- [ ] **Step 3: Implement the themed tick checkbox**

Create `src/telegram_downloader/ui/tick_checkbox.py`:

```python
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QCheckBox, QStyle, QStyleOptionButton, QWidget


class TickCheckBox(QCheckBox):
    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self.checkState() is not Qt.CheckState.Checked:
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            option,
            self,
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#FFFFFF"), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        path = QPainterPath()
        path.moveTo(QPointF(indicator.left() + 3.5, indicator.center().y()))
        path.lineTo(QPointF(indicator.left() + 7.0, indicator.bottom() - 3.5))
        path.lineTo(QPointF(indicator.right() - 2.5, indicator.top() + 3.5))
        painter.drawPath(path)
        painter.end()
```

- [ ] **Step 4: Run the widget test and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_tick_checkbox.py -q
```

Expected: `1 passed`.

- [ ] **Step 5: Replace only account-content media checkboxes**

In `ui/content_browser.py`, import `TickCheckBox`, change the annotation and constructor to:

```python
        self.media_checks: dict[MediaKind, TickCheckBox] = {}
        for kind in MediaKind:
            check = TickCheckBox(_MEDIA_LABELS[kind])
            check.setChecked(True)
            self.media_checks[kind] = check
            media_row.addWidget(check)
```

Remove `QCheckBox` from the PySide widget imports because this page no longer uses it.

- [ ] **Step 6: Run account-content UI tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_tick_checkbox.py tests/ui/test_content_browser.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the checked-state rendering**

```powershell
git add -- tests/ui/test_tick_checkbox.py src/telegram_downloader/ui/tick_checkbox.py src/telegram_downloader/ui/content_browser.py
git commit -m "feat: draw ticks in media filter options"
```

### Task 7: Run full verification and inspect the integrated diff

**Files:**
- Verify all files changed in Tasks 1-6.

- [ ] **Step 1: Run all focused regression tests together**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_catalog.py tests/test_gateway.py tests/test_content_browser.py tests/test_account_wide_search_e2e.py tests/ui/test_content_models.py tests/ui/test_content_result_delegate.py tests/ui/test_tick_checkbox.py tests/ui/test_content_browser.py -q
```

Expected: exit code `0` with no failures or errors.

- [ ] **Step 2: Run static checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
```

Expected: `All checks passed!`.

- [ ] **Step 3: Run the complete automated suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: exit code `0` with no failures or errors.

- [ ] **Step 4: Verify source compilation and whitespace**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

Expected: both commands exit `0` and produce no error output.

- [ ] **Step 5: Inspect final scope and history**

Run:

```powershell
git status --short
git log -7 --oneline --decorate
```

Expected: the worktree is clean, and the six implementation commits follow the plan/design commits without unrelated files.

- [ ] **Step 6: Review requirements line by line**

Confirm from fresh test output and the final diff:

```text
[ ] Result table has seven columns and content column 2 stretches.
[ ] Long source titles wrap and can increase row height.
[ ] Account-content media options render white ticks when checked.
[ ] Mixed albums cannot add photos to video-only searches.
[ ] Stored wrong-type rows are pruned with result_count repaired.
[ ] Thumbnail candidates exclude video data and oversized images.
[ ] Video rows show a play-mark fallback only when no raster thumbnail exists.
```

If any item cannot be supported by the diff and fresh verification output, return to its owning task before reporting completion.
