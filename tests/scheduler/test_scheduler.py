"""Tests for TaskScheduler — scheduling logic, lifecycle, and execution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from openjarvis.scheduler.scheduler import ScheduledTask, TaskScheduler
from openjarvis.scheduler.store import SchedulerStore


@pytest.fixture()
def store(tmp_path):
    s = SchedulerStore(tmp_path / "scheduler_test.db")
    yield s
    s.close()


@pytest.fixture()
def scheduler(store):
    sched = TaskScheduler(store, poll_interval=1)
    yield sched
    sched.stop()


# -- ScheduledTask dataclass -------------------------------------------------


class TestScheduledTask:
    def test_round_trip(self):
        task = ScheduledTask(
            id="abc123",
            prompt="hello",
            schedule_type="interval",
            schedule_value="60",
            agent="orchestrator",
            tools="calculator,think",
            metadata={"key": "value"},
        )
        d = task.to_dict()
        restored = ScheduledTask.from_dict(d)
        assert restored.id == "abc123"
        assert restored.prompt == "hello"
        assert restored.schedule_type == "interval"
        assert restored.agent == "orchestrator"
        assert restored.tools == "calculator,think"
        assert restored.metadata == {"key": "value"}

    def test_defaults(self):
        task = ScheduledTask(
            id="x",
            prompt="p",
            schedule_type="once",
            schedule_value="2026-01-01T00:00:00",
        )
        assert task.context_mode == "isolated"
        assert task.status == "active"
        assert task.agent == "simple"
        assert task.tools == ""
        assert task.metadata == {}


# -- TaskScheduler create/list -----------------------------------------------


class TestCreateAndList:
    def test_create_task(self, scheduler):
        task = scheduler.create_task(
            prompt="hello world",
            schedule_type="interval",
            schedule_value="3600",
        )
        assert task.id
        assert task.prompt == "hello world"
        assert task.schedule_type == "interval"
        assert task.next_run is not None
        assert task.status == "active"

    def test_create_task_with_agent_and_tools(self, scheduler):
        task = scheduler.create_task(
            prompt="hello",
            schedule_type="once",
            schedule_value="2099-01-01T00:00:00+00:00",
            agent="orchestrator",
            tools="calculator,think",
        )
        assert task.agent == "orchestrator"
        assert task.tools == "calculator,think"

    def test_list_tasks_empty(self, scheduler):
        assert scheduler.list_tasks() == []

    def test_list_tasks(self, scheduler):
        scheduler.create_task("a", "interval", "60")
        scheduler.create_task("b", "interval", "120")
        assert len(scheduler.list_tasks()) == 2

    def test_list_tasks_filter_status(self, scheduler):
        t1 = scheduler.create_task("a", "interval", "60")
        scheduler.create_task("b", "interval", "120")
        scheduler.pause_task(t1.id)
        active = scheduler.list_tasks(status="active")
        paused = scheduler.list_tasks(status="paused")
        assert len(active) == 1
        assert len(paused) == 1


# -- Pause / resume / cancel -------------------------------------------------


class TestPauseResumeCancel:
    def test_pause_task(self, scheduler):
        task = scheduler.create_task("test", "interval", "60")
        scheduler.pause_task(task.id)
        tasks = scheduler.list_tasks(status="paused")
        assert len(tasks) == 1
        assert tasks[0].status == "paused"

    def test_resume_task(self, scheduler):
        task = scheduler.create_task("test", "interval", "60")
        scheduler.pause_task(task.id)
        scheduler.resume_task(task.id)
        tasks = scheduler.list_tasks(status="active")
        assert len(tasks) == 1

    def test_cancel_task(self, scheduler):
        task = scheduler.create_task("test", "interval", "60")
        scheduler.cancel_task(task.id)
        tasks = scheduler.list_tasks(status="cancelled")
        assert len(tasks) == 1
        assert tasks[0].next_run is None

    def test_pause_nonexistent(self, scheduler):
        with pytest.raises(KeyError):
            scheduler.pause_task("nonexistent")

    def test_resume_nonexistent(self, scheduler):
        with pytest.raises(KeyError):
            scheduler.resume_task("nonexistent")

    def test_cancel_nonexistent(self, scheduler):
        with pytest.raises(KeyError):
            scheduler.cancel_task("nonexistent")


# -- _compute_next_run -------------------------------------------------------


class TestComputeNextRun:
    def test_interval(self, scheduler):
        task = ScheduledTask(
            id="t", prompt="p", schedule_type="interval", schedule_value="300"
        )
        next_run = scheduler._compute_next_run(task)
        assert next_run is not None
        # Should be roughly 300 seconds from now
        parsed = datetime.fromisoformat(next_run)
        diff = (parsed - datetime.now(timezone.utc)).total_seconds()
        assert 295 <= diff <= 310

    def test_once_not_yet_run(self, scheduler):
        target = "2099-06-15T12:00:00+00:00"
        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="once",
            schedule_value=target,
            last_run=None,
        )
        next_run = scheduler._compute_next_run(task)
        assert next_run == target

    def test_once_already_run(self, scheduler):
        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="once",
            schedule_value="2099-06-15T12:00:00+00:00",
            last_run="2099-06-15T12:01:00+00:00",
        )
        next_run = scheduler._compute_next_run(task)
        assert next_run is None

    def test_cron_fallback(self, scheduler):
        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="cron",
            schedule_value="30 2 * * *",
        )
        next_run = scheduler._compute_next_run(task)
        assert next_run is not None

    def test_cron_honors_metadata_timezone(self, scheduler, monkeypatch):
        fixed_now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="cron",
            schedule_value="0 5 * * *",
            metadata={"timezone": "America/Los_Angeles"},
        )
        monkeypatch.setattr("openjarvis.scheduler.scheduler.datetime", FixedDatetime)

        next_run = scheduler._compute_next_run(task)

        assert next_run == "2026-01-15T13:00:00+00:00"

    def test_cron_without_dependency_fails_loudly(self, scheduler, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def without_croniter(name, *args, **kwargs):
            if name == "croniter":
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", without_croniter)
        with pytest.raises(RuntimeError, match="croniter is required"):
            scheduler._compute_next_cron(
                "0 5 * * *",
                datetime(2026, 1, 15, tzinfo=timezone.utc),
            )

    def test_unknown_type(self, scheduler):
        task = ScheduledTask(
            id="t", prompt="p", schedule_type="unknown", schedule_value="x"
        )
        assert scheduler._compute_next_run(task) is None


# -- _execute_task -----------------------------------------------------------


class TestExecuteTask:
    def test_execute_with_system(self, store):
        mock_system = MagicMock()
        mock_system.ask.return_value = "result text"
        sched = TaskScheduler(store, system=mock_system, poll_interval=1)

        task = sched.create_task("what is 2+2?", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        mock_system.ask.assert_called_once()
        call_args = mock_system.ask.call_args
        assert call_args[0][0] == "what is 2+2?"

        # Check that a run log was recorded
        logs = store.get_run_logs(task.id)
        assert len(logs) == 1
        assert logs[0]["success"] == 1
        assert logs[0]["result"] == "result text"

    def test_execute_without_system(self, store):
        sched = TaskScheduler(store, poll_interval=1)
        task = sched.create_task("dry run", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        logs = store.get_run_logs(task.id)
        assert len(logs) == 1
        assert logs[0]["success"] == 1
        assert "dry-run" in logs[0]["result"]

    def test_execute_with_error(self, store):
        mock_system = MagicMock()
        mock_system.ask.side_effect = RuntimeError("engine down")
        sched = TaskScheduler(store, system=mock_system, poll_interval=1)

        task = sched.create_task("fail", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        logs = store.get_run_logs(task.id)
        assert len(logs) == 1
        assert logs[0]["success"] == 0
        assert "engine down" in logs[0]["error"]

    def test_execute_publishes_events(self, store):
        mock_bus = MagicMock()
        sched = TaskScheduler(store, bus=mock_bus, poll_interval=1)
        task = sched.create_task("test", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        assert mock_bus.publish.call_count == 2
        start_call = mock_bus.publish.call_args_list[0]
        end_call = mock_bus.publish.call_args_list[1]
        assert start_call[0][0] == "scheduler_task_start"
        assert end_call[0][0] == "scheduler_task_end"

    def test_execute_once_task_completed_after_run(self, store):
        sched = TaskScheduler(store, poll_interval=1)
        task = sched.create_task("one-shot", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        updated = store.get_task(task.id)
        assert updated["status"] == "completed"
        assert updated["next_run"] is None

    def test_execute_with_tools(self, store):
        mock_system = MagicMock()
        mock_system.ask.return_value = "4"
        sched = TaskScheduler(store, system=mock_system, poll_interval=1)

        task = sched.create_task(
            "what is 2+2?",
            "once",
            "2026-01-01T00:00:00+00:00",
            tools="calculator,think",
        )
        sched._execute_task(task)

        call_kwargs = mock_system.ask.call_args[1]
        assert call_kwargs["tools"] == ["calculator", "think"]


# -- Start / stop lifecycle ---------------------------------------------------


class TestLifecycle:
    def test_start_stop(self, scheduler):
        scheduler.start()
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()
        scheduler.stop()
        assert not scheduler._thread

    def test_double_start(self, scheduler):
        scheduler.start()
        t1 = scheduler._thread
        scheduler.start()  # Should not create a second thread
        assert scheduler._thread is t1
        scheduler.stop()

    def test_poll_loop_invokes_poll_once(self, store, monkeypatch):
        sched = TaskScheduler(store, poll_interval=60)
        calls = {"n": 0}

        def _once() -> list[str]:
            calls["n"] += 1
            sched._stop_event.set()
            return []

        monkeypatch.setattr(sched, "poll_once", _once)
        sched._poll_loop()
        assert calls["n"] == 1


# -- Due slots, grace, and timezones ------------------------------------------


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


class TestDueSlots:
    def test_poll_once_runs_a_recent_miss_and_does_not_repeat(self, store):
        now = _utc(2026, 6, 1, 12, 0)
        system = MagicMock()
        system.ask.return_value = {"content": "result text"}
        sched = TaskScheduler(
            store,
            system,
            clock=lambda: now,
            misfire_grace_seconds=7200,
        )
        task = sched.create_task("immediate", "once", "2026-06-01T11:30:00+00:00")

        assert sched.poll_once() == [task.id]
        system.ask.assert_called_once()
        logs = store.get_run_logs(task.id)
        assert logs[0]["success"] == 1
        assert logs[0]["result"] == "result text"
        assert sched.poll_once() == []
        assert system.ask.call_count == 1

    def test_outside_grace_skips_without_executing(self, store):
        now = _utc(2026, 6, 1, 12, 0)
        system = MagicMock()
        system.ask.return_value = "should not run"
        sched = TaskScheduler(
            store,
            system,
            clock=lambda: now,
            misfire_grace_seconds=3600,
        )
        task = sched.create_task("late", "once", "2026-06-01T09:00:00+00:00")

        assert sched.poll_once() == [task.id]
        system.ask.assert_not_called()
        updated = store.get_task(task.id)
        assert updated["status"] == "completed"
        assert updated["next_run"] is None
        logs = store.get_run_logs(task.id)
        assert logs[0]["success"] == 0
        assert "misfire grace" in logs[0]["error"]

    def test_grace_boundary_is_inclusive(self, tmp_path):
        scheduled = _utc(2026, 4, 1, 6, 0)
        grace = 3600

        def run_at(moment: datetime) -> bool:
            db = SchedulerStore(tmp_path / f"{int(moment.timestamp())}.db")
            system = MagicMock()
            system.ask.return_value = "ok"
            sched = TaskScheduler(
                db,
                system,
                clock=lambda: moment,
                misfire_grace_seconds=grace,
            )
            sched.create_task("boundary", "once", scheduled.isoformat())
            sched.poll_once()
            called = system.ask.called
            db.close()
            return called

        assert run_at(scheduled + timedelta(seconds=grace)) is True
        assert run_at(scheduled + timedelta(seconds=grace + 1)) is False

    def test_slow_interval_is_not_due_again_immediately(self, store):
        start = _utc(2026, 6, 1, 12, 0)
        clock = {"now": start}
        system = MagicMock()

        def _ask(*_args, **_kwargs):
            clock["now"] = start + timedelta(seconds=90)
            return "done"

        system.ask.side_effect = _ask
        sched = TaskScheduler(
            store,
            system,
            clock=lambda: clock["now"],
            misfire_grace_seconds=7200,
        )
        task = sched.create_task("tick", "interval", "60")
        row = store.get_task(task.id)
        assert row is not None
        row["next_run"] = start.isoformat()
        store.update_task(row)

        assert sched.poll_once() == [task.id]
        updated = store.get_task(task.id)
        assert updated is not None
        assert datetime.fromisoformat(updated["next_run"]) == start + timedelta(
            seconds=150
        )
        assert sched.poll_once() == []

    def test_zero_grace_runs_however_late(self, store):
        now = _utc(2026, 6, 1, 12, 0)
        system = MagicMock()
        system.ask.return_value = "caught up"
        sched = TaskScheduler(
            store,
            system,
            clock=lambda: now,
            misfire_grace_seconds=0,
        )
        task = sched.create_task("ancient", "once", "2020-01-01T00:00:00+00:00")
        assert sched.poll_once() == [task.id]
        system.ask.assert_called_once()

    def test_claim_is_exclusive_across_connections(self, tmp_path):
        path = tmp_path / "scheduler.db"
        store_a = SchedulerStore(path)
        store_b = SchedulerStore(path)
        now = _utc(2026, 3, 1, 12, 0)
        sched = TaskScheduler(store_a, clock=lambda: now)
        task = sched.create_task("p", "once", "2026-03-01T11:00:00+00:00")

        first = store_a.claim_due_task(
            task.id,
            task.next_run or "",
            new_next_run=None,
            claimed_at=now.isoformat(),
            new_status="completed",
        )
        second = store_b.claim_due_task(
            task.id,
            task.next_run or "",
            new_next_run=None,
            claimed_at=now.isoformat(),
            new_status="completed",
        )

        assert first is True
        assert second is False
        store_a.close()
        store_b.close()

    def test_dry_run_does_not_invoke_success_callback(self, store):
        called: list[str] = []
        sched = TaskScheduler(
            store,
            on_success=lambda _task, text: called.append(text),
        )
        task = sched.create_task("dry", "once", "2020-01-01T00:00:00+00:00")
        sched._execute_task(task)
        assert called == []

    @pytest.mark.parametrize(
        "local_fire",
        [
            datetime(2026, 1, 15, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem")),
            datetime(2026, 7, 15, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem")),
        ],
    )
    def test_cron_fires_in_configured_timezone(self, store, local_fire):
        before = (local_fire - timedelta(minutes=1)).astimezone(timezone.utc)
        due = (local_fire + timedelta(seconds=5)).astimezone(timezone.utc)
        clock = {"now": before}
        system = MagicMock()
        system.ask.return_value = {"content": "בוקר טוב"}
        sched = TaskScheduler(
            store,
            system,
            clock=lambda: clock["now"],
            misfire_grace_seconds=7200,
        )
        task = sched.create_task(
            "Generate my morning digest",
            "cron",
            "0 8 * * *",
            agent="morning_digest",
            metadata={"timezone": "Asia/Jerusalem"},
        )
        assert datetime.fromisoformat(task.next_run) == local_fire.astimezone(
            timezone.utc
        )

        clock["now"] = due
        delivered: dict[str, str] = {}
        sched.set_on_success(lambda _task, text: delivered.setdefault("text", text))
        assert sched.poll_once() == [task.id]
        system.ask.assert_called_once()
        assert delivered["text"] == "בוקר טוב"

        # The slot was claimed. A restart at the same clock must not fire again.
        assert sched.poll_once() == []
        assert system.ask.call_count == 1
        updated = store.get_task(task.id)
        next_local = datetime.fromisoformat(updated["next_run"]).astimezone(
            ZoneInfo("Asia/Jerusalem")
        )
        assert (next_local.hour, next_local.minute) == (8, 0)
        assert next_local.date() == (local_fire + timedelta(days=1)).date()

    def test_invalid_timezone_is_utc_and_logged_once(self, store, caplog):
        fixed = _utc(2026, 1, 15, 12, 0)
        sched = TaskScheduler(store, clock=lambda: fixed)
        bad_name = "Not/ARealZone"
        with caplog.at_level("ERROR"):
            bad = sched.create_task(
                "p",
                "cron",
                "0 8 * * *",
                metadata={"timezone": bad_name},
            )
            good = sched.create_task(
                "p",
                "cron",
                "0 8 * * *",
                metadata={"timezone": "UTC"},
            )
        assert bad.next_run == good.next_run
        assert bad_name in caplog.text
        assert "UTC" in caplog.text
