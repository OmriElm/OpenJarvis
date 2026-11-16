"""Tests for the scheduler hosted by ``jarvis serve``."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from openjarvis.core.config import JarvisConfig
from openjarvis.scheduler.service import (
    scheduler_is_enabled,
    start_background_scheduler,
)


def test_disabled_config_does_not_start(tmp_path) -> None:
    cfg = JarvisConfig()
    cfg.digest.enabled = False
    cfg.scheduler.enabled = False
    cfg.scheduler.db_path = str(tmp_path / "scheduler.db")
    assert scheduler_is_enabled(cfg) is False
    assert start_background_scheduler(cfg, MagicMock()) is None


def test_digest_enabled_syncs_once_and_survives_a_restart(tmp_path) -> None:
    cfg = JarvisConfig()
    cfg.digest.enabled = True
    cfg.digest.schedule = "0 8 * * *"
    cfg.digest.timezone = "Asia/Jerusalem"
    cfg.scheduler.db_path = str(tmp_path / "scheduler.db")
    cfg.scheduler.poll_interval = 60
    system = MagicMock()
    system.ask.return_value = {"content": "digest"}

    def clock() -> datetime:
        # Well before 08:00 Asia/Jerusalem, so the synced task is not due.
        return datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)

    started = start_background_scheduler(cfg, system, clock=clock)
    assert started is not None
    scheduler, store = started
    try:
        tasks = scheduler.list_tasks(status="active")
        assert len(tasks) == 1
        assert tasks[0].metadata["timezone"] == "Asia/Jerusalem"
        assert tasks[0].agent == "morning_digest"
        task_id = tasks[0].id
        next_run = tasks[0].next_run
    finally:
        scheduler.stop()
        store.close()

    restarted = start_background_scheduler(cfg, system, clock=clock)
    assert restarted is not None
    scheduler, store = restarted
    try:
        tasks = scheduler.list_tasks(status="active")
        assert len(tasks) == 1
        assert tasks[0].id == task_id
        assert tasks[0].next_run == next_run
        system.ask.assert_not_called()
    finally:
        scheduler.stop()
        store.close()


def test_scheduler_enabled_without_digest_still_starts(tmp_path) -> None:
    cfg = JarvisConfig()
    cfg.digest.enabled = False
    cfg.scheduler.enabled = True
    cfg.scheduler.db_path = str(tmp_path / "scheduler.db")
    started = start_background_scheduler(cfg, MagicMock())
    assert started is not None
    scheduler, store = started
    try:
        assert scheduler.list_tasks() == []
    finally:
        scheduler.stop()
        store.close()
