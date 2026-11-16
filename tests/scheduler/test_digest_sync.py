"""Tests for aligning the digest task with configuration."""

from __future__ import annotations

from datetime import datetime, timezone

from openjarvis.core.config import DigestConfig
from openjarvis.scheduler.digest_sync import sync_digest_task
from openjarvis.scheduler.scheduler import TaskScheduler
from openjarvis.scheduler.store import SchedulerStore

_NOW = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)


def _scheduler(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    return store, TaskScheduler(store, clock=lambda: _NOW)


def test_sync_is_idempotent_and_keeps_next_run(tmp_path) -> None:
    store, sched = _scheduler(tmp_path)
    digest = DigestConfig(
        enabled=True,
        schedule="0 8 * * *",
        timezone="Asia/Jerusalem",
    )
    try:
        first = sync_digest_task(sched, digest)
        assert first.task_id is not None
        next_run = store.get_task(first.task_id)["next_run"]
        second = sync_digest_task(sched, digest)
        assert second.task_id == first.task_id
        assert second.cancelled == 0
        assert store.get_task(first.task_id)["next_run"] == next_run
        assert len(sched.list_tasks(status="active")) == 1
    finally:
        store.close()


def test_schedule_change_keeps_the_task_id(tmp_path) -> None:
    store, sched = _scheduler(tmp_path)
    try:
        first = sync_digest_task(
            sched,
            DigestConfig(enabled=True, schedule="0 8 * * *", timezone="UTC"),
        )
        previous = store.get_task(first.task_id)["next_run"]
        second = sync_digest_task(
            sched,
            DigestConfig(enabled=True, schedule="30 9 * * *", timezone="UTC"),
        )
        assert second.task_id == first.task_id
        updated = store.get_task(first.task_id)
        assert updated["schedule_value"] == "30 9 * * *"
        assert updated["next_run"] != previous
        local = datetime.fromisoformat(updated["next_run"]).astimezone(timezone.utc)
        assert (local.hour, local.minute) == (9, 30)
    finally:
        store.close()


def test_pause_is_respected_until_an_explicit_reschedule(tmp_path) -> None:
    store, sched = _scheduler(tmp_path)
    digest = DigestConfig(enabled=True, schedule="0 8 * * *", timezone="UTC")
    try:
        created = sync_digest_task(sched, digest)
        sched.pause_task(created.task_id)
        held = sync_digest_task(sched, digest, respect_pause=True)
        assert held.task_id is None
        assert sched.list_tasks(status="active") == []
        assert len(sched.list_tasks(status="paused")) == 1

        forced = sync_digest_task(sched, digest, respect_pause=False)
        assert forced.task_id is not None
        assert forced.cancelled == 1
        assert len(sched.list_tasks(status="active")) == 1
        assert sched.list_tasks(status="paused") == []
    finally:
        store.close()


def test_disable_cancels_the_managed_task(tmp_path) -> None:
    store, sched = _scheduler(tmp_path)
    try:
        sync_digest_task(
            sched,
            DigestConfig(enabled=True, schedule="0 8 * * *", timezone="UTC"),
        )
        result = sync_digest_task(
            sched,
            DigestConfig(enabled=False),
            respect_pause=False,
        )
        assert result.task_id is None
        assert result.cancelled == 1
        assert sched.list_tasks(status="active") == []
    finally:
        store.close()


def test_invalid_cron_leaves_tasks_unchanged(tmp_path) -> None:
    store, sched = _scheduler(tmp_path)
    try:
        result = sync_digest_task(
            sched,
            DigestConfig(enabled=True, schedule="not a cron", timezone="UTC"),
        )
        assert result.task_id is None
        assert sched.list_tasks() == []
    finally:
        store.close()
