"""``jarvis scheduler start`` executes tasks instead of dry-running."""

from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner

from openjarvis.cli.scheduler_cmd import scheduler
from openjarvis.core.config import JarvisConfig
from openjarvis.scheduler.scheduler import TaskScheduler
from openjarvis.scheduler.store import SchedulerStore


def test_start_attaches_a_system_and_says_tasks_will_run(tmp_path, monkeypatch) -> None:
    store = SchedulerStore(tmp_path / "scheduler.db")
    cfg = JarvisConfig()
    cfg.digest.enabled = False
    cfg.scheduler.misfire_grace_seconds = 7200
    system = MagicMock()
    builder = MagicMock()
    builder.build.return_value = system
    created: dict[str, TaskScheduler] = {}
    real = TaskScheduler

    def factory(*args, **kwargs):
        sched = real(*args, **kwargs)
        created["scheduler"] = sched
        return sched

    monkeypatch.setattr("openjarvis.cli.scheduler_cmd._get_store", lambda: store)
    monkeypatch.setattr("openjarvis.core.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "openjarvis.cli.scheduler_cmd._block_until_signal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("openjarvis.system.SystemBuilder", lambda *a, **k: builder)
    monkeypatch.setattr("openjarvis.scheduler.scheduler.TaskScheduler", factory)

    result = CliRunner().invoke(scheduler, ["start", "--poll-interval", "30"])

    assert result.exit_code == 0, result.output
    assert "Due tasks will be executed" in result.output
    assert created["scheduler"]._system is system
    assert not created["scheduler"]._thread


def test_start_refuses_to_dry_run_when_the_system_cannot_be_built(
    tmp_path, monkeypatch
) -> None:
    store = SchedulerStore(tmp_path / "scheduler.db")
    cfg = JarvisConfig()
    builder = MagicMock()
    builder.build.side_effect = RuntimeError("no engine")

    monkeypatch.setattr("openjarvis.cli.scheduler_cmd._get_store", lambda: store)
    monkeypatch.setattr("openjarvis.core.config.load_config", lambda: cfg)
    monkeypatch.setattr("openjarvis.system.SystemBuilder", lambda *a, **k: builder)

    result = CliRunner().invoke(scheduler, ["start"])

    assert result.exit_code != 0
    assert "No tasks were executed" in result.output
