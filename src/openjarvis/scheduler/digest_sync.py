"""Keep the persisted morning-digest task aligned with ``[digest]``.

The digest schedule lives in config and is executed by
:class:`~openjarvis.scheduler.scheduler.TaskScheduler`. Creating the task
here means editing ``config.toml`` is enough: ``jarvis serve`` does not
also need ``jarvis digest --schedule``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openjarvis.core.config import DigestConfig
from openjarvis.scheduler.scheduler import ScheduledTask, TaskScheduler

logger = logging.getLogger(__name__)

DIGEST_AGENT = "morning_digest"
DIGEST_PROMPT = "Generate my morning digest"
MANAGED_BY = "digest"


@dataclass(frozen=True, slots=True)
class DigestSyncResult:
    """Outcome of aligning the digest task with configuration."""

    task_id: str | None
    cancelled: int


def sync_digest_task(
    scheduler: TaskScheduler,
    digest: DigestConfig,
    *,
    respect_pause: bool = True,
) -> DigestSyncResult:
    """Make the persisted digest task match *digest*.

    An unchanged active task keeps its stored ``next_run``. Restarting the
    process therefore does not run a slot that was already claimed and does
    not skip a slot that is still due. When *respect_pause* is true, a
    paused digest task is left paused and a second task is not created.
    ``jarvis digest --schedule`` passes false so an explicit change becomes
    active immediately.

    A disabled digest cancels active managed tasks. With *respect_pause*
    false it also cancels paused ones, which is what ``--schedule off``
    does.

    Parameters
    ----------
    scheduler:
        Scheduler that owns the task database.
    digest:
        Digest configuration to apply.
    respect_pause:
        When true, paused managed tasks are not cancelled or replaced.

    Returns
    -------
    DigestSyncResult
        The active task id after the sync, if one remains, and how many
        tasks were cancelled.
    """
    managed = [task for task in scheduler.list_tasks() if _is_managed(task)]
    active = [task for task in managed if task.status == "active"]
    paused = [task for task in managed if task.status == "paused"]
    cancelled = 0

    if not digest.enabled:
        for task in active:
            scheduler.cancel_task(task.id)
            cancelled += 1
        if not respect_pause:
            for task in paused:
                scheduler.cancel_task(task.id)
                cancelled += 1
        if cancelled:
            logger.info("Cancelled %d digest scheduler task(s)", cancelled)
        return DigestSyncResult(task_id=None, cancelled=cancelled)

    schedule = (digest.schedule or "").strip()
    timezone_name = (digest.timezone or "").strip()
    if not _cron_is_valid(schedule):
        logger.error(
            "Digest is enabled but schedule %r is not a valid cron expression; "
            "leaving existing scheduler tasks unchanged",
            schedule,
        )
        current = active[0].id if active else None
        return DigestSyncResult(task_id=current, cancelled=0)

    if respect_pause and not active and paused:
        logger.info(
            "Digest schedule is enabled but the digest task is paused; "
            "not creating another one"
        )
        return DigestSyncResult(task_id=None, cancelled=0)

    if not respect_pause:
        for task in paused:
            scheduler.cancel_task(task.id)
            cancelled += 1

    metadata = {"timezone": timezone_name, "managed_by": MANAGED_BY}
    match = next(
        (task for task in active if _matches(task, schedule, timezone_name)),
        None,
    )
    if match is None and active:
        primary = active[0]
        merged = dict(primary.metadata or {})
        merged.update(metadata)
        try:
            match = scheduler.reschedule_task(
                primary.id,
                schedule_value=schedule,
                metadata=merged,
            )
        except (RuntimeError, ValueError):
            logger.error(
                "Could not reschedule digest task %s; leaving it unchanged",
                primary.id,
            )
            return DigestSyncResult(task_id=primary.id, cancelled=cancelled)
        for extra in active[1:]:
            scheduler.cancel_task(extra.id)
            cancelled += 1
        logger.info(
            "Updated digest task %s to cron %r (%s)",
            match.id,
            schedule,
            timezone_name or "UTC",
        )
        return DigestSyncResult(task_id=match.id, cancelled=cancelled)

    if match is None:
        created = scheduler.create_task(
            prompt=DIGEST_PROMPT,
            schedule_type="cron",
            schedule_value=schedule,
            agent=DIGEST_AGENT,
            metadata=dict(metadata),
        )
        logger.info(
            "Created digest task %s for cron %r (%s)",
            created.id,
            schedule,
            timezone_name or "UTC",
        )
        return DigestSyncResult(task_id=created.id, cancelled=cancelled)

    for extra in active:
        if extra.id != match.id:
            scheduler.cancel_task(extra.id)
            cancelled += 1
    return DigestSyncResult(task_id=match.id, cancelled=cancelled)


def _is_managed(task: ScheduledTask) -> bool:
    """Return True for tasks this module owns.

    Tasks created before ``managed_by`` existed are recognized by the
    digest agent and the prompt ``jarvis digest --schedule`` writes.
    """
    if task.status not in {"active", "paused"}:
        return False
    if task.agent != DIGEST_AGENT:
        return False
    meta: dict[str, Any] = task.metadata or {}
    if meta.get("managed_by") == MANAGED_BY:
        return True
    return task.prompt == DIGEST_PROMPT


def _matches(task: ScheduledTask, schedule: str, timezone_name: str) -> bool:
    meta = task.metadata or {}
    return (
        task.schedule_type == "cron"
        and task.schedule_value == schedule
        and str(meta.get("timezone") or "") == timezone_name
        and bool(task.next_run)
    )


def _cron_is_valid(expression: str) -> bool:
    """Return True when *expression* is a croniter cron expression."""
    if not expression:
        return False
    try:
        from croniter import croniter
    except ImportError:
        logger.error("croniter is required for digest schedules; reinstall OpenJarvis")
        return False
    try:
        croniter(expression)
    except (ValueError, KeyError):
        return False
    return True


__all__ = [
    "DIGEST_AGENT",
    "DIGEST_PROMPT",
    "MANAGED_BY",
    "DigestSyncResult",
    "sync_digest_task",
]
