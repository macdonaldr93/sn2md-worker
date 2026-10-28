# CLAUDE.md — workflows

DBOS workflow modules. Every workflow follows the same conventions.

## 1. Wrapper, impl, and step boundaries

```python
@DBOS.workflow()
def convert_note(file_id: str, source_path: str, correlation_id: str | None = None) -> None:
    with structlog.contextvars.bound_contextvars(dbos_workflow_id=DBOS.workflow_id):
        convert_note_impl(
            file_id=file_id,
            source_path=source_path,
            source=get_drive_client(),
            settings=get_settings(),
            correlation_id=correlation_id,
        )


def convert_note_impl(
    *,
    file_id: str,
    source_path: str,
    source: NoteSource,
    settings: Settings,
    correlation_id: str | None = None,
) -> None:
    ...
```

- **Public wrapper**: positional args (matching `DBOS.enqueue_workflow`
  ergonomics) with `correlation_id` as the trailing defaulted argument so
  DBOS recovery of pre-upgrade workflows remains compatible. Binds
  `dbos_workflow_id` into structlog contextvars.
- **`_impl`**: keyword-only args taking injected dependencies (`source:
  NoteSource` for `convert_note`, `backfill`, `delete_output`; `drive:
  DriveClient` for `poll_changes`, `renew_watch_channel`) plus `settings`.
  Unit tests call `_impl` directly with fakes/mocks.
- **Scheduler wrappers** (`scheduled_poll_changes`, `scheduled_backfill`,
  `renew_watch_channel`): keep the `(scheduled_time, context)` signature
  required by `DBOS.create_schedule` and mint a fresh `correlation_id` per
  tick.
- **`@DBOS.step()` boundaries**: DBOS assigns sequential `function_id`s to
  every step and child `DBOS.enqueue_workflow` call inside a workflow and
  verifies them against `operation_outputs` on recovery replay. Any
  non-deterministic I/O (Drive API calls, SQLite reads/writes, filesystem
  checks) that determines whether or what to enqueue MUST run inside a
  `@DBOS.step()` returning a frozen dataclass (or primitive), with
  `DBOS.enqueue_workflow` called from the enclosing workflow loop.

## 2. Structured log events + correlation

Every impl opens a `bound_contextvars` scope and logs `<workflow>_started`,
`<workflow>_succeeded`, `<workflow>_failed` (`exc_info=True`), or
`<workflow>_skipped(reason=…)`:

- `request_id` is HTTP-scoped (bound by `RequestIdMiddleware` and echoed
  in `X-Request-Id`).
- `correlation_id` traces one logical operation across DBOS queues. It is
  minted at root triggers (webhook receipt, cron ticks, startup helpers)
  and passed as the trailing workflow argument to every child enqueue.

## 3. Queues and schedules

Use the queue constants from `workflows/queues.py` and pass `correlation_id`
through on every child enqueue. Conversion enqueues use
`SetEnqueueOptions(deduplication_id=f"{file_id}:{source_path}")` to drop
redundant enqueues while a conversion for the same file and path is
already queued or running.

Queues:
- `convert_queue` (`worker_concurrency=settings.queue.convert_concurrency`,
  default 2) — runs `convert_note`.
- `delete_queue` (`worker_concurrency=2`) — runs `delete_output`
  independently so long Gemini conversions never block vault deletes.
- `poll_queue` (`worker_concurrency=1`) — serializes `poll_changes` and
  `backfill`.

Schedules (idempotent; updated in place if the configured cron changes):
- `renew-watch-channel` (`0 6 * * *`) — daily watch-channel renewal check.
- `fallback-poll-changes` (`settings.drive.fallback_poll_cron`, default
  `*/5 * * * *`) — enqueues `poll_changes("fallback")` on `poll_queue` to
  catch dropped Drive push notifications.
- `backfill-sweep` (`settings.drive.backfill_sweep_cron`, default
  `0 5 * * *`) — enqueues `backfill` on `poll_queue` daily to retry
  conversions that previously failed.

## 4. Startup sequence after `DBOS.launch()`

1. Cancel any active (`ENQUEUED` / `PENDING` / `DELAYED`) workflows left by
   a different `DBOS.application_version` so their queue deduplication IDs
   are released.
2. Register queues and schedules.
3. Ensure an active watch channel exists (and enqueue a recovery poll if
   the prior channel expired while the worker was down).
4. Enqueue the startup `backfill`.

## 5. Recovery behaviors

- **Cursor expired** (Drive 4xx on `changes.list`): resets cursor from
  Drive's current start token and enqueues `backfill`.
- **Supernote replace-on-sync**: `delete_output` checks Drive for a live
  file with the same name in the same parent folder before deleting vault
  output; if found, it repoints the conversion record to the new `fileId`
  and enqueues `convert_note`.
- **Drive move or rename**: when `convert_note` sees an existing conversion
  record for the same `fileId` at a different `logical_key`, it seeds
  cached pages from the old vault folder and removes the old vault folder
  and stale DB rows once conversion finishes. Moving a note outside the
  source folder triggers `delete_output`.
- **Watch-channel renewal**: serialized by an advisory file lock and uses
  two-phase persistence (pending row written before calling Drive,
  confirmed and marked active after) so pushes arriving mid-handshake
  still authenticate. Replacing an existing channel enqueues a catch-up
  poll.
- **Dropped pushes & failed conversions**: covered while the process stays
  up by the `fallback-poll-changes` and `backfill-sweep` schedules.

## 6. Deferred workflows

- `debounce_file` — schema and repository exist, but `poll_changes`
  enqueues `convert_note` directly because Drive only notifies on
  completed uploads.
