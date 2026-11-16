"""``jarvis scheduler`` -- task scheduling commands."""

from __future__ import annotations

import logging
import signal
import sys
from typing import Any, Optional

import click
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)


def _get_store() -> "SchedulerStore":  # noqa: F821
    """Build a SchedulerStore from user config."""
    from openjarvis.core.config import DEFAULT_CONFIG_DIR, load_config
    from openjarvis.scheduler.store import SchedulerStore

    config = load_config()
    db_path = getattr(getattr(config, "scheduler", None), "db_path", None) or str(
        DEFAULT_CONFIG_DIR / "scheduler.db"
    )
    return SchedulerStore(db_path)


def _get_scheduler(store: "SchedulerStore") -> "TaskScheduler":  # noqa: F821
    """Build a TaskScheduler from a store."""
    from openjarvis.scheduler.scheduler import TaskScheduler

    return TaskScheduler(store)


@click.group()
def scheduler() -> None:
    """Manage scheduled tasks."""


@scheduler.command("create")
@click.argument("prompt")
@click.option(
    "--type",
    "schedule_type",
    required=True,
    type=click.Choice(["cron", "interval", "once"]),
    help="Schedule type.",
)
@click.option(
    "--value",
    "schedule_value",
    required=True,
    help="Schedule value (cron expr, seconds, or ISO datetime).",
)
@click.option("--agent", default="simple", help="Agent to use for execution.")
@click.option("--tools", default="", help="Comma-separated tool names.")
def scheduler_create(
    prompt: str,
    schedule_type: str,
    schedule_value: str,
    agent: str,
    tools: str,
) -> None:
    """Create a new scheduled task."""
    console = Console()
    store = _get_store()
    try:
        sched = _get_scheduler(store)
        task = sched.create_task(
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            agent=agent,
            tools=tools,
        )
        console.print(f"[green]Created task {task.id}[/green]")
        console.print(f"  Type: {task.schedule_type}")
        console.print(f"  Value: {task.schedule_value}")
        console.print(f"  Next run: {task.next_run or 'N/A'}")
        console.print(f"  Agent: {task.agent}")
        if task.tools:
            console.print(f"  Tools: {task.tools}")
    finally:
        store.close()


@scheduler.command("list")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["active", "paused", "completed", "cancelled"]),
    help="Filter by status.",
)
def scheduler_list(status: Optional[str]) -> None:
    """List scheduled tasks."""
    console = Console()
    store = _get_store()
    try:
        sched = _get_scheduler(store)
        tasks = sched.list_tasks(status=status)

        if not tasks:
            console.print("[dim]No scheduled tasks found.[/dim]")
            return

        table = Table(title="Scheduled Tasks")
        table.add_column("ID", style="cyan")
        table.add_column("Prompt", max_width=40)
        table.add_column("Type", style="blue")
        table.add_column("Status", style="green")
        table.add_column("Next Run")
        table.add_column("Agent")

        for t in tasks:
            prompt_short = t.prompt[:37] + "..." if len(t.prompt) > 40 else t.prompt
            status_style = {
                "active": "green",
                "paused": "yellow",
                "completed": "dim",
                "cancelled": "red",
            }.get(t.status, "white")
            table.add_row(
                t.id,
                prompt_short,
                t.schedule_type,
                f"[{status_style}]{t.status}[/{status_style}]",
                t.next_run or "N/A",
                t.agent,
            )
        console.print(table)
    finally:
        store.close()


@scheduler.command("pause")
@click.argument("task_id")
def scheduler_pause(task_id: str) -> None:
    """Pause a scheduled task."""
    console = Console()
    store = _get_store()
    try:
        sched = _get_scheduler(store)
        sched.pause_task(task_id)
        console.print(f"[yellow]Task {task_id} paused[/yellow]")
    except KeyError:
        console.print(f"[red]Task not found: {task_id}[/red]")
    finally:
        store.close()


@scheduler.command("resume")
@click.argument("task_id")
def scheduler_resume(task_id: str) -> None:
    """Resume a paused task."""
    console = Console()
    store = _get_store()
    try:
        sched = _get_scheduler(store)
        sched.resume_task(task_id)
        console.print(f"[green]Task {task_id} resumed[/green]")
    except KeyError:
        console.print(f"[red]Task not found: {task_id}[/red]")
    finally:
        store.close()


@scheduler.command("cancel")
@click.argument("task_id")
def scheduler_cancel(task_id: str) -> None:
    """Cancel a scheduled task."""
    console = Console()
    store = _get_store()
    try:
        sched = _get_scheduler(store)
        sched.cancel_task(task_id)
        console.print(f"[red]Task {task_id} cancelled[/red]")
    except KeyError:
        console.print(f"[red]Task not found: {task_id}[/red]")
    finally:
        store.close()


@scheduler.command("logs")
@click.argument("task_id")
@click.option("-n", "--limit", default=10, type=int, help="Number of logs to show.")
def scheduler_logs(task_id: str, limit: int) -> None:
    """Show run logs for a scheduled task."""
    console = Console()
    store = _get_store()
    try:
        logs = store.get_run_logs(task_id, limit=limit)
        if not logs:
            console.print(f"[dim]No run logs for task {task_id}[/dim]")
            return

        table = Table(title=f"Run Logs for {task_id}")
        table.add_column("ID", justify="right")
        table.add_column("Started")
        table.add_column("Finished")
        table.add_column("Success")
        table.add_column("Result", max_width=40)
        table.add_column("Error", max_width=30)

        for log in logs:
            success_str = "[green]yes[/green]" if log["success"] else "[red]no[/red]"
            result_text = log["result"]
            result_short = (
                (result_text[:37] + "...") if len(result_text) > 40 else result_text
            )
            table.add_row(
                str(log["id"]),
                log["started_at"],
                log.get("finished_at") or "N/A",
                success_str,
                result_short,
                log.get("error", ""),
            )
        console.print(table)
    finally:
        store.close()


@scheduler.command("run-task")
@click.argument("agent_name")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would run without executing.",
)
def scheduler_run_task(agent_name: str, dry_run: bool) -> None:
    """Immediately execute the active task for AGENT_NAME.

    Finds the first active scheduled task whose agent matches AGENT_NAME
    and runs it right now — useful for testing and for launchd invocation
    when OpenJarvis is not running as a persistent daemon.

    Example (launchd plist ProgramArguments):
        jarvis scheduler run-task proactive
    """
    console = Console()
    store = _get_store()
    try:
        sched = _get_scheduler(store)
        tasks = sched.list_tasks(status="active")
        match = next((t for t in tasks if t.agent == agent_name), None)

        if match is None:
            console.print(
                f"[yellow]No active task found for agent '{agent_name}'. "
                "Register it first with 'jarvis scheduler create'.[/yellow]"
            )
            return

        if dry_run:
            console.print("[dim]Dry run — would execute:[/dim]")
            console.print(f"  Task : {match.id}")
            console.print(f"  Agent: {match.agent}")
            console.print(f"  Prompt: {match.prompt[:80]}")
            return

        console.print(f"Running task [cyan]{match.id}[/cyan] (agent: {match.agent})…")

        from openjarvis.core.config import load_config
        from openjarvis.system import SystemBuilder

        system = SystemBuilder(load_config()).build()
        result = system.ask(match.prompt, agent=match.agent)

        # Log the run result in the scheduler store
        from datetime import datetime, timezone

        if isinstance(result, (dict, list)):
            import json as _json

            result_str = _json.dumps(result, default=str)
        else:
            result_str = str(result) if result is not None else ""

        now = datetime.now(timezone.utc).isoformat()
        store.log_run(
            task_id=match.id,
            started_at=now,
            finished_at=now,
            success=True,
            result=result_str,
            error="",
        )

        console.print("[green]Done.[/green]")
        if result:
            console.print(result)
        if match.agent == "morning_digest":
            from openjarvis.agents.digest_delivery import deliver_digest_text
            from openjarvis.scheduler.scheduler import coerce_ask_result

            try:
                report = deliver_digest_text(coerce_ask_result(result), load_config())
            except Exception as exc:
                console.print(
                    f"[yellow]Digest delivery failed ({type(exc).__name__}).[/yellow]"
                )
            else:
                if report.failed:
                    console.print(
                        "[yellow]Digest ran, but "
                        f"{report.failed} channel delivery(ies) failed.[/yellow]"
                    )
                elif report.sent:
                    console.print(
                        f"[green]Delivered the digest to {report.sent} "
                        "channel(s).[/green]"
                    )
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise SystemExit(1)
    finally:
        store.close()


def _build_scheduler_system(config: Any) -> Any:
    """Build a Jarvis system that can execute scheduled tasks.

    Raises
    ------
    click.ClickException
        When the engine or another startup dependency is unavailable.
        The command stops instead of polling in dry-run mode.
    """
    from openjarvis.system import SystemBuilder

    try:
        return SystemBuilder(config).build()
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(
            "Scheduler did not start because the Jarvis system failed to "
            f"initialize ({type(exc).__name__}: {exc}). "
            "No tasks were executed. Fix the engine configuration, or run "
            "`jarvis serve` once an engine is available."
        ) from exc


def _block_until_signal(sched: Any, console: Console) -> None:
    """Block until SIGINT or SIGTERM. Windows has no ``signal.pause``."""

    def _handle_signal(signum: int, frame: object) -> None:
        sched.stop()
        console.print("\n[yellow]Scheduler stopped.[/yellow]")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        signal.pause()
    except AttributeError:
        import time

        while True:
            time.sleep(1)


@scheduler.command("start")
@click.option(
    "--poll-interval",
    default=60,
    type=int,
    help="Seconds between poll cycles.",
)
def scheduler_start(poll_interval: int) -> None:
    """Start the scheduler and execute due tasks until interrupted.

    Tasks are sent to the configured agent. A missed run fires once on
    startup when it is still inside ``scheduler.misfire_grace_seconds``
    (default two hours); older misses are skipped. The slot is claimed
    before it runs, so restarting this process does not run it twice.

    This is not a dry run. Use ``jarvis scheduler run-task --dry-run`` to
    preview one task without executing it.
    """
    console = Console()
    from openjarvis.agents.digest_delivery import deliver_scheduled_result
    from openjarvis.core.config import load_config
    from openjarvis.scheduler.digest_sync import sync_digest_task
    from openjarvis.scheduler.scheduler import TaskScheduler
    from openjarvis.scheduler.service import coerce_grace_seconds, coerce_poll_interval

    config = load_config()
    interval = coerce_poll_interval(poll_interval)
    store = _get_store()
    system = None
    sched = None
    try:
        system = _build_scheduler_system(config)
        sched = TaskScheduler(
            store,
            system,
            poll_interval=interval,
            misfire_grace_seconds=coerce_grace_seconds(
                config.scheduler.misfire_grace_seconds
            ),
            on_success=lambda task, text, _cfg=config: deliver_scheduled_result(
                task, text, _cfg
            ),
        )
        try:
            sync_digest_task(sched, config.digest, respect_pause=True)
        except Exception as exc:
            logger.warning(
                "Could not sync the digest schedule (%s)",
                type(exc).__name__,
            )
        sched.start()
        console.print(
            f"[green]Scheduler running[/green] (poll every {interval}s). "
            "Due tasks will be executed. Press Ctrl+C to stop."
        )
        try:
            _block_until_signal(sched, console)
        except KeyboardInterrupt:
            console.print("\n[yellow]Scheduler stopped.[/yellow]")
    finally:
        if sched is not None:
            sched.stop()
        store.close()
        if system is not None and hasattr(system, "close"):
            try:
                system.close()
            except Exception as exc:
                logger.warning(
                    "Scheduler system shutdown failed (%s)",
                    type(exc).__name__,
                )
