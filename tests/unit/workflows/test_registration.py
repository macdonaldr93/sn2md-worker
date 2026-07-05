"""BDD tests for `workflows.register_schedules` idempotency.

The startup entrypoint calls `register_schedules()` every boot; DBOS
persists rows in `workflow_schedules`, so on a second boot the row
already exists. The register step must be a no-op in that case.
"""

from __future__ import annotations

from unittest.mock import patch

from sn2md_worker.workflows import (
    RENEW_SCHEDULE_CRON,
    RENEW_SCHEDULE_NAME,
    register_schedules,
    renew_watch_channel,
)


class TestRegisterSchedulesOnAFreshDatabase:
    def test_calls_create_schedule_with_the_configured_cron(self) -> None:
        # GIVEN — no existing schedule row
        with (
            patch(
                "sn2md_worker.workflows.DBOS.get_schedule",
                return_value=None,
            ) as get_sched,
            patch(
                "sn2md_worker.workflows.DBOS.create_schedule",
            ) as create_sched,
        ):
            # WHEN
            register_schedules()

        # THEN
        get_sched.assert_called_once_with(RENEW_SCHEDULE_NAME)
        create_sched.assert_called_once()
        kwargs = create_sched.call_args.kwargs
        assert kwargs["schedule_name"] == RENEW_SCHEDULE_NAME
        assert kwargs["workflow_fn"] is renew_watch_channel
        assert kwargs["schedule"] == RENEW_SCHEDULE_CRON
        assert kwargs["context"] == "cron"


class TestRegisterSchedulesWhenScheduleAlreadyExists:
    def test_pre_check_short_circuits_create_schedule(self) -> None:
        # GIVEN — DBOS reports the schedule already exists
        with (
            patch(
                "sn2md_worker.workflows.DBOS.get_schedule",
                return_value={"schedule_name": RENEW_SCHEDULE_NAME},
            ),
            patch(
                "sn2md_worker.workflows.DBOS.create_schedule",
            ) as create_sched,
        ):
            # WHEN
            register_schedules()

        # THEN — no create_schedule call means we won't hit the DBOS
        # duplicate-key error, no matter what its message format becomes.
        create_sched.assert_not_called()
