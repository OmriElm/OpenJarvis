# Task Scheduler

The task scheduler lets you run agent queries automatically on a schedule -- once at a future time, on a recurring interval, or via a cron expression. Scheduled tasks are persisted in SQLite so they survive process restarts, and execution is handled by a background daemon thread that polls for due tasks every 60 seconds.

!!! note "Where it runs"
    The scheduler is a standalone module (`openjarvis.scheduler`). `jarvis serve` polls it when `[scheduler] enabled` or `[digest] enabled` is true, which is what a background service such as the Windows service already runs. `jarvis scheduler start` runs the same loop in the foreground and executes due tasks. Passing `system=None` to `TaskScheduler` is still a dry run for library callers and tests.

---

## Schedule Types

| `schedule_type` | `schedule_value` format            | Example                     | Meaning                             |
|-----------------|------------------------------------|-----------------------------|-------------------------------------|
| `once`          | ISO 8601 UTC datetime              | `"2026-03-01T09:00:00Z"`    | Run once at that timestamp          |
| `interval`      | Seconds as a string                | `"3600"`                    | Run every hour, starting immediately |
| `cron`          | Standard 5-field cron expression   | `"0 9 * * 1-5"`             | 09:00 in the task timezone, else UTC |

!!! tip "Cron support"
    Cron expressions require `croniter`, which is installed with OpenJarvis. A missing `croniter` is reported as an error. It is not replaced with an hourly approximation. Put an IANA timezone in the task metadata (`{"timezone": "Asia/Jerusalem"}`) to evaluate the expression in local time; the stored `next_run` is UTC. An unknown timezone is logged once and treated as UTC.

---

## CLI Commands

The `jarvis scheduler` subcommand group manages tasks and the daemon from the terminal.

### Start the daemon

```bash
jarvis scheduler start
```

Runs due tasks in the foreground until interrupted (++ctrl+c++). The command builds a Jarvis system and executes each task with its agent. It is not a dry run. If the system cannot be built (for example no inference engine is reachable) the command exits and reports that no tasks were executed.

A task that became due while the process was stopped runs once on startup when it is still inside `misfire_grace_seconds` (default two hours, `7200`). Older misses are skipped and the next future slot is stored, so a daily job does not fire in the evening after a day offline. The slot is claimed in SQLite before it runs, so restarting the process does not run that slot again. Set `misfire_grace_seconds = 0` to always run one missed execution, however late it is.

`jarvis serve` does this on its own when the scheduler or the digest is enabled, which is the usual way to leave schedules running. Use `jarvis scheduler start` when you want the loop in a terminal instead. Do not run both against the same database: the claim prevents a double execution, but two processes will still contend.

In production, prefer `jarvis serve` under systemd, launchd, or the Windows service (see [Deployment](../deployment/systemd.md)).

### Create a task

```bash
# Run once at a specific time
jarvis scheduler create \
  --prompt "Generate the weekly summary report" \
  --type once \
  --value "2026-03-01T09:00:00Z"

# Run every hour
jarvis scheduler create \
  --prompt "Check for new emails and summarize" \
  --type interval \
  --value "3600" \
  --agent orchestrator \
  --tools retrieval,think

# Run on a cron schedule
jarvis scheduler create \
  --prompt "Summarize overnight logs" \
  --type cron \
  --value "0 8 * * 1-5"
```

### List tasks

```bash
# All tasks
jarvis scheduler list

# Active tasks only
jarvis scheduler list --status active

# Paused tasks
jarvis scheduler list --status paused
```

Example output:

```
ID               AGENT     TYPE       VALUE          STATUS   NEXT RUN
a3f9b12c4d8e    simple    cron       0 8 * * 1-5    active   2026-02-26T08:00:00+00:00
b7c2e56f1a3d    orchestr  interval   3600           active   2026-02-25T14:05:00+00:00
```

### Pause and resume tasks

```bash
# Pause a running task
jarvis scheduler pause a3f9b12c4d8e

# Resume -- next_run is recomputed from the current time
jarvis scheduler resume a3f9b12c4d8e
```

### Cancel a task

```bash
# Permanently cancel (status -> "cancelled", next_run cleared)
jarvis scheduler cancel a3f9b12c4d8e
```

### View run logs

```bash
# Last 10 executions for a task
jarvis scheduler logs a3f9b12c4d8e
```

Example output:

```
Run 1: started=2026-02-25T08:00:01Z finished=2026-02-25T08:00:04Z success=True
  Result: Overnight logs contain 3 warnings and no errors.
Run 2: started=2026-02-24T08:00:00Z finished=2026-02-24T08:00:05Z success=True
  Result: Logs are clean.
```

---

## Python API

```python title="scheduler_example.py"
from openjarvis.scheduler.store import SchedulerStore
from openjarvis.scheduler.scheduler import TaskScheduler

# Set up storage
store = SchedulerStore(db_path="~/.openjarvis/scheduler.db")  # (1)!

# Wire in a JarvisSystem for task execution
from openjarvis import Jarvis
jarvis = Jarvis()

scheduler = TaskScheduler(
    store=store,
    system=jarvis,         # (2)!
    poll_interval=60,      # (3)!
)

# Create tasks
daily_summary = scheduler.create_task(
    prompt="Summarize the latest news headlines",
    schedule_type="cron",
    schedule_value="0 8 * * *",
    agent="simple",
)
print(f"Created task {daily_summary.id}, next run: {daily_summary.next_run}")

# List active tasks
for task in scheduler.list_tasks(status="active"):
    print(f"  {task.id}: {task.prompt} @ {task.next_run}")

# Manage task state
scheduler.pause_task(daily_summary.id)
scheduler.resume_task(daily_summary.id)   # next_run recomputed from now
scheduler.cancel_task(daily_summary.id)  # permanent

# Start the background thread
scheduler.start()   # (4)!

# ... application runs ...

scheduler.stop()
jarvis.close()
```

1. SQLite database storing all task state and run logs.
2. The scheduler calls `system.ask(task.prompt, agent=task.agent, tools=...)` when a task is due. Pass `system=None` for a dry-run mode that logs what it would execute without calling the agent.
3. Seconds between polling cycles. Lower values increase responsiveness at the cost of more SQLite reads.
4. Starts a daemon thread named `"jarvis-scheduler"`. Daemon threads exit automatically when the main process exits.

---

## ScheduledTask Fields

Every task is represented as a `ScheduledTask` dataclass.

| Field            | Type              | Default       | Description                                       |
|------------------|-------------------|---------------|---------------------------------------------------|
| `id`             | `str`             | auto (16 hex) | Unique task identifier                            |
| `prompt`         | `str`             | --            | Query sent to the agent on execution              |
| `schedule_type`  | `str`             | --            | `"cron"`, `"interval"`, or `"once"`              |
| `schedule_value` | `str`             | --            | Cron expression, interval seconds, or ISO datetime|
| `context_mode`   | `str`             | `"isolated"`  | Execution context mode                            |
| `status`         | `str`             | `"active"`    | `"active"`, `"paused"`, `"completed"`, `"cancelled"` |
| `next_run`       | `str` or `None`   | computed      | ISO 8601 UTC datetime of the next execution       |
| `last_run`       | `str` or `None`   | `None`        | ISO 8601 UTC datetime of the last execution       |
| `agent`          | `str`             | `"simple"`    | Agent registry key to use for execution           |
| `tools`          | `str`             | `""`          | Comma-separated tool names for the agent          |
| `metadata`       | `dict`            | `{}`          | Arbitrary metadata for the task                   |

---

## Using Scheduler Tools with Agents

The five scheduler MCP tools (`schedule_task`, `list_scheduled_tasks`, `pause_scheduled_task`, `resume_scheduled_task`, `cancel_scheduled_task`) can be passed to any `ToolUsingAgent`, allowing an agent to schedule follow-up tasks autonomously.

```bash
# Let the orchestrator schedule its own follow-up
jarvis ask --agent orchestrator \
  --tools schedule_task,list_scheduled_tasks \
  "Research transformer architectures and schedule a daily summary at 8am"
```

```python
from openjarvis import Jarvis

j = Jarvis()
response = j.ask(
    "Schedule a weekly digest of research papers every Monday at 9am",
    agent="orchestrator",
    tools=["schedule_task"],
)
print(response)
```

See [Scheduler Tools](tools.md#scheduler-tools) for full parameter reference.

---

## Configuration

Scheduler settings live in the `[scheduler]` section of `~/.openjarvis/config.toml`.

```toml title="~/.openjarvis/config.toml"
[scheduler]
enabled = false
db_path = "~/.openjarvis/scheduler.db"
poll_interval = 60
# Run a missed task once if it is at most this many seconds late.
# 0 = always catch up one missed run.
misfire_grace_seconds = 7200
```

| Key                      | Type   | Default                      | Description                                      |
|--------------------------|--------|------------------------------|--------------------------------------------------|
| `enabled`                | `bool` | `false`                      | `jarvis serve` polls scheduled tasks             |
| `db_path`                | `str`  | `~/.openjarvis/scheduler.db` | SQLite database path                             |
| `poll_interval`          | `int`  | `60`                         | Seconds between polling cycles                   |
| `misfire_grace_seconds`  | `int`  | `7200`                       | Max lateness that still runs once. `0` = no cap |

---

## See Also

- [Scheduler Tools reference](tools.md#scheduler-tools) -- MCP tool parameter details
- [Architecture: Agentic Logic](../architecture/agents.md) -- how the scheduler integrates with agents
- [Deployment: systemd](../deployment/systemd.md) -- running the scheduler as a system service
