# mend

AI-assisted system recovery, backup, and health monitoring.  
Kiro CLI is the intelligent layer — it drives operations, diagnoses failures, and explains results in plain English. mend is the execution engine underneath.

---

## Quick Start

```bash
sudo ln -s ~/mend/mend /usr/local/bin/mend
mend schedule        # install 3-day auto-backup timer (run once)
```

After that, the system backs up automatically. Everything else is on-demand.

---

## Commands

```bash
mend backup              # back up full system → ~/backup
mend backup-github       # back up + push configs/dotfiles/packages to private GitHub repo
mend recover             # restore system from last backup
mend schedule            # install systemd timer: auto-backup every 3 days
mend status              # last backup time, disk usage, schedule status

mend watch               # start continuous health monitoring (hourly)
mend watch <seconds>     # custom interval
mend check               # run one health check cycle now
mend snapshots           # list rollback snapshots
mend rollback <name>     # restore system to a named snapshot
mend lkg                 # restore to last known good verified state
```

---

## Interactive UI

```bash
python3 main.py
```

Split-pane curses interface — no typing required.

```
 mend                          [status bar]
─────────────────────────────────────────────
 Tasks  [t:all]      │  Log (live)
══════════════════   │  ══════════════════════
 Full Recovery       │   ✓ update_mirrors
 Backup              │   ▶ backup_system
 Recover             │   ↺ install_base: retry
 ...                 │
─────────────────────────────────────────────
 ↑↓ nav  Enter run  Tab→log  t filter  w check  s snaps  p pause  r reset  q quit
```

| Key | Action |
|---|---|
| `↑↓` / `j k` | Navigate |
| `Enter` | Run task |
| `Tab` | Switch focus to log pane |
| `t` | Cycle tag filter |
| `w` | Run one watchdog health check |
| `s` | Snapshot picker → rollback |
| `p` | Pause / resume |
| `r` | Reset state |
| `q` / `Esc` | Quit |

Other CLI flags:

```bash
python3 main.py --dry-run          # show what would run, no changes
python3 main.py --task <name>      # run task non-interactively
python3 main.py --history          # print run history
python3 main.py --shell            # escape hatch to raw shell
python3 main.py --watch-once       # one health check cycle
python3 main.py --lkg              # rollback to last known good
python3 main.py --snapshots        # list snapshots
python3 main.py --rollback <name>  # rollback to snapshot
python3 main.py --external <path>  # add external sync target (repeatable)
```

---

## Architecture

```
main.py       — entry point, task definitions, CLI flags, watchdog setup
registry.py   — operation definitions and factory functions
executor.py   — convergence loop, failure handling, deep validation
state.py      — state manager: persist, verify, last-known-good, rollback
inspector.py  — deep system snapshot (OS, CPU, memory, disk, GPU, network, …)
watchdog.py   — periodic health checks, drift detection, AI correction, snapshots
ui.py         — split-pane curses UI, failure recovery menu, snapshot picker
mend          — shell entry point (Kiro CLI wrapper)
```

---

## How It Works

**Intent-driven execution.** Every action is a registered `Operation` with declared preconditions, an idempotent action, and postconditions. Nothing runs outside this boundary.

**Deep context awareness.** Before any operation runs, the system takes a full environment snapshot: OS, kernel, CPU, memory, disk, partitions, GPU, temperatures, battery, display, network, DNS, packages, services, processes. Pre/postconditions evaluate against real-time truth.

**Three failure classes.**

| Class | Behaviour |
|---|---|
| `system` | Halt. Manual fix required. Cannot be skipped. |
| `external` | AI fix attempted automatically, then manual/skip/abort offered. |
| `recoverable` | AI fix offered first, then manual, skip, or abort. |

**Failure recovery menu** (curses, arrow-key):  
On any failure, a full-screen menu appears with scrollable error output and recovery options. No blind skipping — every skip is an explicit choice.

**Self-healing convergence loop.** Each operation: check preconditions → run action → check postconditions → retry with exponential backoff (1s → 2s → 4s → 8s → 16s).

**AI deep validation.** After postconditions pass, an optional `deep_validate` function calls Kiro CLI to inspect internal correctness (config syntax, port listening, service behavior) and return a verdict: `done | retry | reapply | reinstall`.

**Multi-pass app testing.** `make_app_test_op` launches an app, monitors it briefly, detects crashes/misbehavior, and tracks a per-app error counter. At 3 failures, Kiro diagnoses and decides retry or reinstall.

**State verification before acceptance.** `finish_task()` runs registered verifiers before marking a task done. If any verifier fails, the task is rejected and the system rolls back automatically.

**Last known good.** Every verified task completion takes a full rsync snapshot tagged `verified-<timestamp>` and records it as `last_known_good`. `mend lkg` restores it.

**External recovery copies.** Verified snapshots are synced to registered external targets (USB, NAS, remote rsync). Rollback tries local first, then external copies in order.

**Parallel execution.** Operations marked `parallel=True` run concurrently via threads. A fatal failure in any worker signals all others to abort.

**Periodic health monitoring.** The watchdog runs registered `HealthCheck` functions on a schedule, diffs snapshots for drift (package changes, mount changes, disk pressure, low memory), and calls Kiro to correct failures — taking a rollback snapshot before any correction.

**Run history.** Last 50 runs archived with task name, status, timestamps, op counts. Survives state resets.

**Dry-run mode.** `--dry-run` evaluates preconditions and reports what would run without executing anything.

**Escape hatch.** `--shell` or the Shell menu item drops to a raw shell. Explicit and intentional.

---

## Defining Operations

Edit `build_registry()` in `main.py`:

```python
from registry import make_shell_op, make_service_op, make_app_test_op, make_ai_validator

# shell command (system-level: halts on failure, no skip)
reg.register(make_shell_op(
    name="verify_fstab",
    description="Verify fstab entries",
    cmd=["findmnt", "--verify"],
    fatal=True,
    tags=["disk"],
))

# systemd service with AI deep validation
reg.register(make_service_op(
    name="start_nginx",
    service="nginx",
    ensure="active",
    fatal=True,
    deep_validate=make_ai_validator(
        "nginx is active, config passes 'nginx -t', and port 80 is listening"
    ),
))

# multi-pass external app test (3-error threshold → AI diagnoses → reinstall)
reg.register(make_app_test_op(
    name="test_postgres",
    cmd=["pg_isready"],
    run_for=3.0,
    expect_exit_zero=True,
    install_cmd=["apt-get", "install", "-y", "postgresql"],
    uninstall_cmd=["apt-get", "remove", "-y", "--purge", "postgresql"],
    tags=["db"],
))
```

Add ops to a task:

```python
TASKS = {
    "Full Recovery": {
        "ops":  ["verify_fstab", "start_nginx", "test_postgres"],
        "deps": [],
    },
}
```

Add custom health checks to the watchdog:

```python
wd.register(HealthCheck(
    name="nginx_running",
    description="nginx is active",
    check=lambda env: (
        env.get("services", {}).get("nginx") == "active", "nginx not active"
    ),
    severity="critical",
    ai_correct=True,
))
```

Register state verifiers (run before any task is accepted as done):

```python
state.add_verifier(lambda env: (os.path.isdir("/mnt/data"), "/mnt/data not mounted"))
```

Register external snapshot targets:

```python
state.external_targets.append("/mnt/usb/mend")
state.external_targets.append("user@nas:/backups/mend")
```

---

## Operation Contract

| Field | Type | Description |
|---|---|---|
| `preconditions` | `list[Check]` | Must pass before action runs |
| `action` | `callable(env) -> str` | Idempotent; returns captured output |
| `postconditions` | `list[Check]` | Must pass after action runs |
| `deep_validate` | `callable(env, output) -> (ok, reason, verdict)` | AI-driven correctness check |
| `failure_class` | `system` / `external` / `recoverable` | Controls failure handling behaviour |
| `max_retries` | `int` | Retry attempts before giving up |
| `timeout` | `int \| None` | Seconds before action is killed |
| `tags` | `list[str]` | Used for menu filtering |
| `parallel` | `bool` | Safe to run concurrently |

---

## GitHub Backup

On first `mend backup-github`, a private repo `mend-system-backup` is created automatically via `gh` CLI (installed if missing). Each run commits and pushes:

- `/etc`
- `~/.config`, `~/.bashrc`, `~/.zshrc`, `~/.profile`, `~/.ssh/config`
- `packages.txt` (full installed package list)

Full system binaries are never pushed to Git — only configs and dotfiles.

---

## Dependencies

None. Stdlib only (`curses`, `subprocess`, `signal`, `threading`, `hashlib`, `socket`, `json`, `rsync` for backups).

Kiro CLI (`kiro-cli`) is used for AI-assisted fixing, deep validation, and the `mend` shell wrapper. It is optional — all operations run without it, AI features degrade gracefully.
