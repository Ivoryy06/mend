#!/usr/bin/env python3
"""
mend — state-aware system controller

Usage:
  python main.py                    interactive arrow-key UI
  python main.py --dry-run          show what would run, no changes
  python main.py --task <name>      run a specific task non-interactively
  python main.py --shell            escape hatch to raw shell
  python main.py --history          print run history and exit
"""

import sys, os, argparse
from registry import Registry, make_mount_op, make_pkg_op, make_copy_op, make_shell_op, make_health_op
from state import StateManager
from executor import Executor
from inspector import snapshot
from ui import Menu, Logger, LivePanel


# ── operation registry ────────────────────────────────────────────────────────

def build_registry() -> Registry:
    reg = Registry()

    reg.register(make_shell_op(
        name="update_mirrors",
        description="Refresh package mirrors",
        cmd=["true"],
        tags=["network", "pkg"],
        parallel=False,
    ))
    reg.register(make_pkg_op(
        name="install_base",
        packages=["base-files"],
        manager="apt",
    ))
    reg.register(make_shell_op(
        name="verify_fstab",
        description="Verify /etc/fstab entries",
        cmd=["findmnt", "--verify"],
        fatal=True,
        tags=["disk"],
    ))
    reg.register(make_health_op(
        name="check_internet",
        description="Verify internet connectivity",
        check_fn=lambda env: (env.get("internet", False), "no internet"),
        tags=["network"],
    ))

    return reg


# ── task definitions with optional dependencies ───────────────────────────────
# deps: ops that must complete before this task's ops run (resolved at runtime)

TASKS: dict[str, dict] = {
    "Full Recovery": {
        "ops":  ["update_mirrors", "install_base", "verify_fstab"],
        "deps": [],
    },
    "Verify Only": {
        "ops":  ["verify_fstab", "check_internet"],
        "deps": [],
    },
    "Install Packages": {
        "ops":  ["update_mirrors", "install_base"],
        "deps": [],
    },
}

# Collect all tags across all ops for the filter menu
def _all_tags(reg: Registry) -> list:
    tags = set()
    for op in reg.all():
        tags.update(op.tags)
    return sorted(tags)


# ── dependency resolution ─────────────────────────────────────────────────────

def _resolve_ops(task_name: str, reg: Registry) -> list:
    """Return ordered op list for a task, prepending dep-task ops if needed."""
    task = TASKS[task_name]
    op_names = []
    for dep_task in task.get("deps", []):
        for name in TASKS[dep_task]["ops"]:
            if name not in op_names:
                op_names.append(name)
    for name in task["ops"]:
        if name not in op_names:
            op_names.append(name)
    return [reg.get(n) for n in op_names]


# ── helpers ───────────────────────────────────────────────────────────────────

def drop_to_shell():
    print("\n[escape hatch] dropping to shell — type 'exit' to return\n")
    os.system(os.environ.get("SHELL", "/bin/bash"))


def print_history(state: StateManager):
    hist = state.history()
    if not hist:
        print("No history.")
        return
    import datetime
    for h in hist[-20:]:
        ts = datetime.datetime.fromtimestamp(h.get("started_at", 0)).strftime("%Y-%m-%d %H:%M")
        print(f"  {ts}  {h['task']:<25} {h['status']:<12} "
              f"✓{h['done']} ✗{h['failed']} ⊘{h['skipped']}")


def _status_line(state: StateManager) -> str:
    s = state.summary()
    return (f"[{s['status']}] {s['task'] or '—'}  "
            f"✓{s['done']} ✗{s['failed']} ⊘{s['skipped']} ▶{s['pending']}  "
            f"{s['elapsed']}s")


# ── run a task ────────────────────────────────────────────────────────────────

def run_task(task_name: str, reg: Registry, state: StateManager,
             logger: Logger, dry_run: bool, interactive: bool = True):

    ops = _resolve_ops(task_name, reg)
    op_names = [o.name for o in ops]
    state.start_task(task_name, op_names)

    def on_event(event, op, msg=""):
        logger.add(event, op, msg)
        if not interactive:
            icons = {"run": "▶", "done": "✓", "failed": "✗", "skip": "⊘",
                     "retry": "↺", "fatal": "☠", "dry": "~"}
            print(f"  {icons.get(event,'·')} {op}" + (f": {msg}" if msg else ""))

    executor = Executor(state, on_event=on_event, dry_run=dry_run)

    if interactive:
        panel = LivePanel(logger, state.summary)
        panel.start()
        ok = executor.run(ops)
        panel.stop()
    else:
        ok = executor.run(ops)

    return ok


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="mend", add_help=True)
    parser.add_argument("--dry-run",  action="store_true", help="show what would run")
    parser.add_argument("--task",     metavar="NAME",      help="run task non-interactively")
    parser.add_argument("--shell",    action="store_true", help="drop to shell")
    parser.add_argument("--history",  action="store_true", help="print run history")
    args = parser.parse_args()

    if args.shell:
        drop_to_shell()
        return

    reg   = build_registry()
    state = StateManager()
    logger = Logger()

    if args.history:
        print_history(state)
        return

    if args.task:
        if args.task not in TASKS:
            print(f"Unknown task: {args.task!r}. Available: {', '.join(TASKS)}")
            sys.exit(1)
        ok = run_task(args.task, reg, state, logger, dry_run=args.dry_run, interactive=False)
        sys.exit(0 if ok else 1)

    # ── interactive UI ────────────────────────────────────────────────────────

    if state.is_interrupted:
        choice = Menu(
            title=f"⚡ Interrupted: {state.task}",
            items=["Resume", "Discard and start fresh"],
        ).run()
        if choice == "Discard and start fresh":
            state.reset()
        elif choice is None:
            return

    tags = _all_tags(reg)

    while True:
        task_names = list(TASKS.keys())
        items = task_names + ["──", "── View log", "── History", "── Shell", "── Quit"]

        choice = Menu(
            title="mend",
            items=items,
            status_fn=lambda: _status_line(state),
            tags=tags,
        ).run()

        if choice is None or choice == "── Quit":
            break
        elif choice == "──":
            continue
        elif choice == "── Shell":
            drop_to_shell()
        elif choice == "── View log":
            logger.show(state.summary())
        elif choice == "── History":
            # show history in log viewer style
            hist_logger = Logger()
            for h in state.history():
                import datetime
                ts = datetime.datetime.fromtimestamp(h.get("started_at", 0)).strftime("%H:%M")
                ev = "done" if h["status"] == "done" else "failed"
                hist_logger.add(ev, h["task"],
                                f"{h['status']} ✓{h['done']} ✗{h['failed']} ⊘{h['skipped']}")
            hist_logger.show({"task": "history", "status": "", "done": 0,
                              "failed": 0, "skipped": 0, "pending": 0, "elapsed": 0})
        elif choice == "__pause__":
            state.pause() if state.status == "running" else state.resume()
        elif choice == "__reset__":
            state.reset()
        elif choice in TASKS:
            ok = run_task(choice, reg, state, logger,
                          dry_run=args.dry_run, interactive=True)
            # brief result shown in status line on next menu render


if __name__ == "__main__":
    main()
