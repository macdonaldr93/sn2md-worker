from __future__ import annotations

from unittest.mock import patch

import pytest

from sn2md_worker.config import DriveConfig, Settings, set_settings
from sn2md_worker.workflows import (
    BACKFILL_SWEEP_SCHEDULE_NAME,
    FALLBACK_POLL_SCHEDULE_NAME,
    POLL_QUEUE_NAME,
    RENEW_SCHEDULE_CRON,
    RENEW_SCHEDULE_NAME,
    backfill,
    enqueue_startup_backfill,
    register_schedules,
    renew_watch_channel,
    scheduled_backfill,
    scheduled_poll_changes,
)

FALLBACK_CRON = "*/7 * * * *"  # distinctive non-default, so we can assert it flows from settings
BACKFILL_SWEEP_CRON = (
    "0 4 * * *"  # distinctive non-default, so we can assert it flows from settings
)


@pytest.fixture
def settings() -> Settings:
    installed = Settings(
        drive=DriveConfig(
            fallback_poll_cron=FALLBACK_CRON,
            backfill_sweep_cron=BACKFILL_SWEEP_CRON,
        )
    )
    set_settings(installed)
    return installed


class TestRegisterSchedulesOnAFreshDatabase:
    def test_registers_all_schedules_with_their_configured_crons(self, settings: Settings) -> None:
        # GIVEN — no existing schedule rows
        with (
            patch(
                "sn2md_worker.workflows.DBOS.get_schedule",
                return_value=None,
            ),
            patch(
                "sn2md_worker.workflows.DBOS.create_schedule",
            ) as create_sched,
        ):
            # WHEN
            register_schedules()

        # THEN - all three schedules were created
        assert create_sched.call_count == 3
        by_name = {
            call.kwargs["schedule_name"]: call.kwargs for call in create_sched.call_args_list
        }

        renew = by_name[RENEW_SCHEDULE_NAME]
        assert renew["workflow_fn"] is renew_watch_channel
        assert renew["schedule"] == RENEW_SCHEDULE_CRON
        assert renew["context"] == "cron"

        fallback = by_name[FALLBACK_POLL_SCHEDULE_NAME]
        assert fallback["workflow_fn"] is scheduled_poll_changes
        assert fallback["schedule"] == FALLBACK_CRON  # flows from settings
        assert fallback["context"] == "cron"

        sweep = by_name[BACKFILL_SWEEP_SCHEDULE_NAME]
        assert sweep["workflow_fn"] is scheduled_backfill
        assert sweep["schedule"] == BACKFILL_SWEEP_CRON  # flows from settings
        assert sweep["context"] == "cron"


class TestRegisterSchedulesWhenSchedulesAlreadyExist:
    def test_pre_check_short_circuits_create_schedule(self, settings: Settings) -> None:
        # GIVEN — DBOS reports every schedule already exists
        with (
            patch(
                "sn2md_worker.workflows.DBOS.get_schedule",
                return_value={"schedule_name": "any"},
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


class TestRegisterSchedulesWhenOnlyFallbackPollIsMissing:
    def test_creates_only_the_missing_fallback_schedule(self, settings: Settings) -> None:
        # GIVEN - every schedule row exists except the fallback poll
        def existing(schedule_name: str) -> dict[str, str] | None:
            if schedule_name == FALLBACK_POLL_SCHEDULE_NAME:
                return None
            return {"schedule_name": schedule_name}

        with (
            patch(
                "sn2md_worker.workflows.DBOS.get_schedule",
                side_effect=existing,
            ),
            patch(
                "sn2md_worker.workflows.DBOS.create_schedule",
            ) as create_sched,
        ):
            # WHEN
            register_schedules()

        # THEN - only the fallback schedule is created
        create_sched.assert_called_once()
        kwargs = create_sched.call_args.kwargs
        assert kwargs["schedule_name"] == FALLBACK_POLL_SCHEDULE_NAME
        assert kwargs["workflow_fn"] is scheduled_poll_changes
        assert kwargs["schedule"] == FALLBACK_CRON


class TestRegisterSchedulesWhenOnlyBackfillSweepIsMissing:
    def test_creates_only_the_missing_backfill_sweep_schedule(self, settings: Settings) -> None:
        # GIVEN - every schedule row exists except the backfill sweep
        def existing(schedule_name: str) -> dict[str, str] | None:
            if schedule_name == BACKFILL_SWEEP_SCHEDULE_NAME:
                return None
            return {"schedule_name": schedule_name}

        with (
            patch(
                "sn2md_worker.workflows.DBOS.get_schedule",
                side_effect=existing,
            ),
            patch(
                "sn2md_worker.workflows.DBOS.create_schedule",
            ) as create_sched,
        ):
            # WHEN
            register_schedules()

        # THEN - only the backfill-sweep schedule is created
        create_sched.assert_called_once()
        kwargs = create_sched.call_args.kwargs
        assert kwargs["schedule_name"] == BACKFILL_SWEEP_SCHEDULE_NAME
        assert kwargs["workflow_fn"] is scheduled_backfill
        assert kwargs["schedule"] == BACKFILL_SWEEP_CRON


class TestEnqueueStartupBackfill:
    def test_enqueues_backfill_with_a_fresh_correlation_id(self) -> None:
        # GIVEN - startup reached the backfill-enqueue step
        with patch("sn2md_worker.workflows.DBOS.enqueue_workflow") as enqueue:
            # WHEN
            enqueue_startup_backfill()

        # THEN - backfill lands on the poll queue with a non-empty
        # correlation id minted for this root trigger
        enqueue.assert_called_once()
        args = enqueue.call_args.args
        assert args[0] == POLL_QUEUE_NAME
        assert args[1] is backfill
        assert isinstance(args[2], str)
        assert args[2]
