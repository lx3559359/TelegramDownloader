# Search Flood-Wait Recovery Design

## Status

Approved for implementation on 2026-08-20.

## Problem

The account-content search field accepts a total result limit of up to 10,000 and defaults to 500, while the Telegram gateway scans at most 100 messages per page. Reaching 500 results therefore requires multiple page requests.

The Telethon client is configured with `flood_sleep_threshold=0`, so every Telegram `FloodWaitError` is surfaced immediately. Download and subscription schedulers already understand this error, but foreground search currently marks the search incomplete and stops. The UI reports the required wait in seconds, does not count down, does not retry, and hides the load-more action for incomplete searches.

As a result, the 500-result setting is logically valid but unreliable during real Telegram use, especially after repeated searches or multiple page loads.

## Goals

- Automatically wait and retry foreground search when Telegram requests a wait of 120 seconds or less.
- Show a one-second countdown with the current retry attempt.
- Retry at most twice for one search-page operation.
- Keep cancellation responsive during the countdown.
- Retry from the same persisted search cursor without duplicating saved results.
- Preserve previously completed pages when the wait is too long or retries are exhausted.
- Allow an incomplete search to continue later from its saved cursor.
- Record only non-sensitive operational details about the flood wait.

## Non-goals

- Automatically load all pages up to the requested result limit after one click. Existing explicit pagination remains unchanged.
- Change Telegram's server-side rate limits or attempt to bypass them.
- Retry authorization failures, access-denied errors, ordinary network failures, or expired media references under this policy.
- Change download or automatic-subscription retry behavior.
- Persist a countdown across application restarts.

## Selected Approach

Implement a retry state machine in `ContentBrowserService`, around one complete search-page operation. A page operation includes the 100-message gateway search, album expansion, deduplication, result planning, and the catalog writes. All Telegram calls happen before the current page is written to the catalog. A `FloodWaitError` can therefore retry from the session's existing cursor without duplicating a partially saved page.

This is preferred over Telethon's automatic flood sleeping because the application must display countdown progress and enforce its own retry cap. It is preferred over controller-level restart because restarting would create duplicate search sessions and lose precise cursor ownership.

## Retry Policy

Introduce an immutable search retry policy with these defaults:

- `maximum_wait_seconds = 120`
- `maximum_retries = 2`

For each page operation:

1. Run the page operation once.
2. If it raises `FloodWaitError` with a wait from 1 through 120 seconds and fewer than two retries have occurred, count down and retry the same page operation.
3. Emit one progress update per second using the existing `SearchProgress` channel. The phase text is `Telegram 限流，{remaining} 秒后自动重试（{attempt}/2）`.
4. Use an injected asynchronous sleep function so countdown behavior is deterministic in tests.
5. Let `asyncio.CancelledError` propagate immediately. Cancellation during sleep therefore needs no polling or special cancellation flag.
6. If the wait exceeds 120 seconds or the third page attempt also receives a flood wait, do not sleep again. Save the session as incomplete with its existing cursor and a safe wait message.

The retry count applies to one foreground page operation. A later user-initiated continuation starts with a fresh two-retry allowance.

## Search State and Continuation

Successful pages retain the existing behavior:

- Results are deduplicated by `(peer_ref, message_id, media_id)`.
- At most 100 new results are accepted per page.
- The persisted cursor advances only after the page is successfully organized and saved.
- The search completes when Telegram is exhausted or the configured result limit is reached.

When automatic flood-wait recovery cannot continue:

- The page operation returns the persisted incomplete session and all previously saved results instead of discarding the active context.
- The controller displays `last_error`, makes that session active, and refreshes search history.
- The result table continues to show all successful prior pages.
- The load-more action is available for non-exhausted incomplete sessions and is labelled `继续搜索`.
- Continuing calls the existing `load_more` path with the saved cursor. A successful continuation moves the session back to running or completed status.

If the first page is rate-limited beyond the policy, the incomplete session is still visible with zero results and can be continued later. This avoids creating a new history record for every manual retry.

## Progress and UI Behavior

The existing indeterminate search progress bar remains in use. No new widget is required.

- Normal scanning continues to display inspected and matched counts.
- Countdown progress keeps the latest available inspected and matched counts and changes only the phase text.
- The cancel button remains visible and cancels both scanning and countdown sleep.
- After a terminal flood wait, the busy state ends and the safe wait message remains visible.
- Opening an incomplete search from history exposes `继续搜索` when the account is logged in and the session is not exhausted.

## Logging and Privacy

Write a warning through the existing redacting application logger when a flood wait is received. The record includes:

- wait seconds;
- retry number or terminal outcome;
- numeric cursor offset, or zero for the first page.

Do not log the keyword, dialog title, account identifier, message text, filenames, phone number, API credentials, or Telegram session data.

## Error Boundaries

- `FloodWaitError`: handled by this retry policy.
- `SessionExpiredError`: continues through the unified login-recovery flow.
- `TransientNetworkError`: keeps the current safe failure behavior and incomplete state.
- `AccessDeniedError`: keeps the current safe access message.
- `asyncio.CancelledError`: records an incomplete search and propagates cancellation.
- Unexpected exceptions: remain visible through the controller's existing safe-error handling.

## Test Strategy

Add test-first coverage for:

1. A short flood wait emits every countdown value, sleeps one second per value, retries the same cursor, and succeeds.
2. Two short flood waits are retried, while a third flood wait ends the operation as incomplete.
3. A wait longer than 120 seconds performs no sleep and leaves an incomplete, resumable session.
4. Cancellation during countdown propagates and leaves the session incomplete without another gateway attempt.
5. Countdown progress preserves the latest inspected and matched counters.
6. An incomplete session exposes `继续搜索` and can become running or completed after a successful continuation.
7. A first-page terminal flood wait still activates a zero-result history record.
8. Existing pagination reaches a 500-result limit across five successful 100-result pages without duplicates.
9. Logs contain wait seconds, attempt, and cursor but do not contain the search keyword or dialog title.

Run the focused content-browser, controller, gateway, and UI tests first, followed by the complete test and Ruff checks.

## Success Criteria

- A Telegram wait of at most 120 seconds is visibly counted down and automatically retried no more than twice.
- The user can cancel at any point during the wait.
- A terminal wait never deletes results from successful pages and never advances the cursor past an unsaved page.
- The same incomplete search can be continued without creating a duplicate search session.
- The 500-result limit remains paginated in 100-result pages and can complete under intermittent short flood waits.
- No sensitive search or account data is added to logs.
