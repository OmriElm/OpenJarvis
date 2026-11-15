"""``jarvis digest`` — display and play the morning digest."""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.markdown import Markdown

from openjarvis.agents.digest_store import DigestStore
from openjarvis.core.config import DEFAULT_CONFIG_PATH, _toml_escape, load_config

logger = logging.getLogger(__name__)


def _play_audio(audio_path: str) -> None:
    """Play audio file in background using available system player."""
    players = ["ffplay -nodisp -autoexit", "aplay", "afplay", "paplay"]
    for player in players:
        cmd_parts = player.split() + [audio_path]
        try:
            subprocess.run(
                cmd_parts,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue


def _scheduler_db_path() -> Path:
    """Return the scheduler database next to config, or the configured path.

    ``scheduler.db_path`` is honored so ``jarvis digest --schedule`` and
    ``jarvis serve`` write the same file. A missing or unreadable config
    falls back to the directory that holds ``config.toml``.
    """
    custom = ""
    try:
        custom = str(load_config().scheduler.db_path or "").strip()
    except Exception as exc:
        logger.warning(
            "Could not read scheduler.db_path (%s); using the config directory",
            type(exc).__name__,
        )
    if custom:
        return Path(custom).expanduser()
    return DEFAULT_CONFIG_PATH.parent / "scheduler.db"


def _read_config_text(config_path: Path) -> str:
    """Read config.toml as UTF-8.

    ``Path.read_text()`` without an encoding uses the locale codec, which
    is cp1252 on a default Windows install and cannot round-trip Hebrew
    or other non-ASCII config.

    Raises
    ------
    RuntimeError
        If the file exists but is not valid UTF-8.
    """
    try:
        return config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"{config_path} is not valid UTF-8. Save the file as UTF-8 and retry."
        ) from exc


def _save_digest_schedule(enabled: bool, cron: str) -> None:
    """Persist digest schedule to config.toml as UTF-8."""
    config_path = DEFAULT_CONFIG_PATH

    # Read existing TOML content (or start fresh)
    content = ""
    if config_path.exists():
        content = _read_config_text(config_path)

    # Check if [digest] section already exists
    lines = content.split("\n")
    new_lines: list[str] = []
    in_digest = False
    digest_written = False

    for line in lines:
        stripped = line.strip()
        # Detect start of [digest] section
        if stripped == "[digest]":
            in_digest = True
            digest_written = True
            new_lines.append("[digest]")
            new_lines.append(f"enabled = {str(enabled).lower()}")
            new_lines.append(f'schedule = "{_toml_escape(cron)}"')
            continue
        # If inside [digest], skip old enabled/schedule keys
        if in_digest:
            if stripped.startswith("[") and stripped != "[digest]":
                in_digest = False
                new_lines.append(line)
            elif stripped.startswith("enabled") or stripped.startswith("schedule"):
                continue
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    if not digest_written:
        # Append [digest] section at the end
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("[digest]")
        new_lines.append(f"enabled = {str(enabled).lower()}")
        new_lines.append(f'schedule = "{_toml_escape(cron)}"')
        new_lines.append("")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(new_lines), encoding="utf-8", newline="\n")


def _create_scheduler_task(cron: str, timezone_name: str = "") -> Optional[str]:
    """Create or update the digest task. Returns the task id, or None."""
    try:
        from openjarvis.core.config import DigestConfig
        from openjarvis.scheduler.digest_sync import sync_digest_task
        from openjarvis.scheduler.scheduler import TaskScheduler
        from openjarvis.scheduler.store import SchedulerStore

        timezone_name = timezone_name or load_config().digest.timezone
        store = SchedulerStore(_scheduler_db_path())
        try:
            scheduler = TaskScheduler(store)
            # respect_pause is false: an explicit ``--schedule`` must leave
            # an active task even if a previous one was paused.
            result = sync_digest_task(
                scheduler,
                DigestConfig(
                    enabled=True,
                    schedule=cron,
                    timezone=timezone_name,
                ),
                respect_pause=False,
            )
            return result.task_id
        finally:
            store.close()
    except Exception as exc:
        logger.warning(
            "Could not create digest scheduler task (%s)",
            type(exc).__name__,
        )
        return None


def _cancel_scheduler_tasks() -> int:
    """Cancel managed digest tasks. Returns the number cancelled."""
    try:
        from openjarvis.core.config import DigestConfig
        from openjarvis.scheduler.digest_sync import sync_digest_task
        from openjarvis.scheduler.scheduler import TaskScheduler
        from openjarvis.scheduler.store import SchedulerStore

        store = SchedulerStore(_scheduler_db_path())
        try:
            scheduler = TaskScheduler(store)
            result = sync_digest_task(
                scheduler,
                DigestConfig(enabled=False),
                respect_pause=False,
            )
            return result.cancelled
        finally:
            store.close()
    except Exception as exc:
        logger.warning(
            "Could not cancel digest scheduler tasks (%s)",
            type(exc).__name__,
        )
        return 0


def _deliver_digest_text(text: str, console: Console) -> None:
    """Send *text* when ``[digest] deliver_to`` is set. Never raises."""
    try:
        from openjarvis.agents.digest_delivery import deliver_digest_text

        report = deliver_digest_text(text, load_config())
    except Exception as exc:
        console.print(
            f"[yellow]Digest delivery failed ({type(exc).__name__}).[/yellow]"
        )
        return
    if report.failed:
        console.print(
            "[yellow]Digest generated, but "
            f"{report.failed} channel delivery(ies) failed.[/yellow]"
        )
    elif report.sent:
        console.print(
            f"[green]Delivered the digest to {report.sent} channel(s).[/green]"
        )


@click.command("digest", help="Display and play the morning digest.")
@click.option("--text-only", is_flag=True, help="Print text without audio playback.")
@click.option("--fresh", is_flag=True, help="Re-generate the digest (skip cache).")
@click.option("--history", is_flag=True, help="Show past digests.")
@click.option("--section", type=str, default="", help="Show only a specific section.")
@click.option("--db-path", type=str, default="", help="Path to digest database.")
@click.option(
    "--schedule",
    type=str,
    default=None,
    is_eager=True,
    help=(
        'Set cron schedule (e.g. "0 6 * * *"), '
        '"off" to disable, or empty to show status.'
    ),
)
def digest(
    text_only: bool,
    fresh: bool,
    history: bool,
    section: str,
    db_path: str,
    schedule: Optional[str],
) -> None:
    """Display and optionally play the morning digest."""
    console = Console()

    # Handle --schedule flag
    if schedule is not None:
        _handle_schedule(console, schedule)
        return

    store = DigestStore(db_path=db_path) if db_path else DigestStore()

    if history:
        past = store.history(limit=10)
        if not past:
            console.print("[dim]No past digests found.[/dim]")
            store.close()
            return
        for artifact in past:
            console.print(
                f"[bold]{artifact.generated_at.strftime('%Y-%m-%d %H:%M')}[/bold]"
                f" — {artifact.model_used} / {artifact.voice_used}"
            )
            console.print(artifact.text[:200] + "...\n")
        store.close()
        return

    if fresh:
        # Trigger on-demand generation
        console.print("[yellow]Generating fresh digest...[/yellow]")
        try:
            from openjarvis.sdk import Jarvis

            with Jarvis() as j:
                j.ask("Generate my morning digest", agent="morning_digest")
        except Exception as exc:
            console.print(f"[red]Failed to generate digest: {exc}[/red]")
            store.close()
            return

        # Reload the freshly-generated digest from the store and play audio
        store.close()
        store = DigestStore(db_path=db_path) if db_path else DigestStore()
        artifact = store.get_latest()
        if artifact is None:
            console.print("[red]Digest was not saved.[/red]")
            store.close()
            return

        audio_path = str(artifact.audio_path)
        console.print(f"[dim]Audio path: '{audio_path}'[/dim]")
        # An empty Path is "."; only a real file is playable audio.
        has_audio = artifact.audio_path.is_file()
        console.print(f"[dim]Audio available: {has_audio}[/dim]")
        if not text_only and has_audio:
            audio_thread = threading.Thread(
                target=_play_audio, args=(audio_path,), daemon=True
            )
            audio_thread.start()
            console.print("[dim]Playing audio...[/dim]")
        elif not text_only:
            console.print("[yellow]Audio unavailable — TTS failed.[/yellow]")
            console.print("[yellow]Check OPENAI_API_KEY is set.[/yellow]")

        console.print(Markdown(artifact.text))
        _deliver_digest_text(artifact.text, console)
        store.close()
        return

    # Try to load today's cached digest
    artifact = store.get_today()
    if artifact is None:
        console.print("[dim]No digest for today. Use --fresh to generate one.[/dim]")
        store.close()
        return

    # Display text
    text = artifact.text
    if section:
        # Try to extract just the requested section
        lines = text.split("\n")
        in_section = False
        section_lines = []
        for line in lines:
            if line.strip().lower().startswith(
                f"## {section.lower()}"
            ) or line.strip().lower().startswith(f"# {section.lower()}"):
                in_section = True
                section_lines.append(line)
            elif in_section and line.strip().startswith("#"):
                break
            elif in_section:
                section_lines.append(line)
        text = "\n".join(section_lines) if section_lines else text

    # Play audio in background while text renders
    audio_path = str(artifact.audio_path)
    if not text_only and artifact.audio_path.is_file():
        audio_thread = threading.Thread(
            target=_play_audio, args=(audio_path,), daemon=True
        )
        audio_thread.start()

    console.print(Markdown(text))
    store.close()


def _handle_schedule(console: Console, schedule: str) -> None:
    """Handle the --schedule option logic."""
    cfg = load_config()
    digest_cfg = cfg.digest

    if schedule == "":
        # Show current schedule status
        status = "enabled" if digest_cfg.enabled else "disabled"
        console.print(f"[bold]Digest schedule:[/bold] {status}")
        console.print(f"  Cron: {digest_cfg.schedule}")
        console.print(f"  Timezone: {digest_cfg.timezone}")
        return

    if schedule.lower() == "off":
        # Disable the schedule
        try:
            _save_digest_schedule(enabled=False, cron=digest_cfg.schedule)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        cancelled = _cancel_scheduler_tasks()
        console.print("[yellow]Digest schedule disabled.[/yellow]")
        if cancelled:
            console.print(f"  Cancelled {cancelled} scheduler task(s).")
        return

    # Set a new cron schedule
    try:
        _save_digest_schedule(enabled=True, cron=schedule)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    task_id = _create_scheduler_task(schedule, digest_cfg.timezone)
    console.print(f"[green]Digest schedule set:[/green] {schedule}")
    if task_id:
        console.print(f"  Scheduler task created: {task_id}")
    else:
        console.print(
            "  [dim]Note: scheduler task not created "
            "(start the scheduler separately).[/dim]"
        )
