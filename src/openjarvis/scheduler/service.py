"""Start the persisted task scheduler inside a long-running process.

``jarvis serve`` is the process users leave running (including the Windows
service). It calls :func:`start_background_scheduler` when the digest or
the task scheduler is enabled. ``jarvis scheduler start`` runs the same
scheduler in the foreground and does not use this helper, because that
command should execute tasks even when neither flag is set.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openjarvis.agents.digest_delivery import deliver_scheduled_result
from openjarvis.core.config import JarvisConfig
from openjarvis.scheduler.digest_sync import sync_digest_task
from openjarvis.scheduler.scheduler import TaskScheduler
from openjarvis.scheduler.store import SchedulerStore

logger = logging.getLogger(__name__)

_DEFAULT_GRACE_SECONDS = 7200
_DEFAULT_POLL_SECONDS = 60


def scheduler_is_enabled(config: JarvisConfig) -> bool:
    """Return True when ``jarvis serve`` should poll scheduled tasks.

    The digest has its own switch. Turning the digest on is enough to run
    its schedule; ``[scheduler] enabled`` also runs every other task.
    """
    digest_on = bool(getattr(getattr(config, "digest", None), "enabled", False))
    scheduler_on = bool(getattr(getattr(config, "scheduler", None), "enabled", False))
    return digest_on or scheduler_on


def resolve_scheduler_db_path(config: JarvisConfig) -> Path:
    """Return the SQLite file shared by the scheduler CLI and the server.

    ``scheduler.db_path`` wins when it is set. Otherwise the database sits
    next to ``config.toml`` so ``jarvis digest --schedule`` and
    ``jarvis serve`` see the same tasks.
    """
    custom = str(getattr(getattr(config, "scheduler", None), "db_path", "") or "")
    custom = custom.strip()
    if custom:
        return Path(custom).expanduser()
    from openjarvis.core.config import DEFAULT_CONFIG_PATH

    return DEFAULT_CONFIG_PATH.parent / "scheduler.db"


def coerce_grace_seconds(value: Any) -> int:
    """Return a misfire grace in seconds, or the default when *value* is bad.

    ``0`` and negative values are preserved: the scheduler treats them as
    "always run one missed execution".
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.error(
            "Invalid scheduler.misfire_grace_seconds (%s); using %d",
            type(value).__name__,
            _DEFAULT_GRACE_SECONDS,
        )
        return _DEFAULT_GRACE_SECONDS


def coerce_poll_interval(value: Any) -> int:
    """Return a poll interval of at least one second."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        logger.error(
            "Invalid scheduler.poll_interval (%s); using %d",
            type(value).__name__,
            _DEFAULT_POLL_SECONDS,
        )
        return _DEFAULT_POLL_SECONDS
    if seconds < 1:
        logger.error(
            "scheduler.poll_interval must be >= 1; using %d", _DEFAULT_POLL_SECONDS
        )
        return _DEFAULT_POLL_SECONDS
    return seconds


def start_background_scheduler(
    config: JarvisConfig,
    system: Any,
    *,
    bus: Any = None,
    clock: Any = None,
) -> tuple[TaskScheduler, SchedulerStore] | None:
    """Start the task scheduler when digest or scheduler mode is enabled.

    The returned scheduler is already running. The caller stops it and
    closes the store on shutdown. Returns ``None`` when neither feature
    is enabled.

    The first poll runs immediately, so a task whose ``next_run`` is still
    inside the misfire grace window executes once on startup. The slot is
    claimed in SQLite before the task runs, so a second process or a
    restart does not execute that same slot again.

    Parameters
    ----------
    config:
        Loaded configuration.
    system:
        Object with ``ask`` used to execute due tasks. Pass a
        :class:`~openjarvis.system.core.JarvisSystem`.
    bus:
        Optional event bus. Scheduler events are published when it is set.
    clock:
        Optional UTC clock forwarded to the scheduler. Production callers
        leave this unset. Tests pass a fixed clock so a task is not due
        only because the suite ran at that minute.

    Returns
    -------
    tuple[TaskScheduler, SchedulerStore] or None
        The running scheduler and the store it owns.
    """
    if not scheduler_is_enabled(config):
        return None

    try:
        import openjarvis.agents  # noqa: F401  # register morning_digest and others
    except Exception as exc:
        logger.warning(
            "Agent registry import failed (%s); scheduled agents may not resolve",
            type(exc).__name__,
        )

    path = resolve_scheduler_db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = SchedulerStore(path)
    try:
        grace = coerce_grace_seconds(
            getattr(config.scheduler, "misfire_grace_seconds", _DEFAULT_GRACE_SECONDS)
        )
        poll = coerce_poll_interval(
            getattr(config.scheduler, "poll_interval", _DEFAULT_POLL_SECONDS)
        )
        scheduler = TaskScheduler(
            store,
            system,
            poll_interval=poll,
            bus=bus,
            clock=clock,
            misfire_grace_seconds=grace,
            on_success=lambda task, text, _cfg=config: deliver_scheduled_result(
                task, text, _cfg
            ),
        )
        try:
            sync_digest_task(scheduler, config.digest, respect_pause=True)
        except Exception as exc:
            logger.error(
                "Could not sync the digest schedule (%s); other tasks will still run",
                type(exc).__name__,
            )
        scheduler.start()
    except Exception:
        store.close()
        raise
    logger.info(
        "Task scheduler started (poll=%ss, misfire_grace=%ss)",
        poll,
        grace,
    )
    return scheduler, store


__all__ = [
    "coerce_grace_seconds",
    "coerce_poll_interval",
    "resolve_scheduler_db_path",
    "scheduler_is_enabled",
    "start_background_scheduler",
]
