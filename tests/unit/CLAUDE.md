# CLAUDE.md — tests/unit

261 unit tests, in-memory SQLite. Directory structure mirrors
`src/sn2md_worker/`.

## When to use BDD vs plain functions

**BDD scenario class** — for behavior tests:

```python
class TestWhen<Scenario>:
    def test_<expected_outcome>(self, ...):
        # GIVEN
        ...
        # WHEN
        ...
        # THEN
        ...
```

Use for anything that exercises orchestration or observable behavior:
webhook handlers, workflow impls, repository semantics,
observability endpoints.

**Plain function tests** — for pure logic:

```python
def test_<input_shape>_produces_<output>() -> None:
    assert func(input) == expected
```

Use for path helpers, model alias mapping, `UTCDateTime` TypeDecorator,
`resolve_source_path` — anything where the test is a straight
input/output check with no scenario framing.

## Fixture conventions

- `engine` fixture creates an in-memory SQLite, runs
  `Base.metadata.create_all`, calls `set_engine(eng)` so workflow code
  can find it via `sql_session()`.
- `settings` fixture uses `Settings(...)` with explicit sub-model
  overrides — pydantic-settings' env sources still fire but explicit
  kwargs win.
- `drive` fixture is a `MagicMock(spec=DriveClient)` — methods you use
  should have `.return_value` or `.side_effect` set inside `# GIVEN`.

## Patching external calls

Patch at the workflow module boundary, not the source:

```python
with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
    convert_note_impl(...)
```

For DBOS itself:

```python
with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
    poll_changes_impl(...)
```

## Anti-patterns to avoid

- **Don't** assert on log messages — the event names are refactored
  when we clean up log conventions. Assert on the observable outcome
  (DB state, enqueue calls, files on disk).
- **Don't** import `convert_note` in tests when what you want is
  `convert_note_impl`. The wrapper is decorator-wrapped and requires
  `set_drive_client` / `set_settings` to have been called.
- **Don't** use `session.commit()` in fixtures unless you're
  specifically testing commit semantics — the fixture-scoped session
  disposes cleanly, and workflow code uses `with session.begin():`
  blocks for its own transactions.
- **Don't** repeat setup code across every test in a class — extract a
  `_seed_*` or `_stub_*` helper at module scope.

## Common helpers

Look at the top of each file for module-level helpers named `_make_*`,
`_seed_*`, `_stub_*`, `_file_metadata`. Reuse them; add new ones
liberally when a test grows a distinctive setup.
