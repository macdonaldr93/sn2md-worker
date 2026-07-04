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
        drive=get_drive_client(),
        settings=get_settings(),
    )


def convert_note_impl(
    *,
    file_id: str,
    source_path: str,
    drive: DriveClient,
    settings: Settings,
) -> None:
    ...
```

- Public wrapper: positional args (matches `DBOS.enqueue_workflow`
  ergonomics) and no dependency on singletons at the call site.
- `_impl`: kwargs, takes `drive` and `settings` (and `now` where a
  workflow uses wall-clock). Tests call the impl directly with fakes.
- `set_drive_client`, `set_settings`, `set_engine` are wired at startup
  by `__main__.py`; wrappers reach them via `get_*()`.

## 2. Structured log events

Every impl body is:

```python
_log.info("<name>_started", **context)
try:
    ...
    _log.info("<name>_succeeded", **outcome_counters)
except Exception as exc:
    _log.error("<name>_failed", error=str(exc), exc_info=True, **context)
    raise
```

Skips use:

```python
_log.info("<name>_skipped", reason="up_to_date", **context)
```

Levels: INFO on happy path, WARNING for graceful degradation
(configuration missing, safety guard tripped), ERROR for failures.

## 3. Enqueueing from another workflow

Use the queue constants from `workflows/__init__.py`:

```python
DBOS.enqueue_workflow(CONVERT_QUEUE_NAME, convert_note, file_id, source_path)
DBOS.enqueue_workflow(POLL_QUEUE_NAME, poll_changes, "webhook")
```

Queues (registered by `workflows.register_queues()` after
`DBOS.launch()`):

- `convert_queue` — `worker_concurrency=settings.queue.convert_concurrency`
  (default 2). Serves `convert_note` and `delete_output`.
- `poll_queue` — `worker_concurrency=1`. Serves `poll_changes` and
  `backfill`.

Schedule (registered by `workflows.register_schedules()`):

- `renew-watch-channel` — cron `0 6 * * *` (daily 06:00 UTC).

## Startup helpers exposed by this package

Called from `__main__.py` in order after `DBOS.launch()`:

1. `register_queues()` — creates the two DBOS queues.
2. `register_schedules()` — installs the daily cron.
3. `seed_cursor_if_ready(drive)` — writes an initial `drive_change_cursor`
   row from `get_start_page_token()` if none exists (dev-friendly:
   skips if DriveClient isn't available).
4. `ensure_active_channel_if_ready(drive, settings)` — delegates to
   `renew_watch_channel_impl` with `trigger_source="startup"` so the
   first channel is created without waiting for the cron.
5. `enqueue_startup_backfill()` — enqueues `backfill` on `poll_queue`.

## Deferred workflows

- `debounce_file` — the `debounce_state` table and repo are in place
  but the workflow itself isn't built. `poll_changes` enqueues
  `convert_note` directly. Add when a real partial-upload conversion
  problem appears.
