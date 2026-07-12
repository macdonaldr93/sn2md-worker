# CLAUDE.md — workflows

DBOS workflow modules. Every workflow follows the same three
conventions.

## 1. Wrapper + impl split

```python
@DBOS.workflow()
def convert_note(file_id: str, source_path: str) -> None:
    convert_note_impl(
        file_id=file_id,
        source_path=source_path,
        source=get_drive_client(),
        settings=get_settings(),
    )


def convert_note_impl(
    *,
    file_id: str,
    source_path: str,
    source: NoteSource,
    settings: Settings,
) -> None:
    ...
```

- Public wrapper: positional args (matches `DBOS.enqueue_workflow`
  ergonomics) and no dependency on singletons at the call site.
- `_impl`: kwargs, takes the injected dependency and `settings` (and
  `now` where a workflow uses wall-clock). Tests call the impl directly
  with fakes.
- The seam'd impls (`convert_note`, `backfill`, `delete_output`) take
  `source: NoteSource` (the ingestion seam from `sources/`); tests mock
  the protocol with `MagicMock(spec=NoteSource)`. The
  `poll_changes` / `renew_watch_channel` impls still take
  `drive: DriveClient` on purpose: the change-feed/watch machinery is
  Drive infrastructure that gets deleted entirely (not reimplemented)
  if a local source ever replaces Drive.
- `set_drive_client`, `set_settings`, `set_engine` are wired at startup
  by `__main__.py`; wrappers reach them via `get_*()`. The wrapper is
  the composition point: it passes `get_drive_client()` as `source=`
  for the seam'd workflows.

## 2. Structured log events + correlation

Every impl body opens a `bound_contextvars` scope so subsequent log
calls inherit the workflow context automatically:

```python
def convert_note_impl(*, file_id, source_path, source, settings):
    key = logical_key(source_path)
    with structlog.contextvars.bound_contextvars(
        workflow="convert_note", file_id=file_id, logical_key=key
    ):
        _log.info("convert_note_started")
        try:
            ...
            _log.info("convert_note_succeeded")
        except Exception as exc:
            _log.error("convert_note_failed", error=str(exc), exc_info=True)
            raise
```

Skips use `reason=`:

```python
_log.info("convert_note_skipped", reason="up_to_date")
```

Levels: INFO on happy path, WARNING for graceful degradation
(configuration missing, safety guard tripped), ERROR for failures.

HTTP requests get an outer `request_id` from
`app.CorrelationIdMiddleware`; workflows run on DBOS worker threads
that don't share those contextvars, so cross-boundary tracing uses
`file_id` / `logical_key`.

## 3. Enqueueing from another workflow

Use the queue constants from `workflows/__init__.py`:

```python
DBOS.enqueue_workflow(CONVERT_QUEUE_NAME, convert_note, file_id, source_path)
DBOS.enqueue_workflow(POLL_QUEUE_NAME, poll_changes, "webhook")
```

Queues (registered by `workflows.register_queues()` after
`DBOS.launch()`):

- `convert_queue` — `worker_concurrency=settings.queue.convert_concurrency`
  (default 2). Serves `convert_note` only.
- `delete_queue` — `worker_concurrency=2`. Serves `delete_output` on
  its own queue so a batch of long conversions can't block the fast
  filesystem-only deletes (a stale delete arriving mid-backfill
  completes without waiting).
- `poll_queue` — `worker_concurrency=1`. Serves `poll_changes` and
  `backfill`.

Schedules (registered by `workflows.register_schedules()`, each
pre-checked idempotently via `DBOS.get_schedule`):

- `renew-watch-channel` — cron `0 6 * * *` (daily 06:00 UTC). Runs
  `renew_watch_channel`.
- `fallback-poll-changes` — cron from `settings.drive.fallback_poll_cron`
  (default `*/5 * * * *`). Runs `scheduled_poll_changes`, which enqueues
  `poll_changes("fallback")` onto `poll_queue`. The safety net for push
  notifications Google silently dropped (or stopped delivering) while
  the process stayed up — the one case the boot-time recovery paths
  can't cover because there was never a restart.

## Startup helpers exposed by this package

Called from `__main__.py` in order after `DBOS.launch()`:

1. `register_queues()` — creates the three DBOS queues.
2. `register_schedules()` — installs the daily renewal cron and the
   fallback poll cron; pre-checks each via `DBOS.get_schedule` before
   calling `create_schedule` so a re-boot on the same DB is a no-op
   instead of raising.
3. `seed_cursor_if_ready(drive)` — writes an initial `drive_change_cursor`
   row from `get_start_page_token()` if none exists (dev-friendly:
   skips if DriveClient isn't available).
4. `ensure_active_channel_if_ready(drive, settings)` — delegates to
   `renew_watch_channel_impl` with `trigger_source="startup"` so the
   first channel is created without waiting for the cron. Also
   enqueues a `poll_changes("recovery")` if the previously-active
   channel expired while we were down.
5. `enqueue_startup_backfill()` — enqueues `backfill` on `poll_queue`.

## Recovery behaviors worth naming

- **Cursor expired** (Drive 4xx on `changes.list`): `poll_changes`
  catches `DrivePermanentError`, resets the cursor via
  `get_start_page_token`, and enqueues `backfill` so nothing is lost.
- **Missed create-side push after `delete_output._repoint`**:
  `delete_output` enqueues `convert_note(live.id, source_path)` after
  repointing, so even if `poll_changes` never saw the replacement, the
  vault stays fresh.
- **Renewal safety**: `renew_watch_channel` uses a two-phase pending
  row — insert BEFORE the Drive call, `confirm` after — so a crash
  mid-renewal doesn't drop notifications. `RENEWAL_HEADROOM = 48h`
  gives the daily cron a two-day cushion.
- **Renewal seam**: when `renew_watch_channel_impl` replaces an existing
  channel (near-expiry or webhook-URL change) it enqueues a catch-up
  `poll_changes("renewal")` so changes landing between the old channel's
  last push and the new channel going active aren't lost. Skipped on
  first-ever creation (`active is None`) since the startup backfill
  already covers the initial state.
- **Dropped push notifications**: the `fallback-poll-changes` cron polls
  Drive on `settings.drive.fallback_poll_cron` regardless of channel
  health, so a push Google silently dropped (or stopped delivering to a
  still-valid channel) while the process stayed up self-heals within one
  cron interval instead of stalling until the next restart.

## Deferred workflows

- `debounce_file` — the `debounce_state` table and repo are in place
  but the workflow itself isn't built. `poll_changes` enqueues
  `convert_note` directly. Add when a real partial-upload conversion
  problem appears.
