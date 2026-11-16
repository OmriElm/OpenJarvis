"""Task scheduler — cron/interval/once execution with background polling."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openjarvis.scheduler.store import SchedulerStore

logger = logging.getLogger(__name__)

# Timezone names already reported so a bad config does not log on every poll.
_warned_timezones: set[str] = set()

# Event type strings (avoids editing core EventType enum)
SCHEDULER_TASK_START = "scheduler_task_start"
SCHEDULER_TASK_END = "scheduler_task_end"


@dataclass(slots=True)
class ScheduledTask:
    """A task scheduled for future or recurring execution."""

    id: str
    prompt: str
    schedule_type: str  # "cron" | "interval" | "once"
    schedule_value: str  # cron expression, interval seconds, ISO datetime
    context_mode: str = "isolated"
    status: str = "active"
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    agent: str = "simple"
    tools: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict for store persistence."""
        return {
            "id": self.id,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value,
            "context_mode": self.context_mode,
            "status": self.status,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "agent": self.agent,
            "tools": self.tools,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ScheduledTask:
        """Deserialize from a plain dict."""
        return cls(
            id=d["id"],
            prompt=d["prompt"],
            schedule_type=d["schedule_type"],
            schedule_value=d["schedule_value"],
            context_mode=d.get("context_mode", "isolated"),
            status=d.get("status", "active"),
            next_run=d.get("next_run"),
            last_run=d.get("last_run"),
            agent=d.get("agent", "simple"),
            tools=d.get("tools", ""),
            metadata=d.get("metadata", {}),
        )


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into aware UTC.

    Naive values are treated as UTC. A trailing ``Z`` is accepted.
    Unparseable values return ``None`` instead of raising, so one bad row
    cannot stop the poll loop.

    Parameters
    ----------
    value:
        Stored timestamp, or ``None``.

    Returns
    -------
    datetime or None
        Aware UTC datetime, or ``None`` when *value* is empty or invalid.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _interval_after(now: datetime, schedule_value: str) -> str:
    """Return ``now`` plus a positive interval, as UTC ISO-8601."""
    seconds = float(schedule_value)
    if seconds <= 0:
        raise ValueError("interval must be positive")
    return (now + timedelta(seconds=seconds)).isoformat()


def grace_window(seconds: int) -> timedelta | None:
    """Convert a misfire setting into a lateness limit.

    ``seconds <= 0`` means a missed slot always runs once (no upper bound).
    A positive value is the maximum lateness that still runs. Anything
    older is skipped and the schedule advances to the next future slot.

    Parameters
    ----------
    seconds:
        ``scheduler.misfire_grace_seconds``.

    Returns
    -------
    timedelta or None
        The window, or ``None`` when catch-up is unbounded.
    """
    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def classify_misfire(
    scheduled_at: datetime,
    now: datetime,
    grace: timedelta | None,
) -> str:
    """Decide what to do with a stored run time.

    Parameters
    ----------
    scheduled_at:
        The slot stored in ``next_run``.
    now:
        The scheduler clock, aware or naive UTC.
    grace:
        Maximum lateness that still runs. ``None`` always runs one missed
        slot.

    Returns
    -------
    str
        ``"wait"`` when the slot is still in the future, ``"run"`` when it
        should execute once, or ``"skip"`` when it is older than *grace*.
    """
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    else:
        scheduled_at = scheduled_at.astimezone(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if scheduled_at > now:
        return "wait"
    if grace is None or (now - scheduled_at) <= grace:
        return "run"
    return "skip"


def interpret_timezone(name: str | None) -> str | None:
    """Return a usable IANA timezone name, or ``None`` for UTC.

    An empty name means UTC on purpose. An unknown name is logged once
    and also treated as UTC so a bad digest timezone cannot stall polling.

    Parameters
    ----------
    name:
        IANA name from task metadata, or ``None``.

    Returns
    -------
    str or None
        *name* when ZoneInfo accepts it, otherwise ``None``.
    """
    if not name:
        return None
    if name in _warned_timezones:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        _warned_timezones.add(name)
        logger.error(
            "Invalid timezone %r; interpreting cron schedules as UTC",
            name,
        )
        return None
    return name


def coerce_ask_result(result: Any) -> str:
    """Turn a scheduler ``ask`` return value into stored text.

    :meth:`JarvisSystem.ask` returns a dict whose ``content`` is the
    reply. Older callers and tests return a plain string. Non-ASCII
    content is preserved (``ensure_ascii`` is not used).

    Parameters
    ----------
    result:
        Whatever ``system.ask`` returned.

    Returns
    -------
    str
        The text to log and, for a digest, to deliver.
    """
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str):
            return content
        import json

        return json.dumps(result, default=str, ensure_ascii=False)
    if result is None:
        return ""
    return str(result)


class TaskScheduler:
    """Scheduler that polls for due tasks and executes them.

    Parameters
    ----------
    store:
        The persistence backend.
    system:
        Optional ``JarvisSystem`` instance for executing prompts.
    poll_interval:
        Seconds between poll cycles (default 60).
    bus:
        Optional event bus for publishing scheduler events.
    clock:
        Injectable UTC clock. Tests pass this so they do not sleep or
        depend on the wall clock. Aware datetimes are converted to UTC;
        naive datetimes are treated as UTC.
    misfire_grace_seconds:
        Run a due task once when it is at most this many seconds late.
        ``0`` or a negative value means a missed task always runs once,
        however late it is. The default is two hours, which covers a
        machine that was asleep through a morning digest but not one
        that was off for days.
    on_success:
        Called with ``(task, result_text)`` after a real execution
        succeeds. Dry-runs (no ``system``) do not call it. Exceptions
        are logged by type and do not change the task's recorded result.
    """

    def __init__(
        self,
        store: SchedulerStore,
        system: Any = None,
        *,
        poll_interval: int = 60,
        bus: Any = None,
        clock: Callable[[], datetime] | None = None,
        misfire_grace_seconds: int = 7200,
        on_success: Callable[[ScheduledTask, str], None] | None = None,
    ) -> None:
        self._store = store
        self._system = system
        self._poll_interval = poll_interval if poll_interval >= 1 else 60
        if poll_interval < 1:
            logger.error("poll_interval must be >= 1; using 60")
        self._bus = bus
        self._clock = clock
        grace_seconds = _coerce_grace(misfire_grace_seconds)
        self._grace = grace_window(grace_seconds)
        self._on_success = on_success
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._warnings: set[str] = set()

    def set_system(self, system: Any) -> None:
        """Attach the runtime used to execute due tasks.

        ``SystemBuilder`` constructs the scheduler before the system
        exists. Without this, a later ``start()`` would dry-run.
        """
        self._system = system

    def set_on_success(
        self,
        callback: Callable[[ScheduledTask, str], None] | None,
    ) -> None:
        """Set or clear the callback invoked after a successful real run."""
        self._on_success = callback

    # -- Public API ----------------------------------------------------------

    def start(self) -> None:
        """Start the background polling daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="jarvis-scheduler"
        )
        self._thread.start()
        logger.info("Scheduler started (poll_interval=%ds)", self._poll_interval)

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 5)
            self._thread = None
        logger.info("Scheduler stopped")

    def create_task(
        self,
        prompt: str,
        schedule_type: str,
        schedule_value: str,
        **kwargs: Any,
    ) -> ScheduledTask:
        """Create and persist a new scheduled task."""
        task = ScheduledTask(
            id=uuid.uuid4().hex[:16],
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            agent=kwargs.get("agent", "simple"),
            tools=kwargs.get("tools", ""),
            context_mode=kwargs.get("context_mode", "isolated"),
            metadata=kwargs.get("metadata", {}),
        )
        task.next_run = self._compute_next_run(task)
        with self._lock:
            self._store.save_task(task.to_dict())
        return task

    def reschedule_task(
        self,
        task_id: str,
        *,
        schedule_value: str,
        metadata: Dict[str, Any],
    ) -> ScheduledTask:
        """Replace a cron task's expression and recompute ``next_run``.

        Used when the digest config changes. The task id is kept so run
        history stays attached. The task is left ``active``.

        Parameters
        ----------
        task_id:
            Existing task.
        schedule_value:
            New cron expression.
        metadata:
            Replacement metadata. Callers merge keys they want to keep.

        Returns
        -------
        ScheduledTask
            The updated task.

        Raises
        ------
        KeyError
            If *task_id* does not exist.
        RuntimeError
            If cron support is missing or the expression is invalid.
        """
        with self._lock:
            current = self._store.get_task(task_id)
            if current is None:
                raise KeyError(f"Task not found: {task_id}")
            current["schedule_type"] = "cron"
            current["schedule_value"] = schedule_value
            current["metadata"] = metadata
            current["status"] = "active"
            task = ScheduledTask.from_dict(current)
            task.next_run = self._compute_next_run(task)
            self._store.update_task(task.to_dict())
            return task

    def list_tasks(self, *, status: Optional[str] = None) -> List[ScheduledTask]:
        """Return tasks, optionally filtered by *status*."""
        with self._lock:
            rows = self._store.list_tasks(status=status)
        return [ScheduledTask.from_dict(r) for r in rows]

    def pause_task(self, task_id: str) -> None:
        """Pause an active task."""
        with self._lock:
            d = self._store.get_task(task_id)
            if d is None:
                raise KeyError(f"Task not found: {task_id}")
            d["status"] = "paused"
            self._store.update_task(d)

    def resume_task(self, task_id: str) -> None:
        """Resume a paused task."""
        with self._lock:
            d = self._store.get_task(task_id)
            if d is None:
                raise KeyError(f"Task not found: {task_id}")
            d["status"] = "active"
            # Recompute next_run from now
            task = ScheduledTask.from_dict(d)
            task.next_run = self._compute_next_run(task)
            self._store.update_task(task.to_dict())

    def cancel_task(self, task_id: str) -> None:
        """Cancel a task (sets status to cancelled)."""
        with self._lock:
            d = self._store.get_task(task_id)
            if d is None:
                raise KeyError(f"Task not found: {task_id}")
            d["status"] = "cancelled"
            d["next_run"] = None
            self._store.update_task(d)

    # -- Background loop -----------------------------------------------------

    def _poll_loop(self) -> None:
        """Poll for due tasks and execute them until stopped."""
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Scheduler poll error")
            self._stop_event.wait(timeout=self._poll_interval)

    def poll_once(self) -> List[str]:
        """Handle every task that is due at the scheduler clock.

        The first thing the background loop does, so a restart runs a
        missed slot immediately when it is still inside the grace window.
        Each slot is claimed in SQLite before it runs. A second scheduler
        pointed at the same database, or this process after a restart,
        will not execute that slot again.

        Tasks outside the grace window are not executed. Their ``next_run``
        moves to the next future slot and a run log records the skip.
        One-shot tasks that are skipped are marked completed.

        Returns
        -------
        list[str]
            Ids of tasks that were claimed, whether they ran or were
            skipped as too late.
        """
        now = self._now()
        try:
            with self._lock:
                due = self._store.get_due_tasks(now.isoformat())
        except Exception:
            logger.exception("Scheduler failed to read due tasks")
            return []

        handled: List[str] = []
        for task_dict in due:
            task_id = str(task_dict.get("id", ""))
            try:
                task = ScheduledTask.from_dict(task_dict)
                if self._handle_due_task(task, now):
                    handled.append(task.id)
            except Exception:
                logger.exception("Scheduler failed to handle task %s", task_id)
        return handled

    def _handle_due_task(self, task: ScheduledTask, now: datetime) -> bool:
        """Claim and run or skip one due task. Return True when claimed."""
        scheduled = parse_utc(task.next_run)
        if scheduled is None:
            self._warn_once(task.id, "next_run is missing or not ISO-8601")
            return False
        if classify_misfire(scheduled, now, self._grace) == "wait":
            # String ordering in SQLite can surface a row that is not
            # actually due yet when timestamp formats differ.
            return False
        try:
            following = self._compute_following_run(task, now)
        except (RuntimeError, ValueError, TypeError):
            self._warn_once(task.id, "schedule could not be advanced")
            return False
        if task.schedule_type != "once" and following is None:
            self._warn_once(task.id, "schedule produced no following run")
            return False

        action = classify_misfire(scheduled, now, self._grace)
        new_status = "completed" if task.schedule_type == "once" else None
        claimed_at = now.isoformat()
        with self._lock:
            owned = self._store.claim_due_task(
                task.id,
                task.next_run or "",
                new_next_run=following,
                claimed_at=claimed_at,
                new_status=new_status,
            )
        if not owned:
            logger.info("Task %s was already claimed", task.id)
            return False

        if action == "skip":
            late = int((now - scheduled).total_seconds())
            grace_s = int(self._grace.total_seconds()) if self._grace is not None else 0
            logger.info(
                "Task %s is %d seconds late (grace %d); skipping this slot",
                task.id,
                late,
                grace_s,
            )
            with self._lock:
                self._store.log_run(
                    task_id=task.id,
                    started_at=claimed_at,
                    finished_at=claimed_at,
                    success=False,
                    result="",
                    error=(
                        f"missed: overdue by {late}s, "
                        f"outside misfire grace of {grace_s}s"
                    ),
                )
            return True

        logger.info("Running scheduled task %s (agent=%s)", task.id, task.agent)
        self._execute_task(task, advance_schedule=False)
        return True

    def _warn_once(self, task_id: str, message: str) -> None:
        """Log a per-task schedule problem once per process."""
        if task_id in self._warnings:
            return
        self._warnings.add(task_id)
        logger.error("Task %s was not run: %s", task_id, message)

    def _execute_task(
        self,
        task: ScheduledTask,
        *,
        advance_schedule: bool = True,
    ) -> None:
        """Execute one task.

        Parameters
        ----------
        task:
            Task to run. Its prompt and agent are read from this object.
        advance_schedule:
            When true (direct callers, including tests), ``next_run`` is
            recomputed after the run. The poll loop claims the next slot
            first and passes false so a successful run is not advanced
            twice.
        """
        started_at = self._now().isoformat()

        # Publish start event
        if self._bus is not None:
            self._bus.publish(
                SCHEDULER_TASK_START,
                {"task_id": task.id, "prompt": task.prompt},
            )

        success = False
        result_text = ""
        error_text = ""
        ran_for_real = self._system is not None

        try:
            if self._system is not None:
                raw_tools = (
                    task.tools
                    if isinstance(task.tools, list)
                    else task.tools.split(",")
                )
                tools_list = (
                    [t.strip() for t in raw_tools if t.strip()] if task.tools else []
                )
                ask_kwargs: Dict[str, Any] = {
                    "agent": task.agent,
                    "tools": tools_list if tools_list else None,
                }
                meta = task.metadata or {}
                if meta.get("operator_id"):
                    ask_kwargs["system_prompt"] = meta.get("system_prompt", "")
                    ask_kwargs["operator_id"] = meta["operator_id"]
                result_text = coerce_ask_result(
                    self._system.ask(
                        task.prompt,
                        **ask_kwargs,
                    )
                )
            else:
                result_text = f"[dry-run] Would execute: {task.prompt}"
            success = True
        except Exception as exc:
            error_text = str(exc)
            logger.error("Task %s failed: %s", task.id, exc)

        finished_at = self._now().isoformat()

        # Log the run
        with self._lock:
            self._store.log_run(
                task_id=task.id,
                started_at=started_at,
                finished_at=finished_at,
                success=success,
                result=result_text,
                error=error_text,
            )

            # Update task state. The poll loop has already claimed the next
            # slot; advancing again would skip a future occurrence.
            d = self._store.get_task(task.id)
            if d is not None:
                d["last_run"] = finished_at
                if advance_schedule:
                    next_run = self._compute_next_run(ScheduledTask.from_dict(d))
                    d["next_run"] = next_run
                    if next_run is None:
                        d["status"] = "completed"
                elif task.schedule_type == "interval":
                    # The claim stored start+interval so a crash cannot repeat
                    # this slot. A run that lasts longer than the interval
                    # would otherwise be due again on the next poll.
                    try:
                        pushed = _interval_after(
                            self._now(),
                            str(task.schedule_value),
                        )
                    except (TypeError, ValueError):
                        pushed = ""
                    proposed = parse_utc(pushed)
                    current = parse_utc(d.get("next_run"))
                    if proposed is not None and (
                        current is None or proposed >= current
                    ):
                        d["next_run"] = pushed
                self._store.update_task(d)

        # Publish end event
        if self._bus is not None:
            self._bus.publish(
                SCHEDULER_TASK_END,
                {
                    "task_id": task.id,
                    "success": success,
                    "result": result_text,
                    "error": error_text,
                },
            )

        if success and ran_for_real and self._on_success is not None:
            try:
                self._on_success(task, result_text)
            except Exception as exc:
                logger.warning(
                    "Post-run callback for task %s failed (%s)",
                    task.id,
                    type(exc).__name__,
                )

    def _now(self) -> datetime:
        """Return the scheduler clock as aware UTC."""
        if self._clock is not None:
            now = self._clock()
        else:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _compute_following_run(
        self,
        task: ScheduledTask,
        now: datetime,
    ) -> Optional[str]:
        """Return the slot after the current one is consumed.

        Interval tasks are measured from *now*, not from the missed slot,
        so a long outage cannot queue a burst of catch-up runs. Cron uses
        the next future match after *now*. A one-shot task does not run
        again.
        """
        if task.schedule_type == "once":
            return None
        if task.schedule_type == "interval":
            return _interval_after(now, task.schedule_value)
        if task.schedule_type == "cron":
            timezone_name = str((task.metadata or {}).get("timezone") or "") or None
            return self._compute_next_cron(task.schedule_value, now, timezone_name)
        raise ValueError(f"unknown schedule type {task.schedule_type!r}")

    def _compute_next_run(self, task: ScheduledTask) -> Optional[str]:
        """Compute the next run time for a task.

        Returns an ISO 8601 string, or ``None`` if the task should not run again.
        """
        now = self._now()

        if task.schedule_type == "once":
            # If already run, no more runs
            if task.last_run is not None:
                return None
            # Otherwise the schedule_value is the target ISO datetime
            return task.schedule_value

        if task.schedule_type == "interval":
            seconds = float(task.schedule_value)
            next_time = now + timedelta(seconds=seconds)
            return next_time.isoformat()

        if task.schedule_type == "cron":
            timezone_name = str((task.metadata or {}).get("timezone") or "") or None
            return self._compute_next_cron(task.schedule_value, now, timezone_name)

        return None

    @staticmethod
    def _compute_next_cron(
        cron_expr: str,
        now: datetime,
        timezone_name: str | None = None,
    ) -> Optional[str]:
        """Compute the next run time from a cron expression.

        Evaluate in the configured IANA timezone and persist the result in UTC.
        Missing cron support is fatal instead of silently changing semantics.
        """
        try:
            from croniter import croniter  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "croniter is required for cron schedules; reinstall OpenJarvis"
            ) from exc

        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        timezone_name = interpret_timezone(timezone_name)
        local_now = now.astimezone(ZoneInfo(timezone_name)) if timezone_name else now
        next_local = croniter(cron_expr, local_now).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=local_now.tzinfo)
        return next_local.astimezone(timezone.utc).isoformat()


def _coerce_grace(value: Any) -> int:
    """Return an int grace, logging and using two hours when *value* is bad."""
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.error(
            "Invalid misfire_grace_seconds (%s); using 7200",
            type(value).__name__,
        )
        return 7200


__all__ = [
    "SCHEDULER_TASK_END",
    "SCHEDULER_TASK_START",
    "ScheduledTask",
    "TaskScheduler",
    "classify_misfire",
    "coerce_ask_result",
    "grace_window",
    "interpret_timezone",
    "parse_utc",
]
