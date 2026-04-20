# mend

A state-aware, intent-driven system controller for recovery and environment management.

---

## Usage

```bash
python3 main.py                  # interactive arrow-key UI
python3 main.py --dry-run        # show what would run, no changes made
python3 main.py --task <name>    # run a specific task non-interactively
python3 main.py --history        # print run history and exit
python3 main.py --shell          # escape hatch to raw shell
```

## Controls

| Key | Action |
|---|---|
| `↑` / `↓` or `j` / `k` | Navigate |
| `Enter` | Select |
| `t` | Cycle tag filter |
| `p` | Pause / resume |
| `r` | Reset state |
| `q` / `Esc` | Quit |

---

## Structure

```
registry.py   — operation definitions and factory functions
state.py      — interrupt-aware state manager, persists to state.json
inspector.py  — environment snapshot (mounts, packages, services, processes, network)
executor.py   — convergence loop with parallel execution and exponential backoff
ui.py         — live execution panel, arrow-key menu, scrollable log (curses, stdlib only)
main.py       — entry point, task definitions, dependency resolution, CLI flags
```

---

## Concepts

**Intent-driven interface.** You select tasks and operations by name, not by typing shell commands. Every action is a registered, named operation with declared preconditions, an idempotent action, and postconditions. Nothing runs outside this boundary.

**Context awareness.** Before any operation runs, the system snapshots the environment: mounted partitions, block devices, installed packages, running services, active processes, network interfaces, and internet connectivity. Pre- and postconditions evaluate against this snapshot, so decisions are grounded in what actually exists.

**State-aware orchestration.** Task progress is persisted to `state.json` after every operation. If the process is interrupted (SIGINT, SIGTERM, or force-kill), the state is preserved. On next launch, you can resume exactly where it stopped or discard and start fresh.

**Self-healing convergence loop.** Each operation follows: check preconditions → run action → check postconditions → retry with exponential backoff. If postconditions fail, the operation retries up to `max_retries` times (backoff: 1s → 2s → 4s → 8s → 16s). Fatal operations halt the entire task on failure. Recoverable operations skip after exhausting retries and allow the task to continue.

**Parallel execution.** Operations marked `parallel=True` are batched and run concurrently via threads. Consecutive parallel ops in a task run together; sequential ops run one at a time. A fatal failure in any parallel worker signals all others to abort via a shared event.

**Dry-run mode.** `--dry-run` evaluates preconditions and reports what would run without executing any actions. Useful for validating a task plan before committing.

**Run history.** Every completed or failed task is archived (last 50 runs) and survives state resets. View from the menu or with `--history`. Each entry records task name, status, start/finish time, and op counts.

**Per-op output and timing.** Captured stdout/stderr (up to 4k) and wall-clock duration are stored per operation in state. Available for inspection in the log viewer.

**Config integrity.** `inspector.check_file()` returns a SHA256 hash alongside existence, size, and permissions. `inspector.hash_configs([...])` hashes a list of config files for drift detection.

**Escape hatch.** `--shell` or the Shell menu item drops to a raw shell. This boundary is explicit and intentional, not a default path.

---

## Defining Operations

Edit `build_registry()` in `main.py`:

```python
# shell command
reg.register(make_shell_op(
    name="verify_fstab",
    description="Verify fstab entries",
    cmd=["findmnt", "--verify"],
    fatal=True,
    tags=["disk"],
    timeout=10,
))

# mount a partition
reg.register(make_mount_op("mount_data", "/dev/sdb1", "/mnt/data", fatal=True))

# install packages
reg.register(make_pkg_op("install_tools", ["rsync", "htop"], manager="apt"))

# copy files
reg.register(make_copy_op("restore_configs", "/mnt/data/configs", "/etc/myapp"))

# pure health check (no action, postcondition only)
reg.register(make_health_op(
    name="check_internet",
    description="Verify internet connectivity",
    check_fn=lambda env: (env.get("internet", False), "no internet"),
    tags=["network"],
))

# systemd service
reg.register(make_service_op("start_nginx", "nginx", ensure="active", fatal=True))
```

Add op names to a task in `TASKS`:

```python
TASKS = {
    "Full Recovery": {
        "ops":  ["mount_data", "restore_configs", "install_tools", "verify_fstab"],
        "deps": [],          # task names whose ops run first
    },
}
```

## Operation Contract

| Field | Type | Description |
|---|---|---|
| `preconditions` | `list[Check]` | Must pass before action runs |
| `action` | `callable(env) -> str` | Idempotent; returns captured output |
| `postconditions` | `list[Check]` | Must pass after action runs |
| `failure_class` | `fatal` / `recoverable` | Fatal halts task; recoverable retries then skips |
| `max_retries` | `int` | Retry attempts before giving up |
| `timeout` | `int \| None` | Seconds before action is killed |
| `tags` | `list[str]` | Used for menu filtering (`t` key) |
| `parallel` | `bool` | Safe to run concurrently with other parallel ops |

## Dependencies

None. Stdlib only (`curses`, `subprocess`, `signal`, `threading`, `hashlib`, `socket`, `json`).
