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

import sys, os, argparse, shutil, subprocess, datetime
from registry import Registry, make_mount_op, make_pkg_op, make_copy_op, make_shell_op, make_health_op, Operation
from state import StateManager
from watchdog import Watchdog, HealthCheck, list_snapshots, rollback_to
from executor import Executor
from inspector import snapshot
from ui import Menu, Logger, LivePanel

HOME = os.path.expanduser("~")
BACKUP_DIR = os.path.join(HOME, "backup")
GITHUB_REPO_DIR = os.path.join(HOME, ".mend-backup-repo")

# Paths excluded from rsync (virtual/ephemeral filesystems)
RSYNC_EXCLUDES = [
    "--exclude=/proc/*", "--exclude=/sys/*", "--exclude=/dev/*",
    "--exclude=/run/*",  "--exclude=/tmp/*", "--exclude=/lost+found",
    "--exclude=/mnt/*",  "--exclude=/media/*",
]

# Files/dirs pushed to GitHub (configs + dotfiles + package list)
GITHUB_BACKUP_PATHS = [
    "/etc",
    os.path.join(HOME, ".config"),
    os.path.join(HOME, ".bashrc"),
    os.path.join(HOME, ".zshrc"),
    os.path.join(HOME, ".profile"),
    os.path.join(HOME, ".ssh", "config"),
]


# ── backup/recover operations ─────────────────────────────────────────────────

def _make_backup_op() -> Operation:
    def action(env) -> str:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        cmd = ["rsync", "-aAXH", "--delete", "--info=progress2"] + RSYNC_EXCLUDES + ["/", BACKUP_DIR + "/"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()[-4000:]
        if r.returncode not in (0, 24):  # 24 = vanished files (ok)
            raise RuntimeError(f"rsync failed: {out}")
        return out

    return Operation(
        name="backup_system",
        description=f"rsync full system → {BACKUP_DIR}",
        preconditions=[lambda env: (shutil.which("rsync") is not None, "rsync not found")],
        action=action,
        postconditions=[lambda env: (os.path.isdir(BACKUP_DIR), f"{BACKUP_DIR} missing after backup")],
        failure_class="system",
        max_retries=1,
        tags=["backup"],
    )


def _make_github_push_op() -> Operation:
    def action(env) -> str:
        logs = []

        # 1. Ensure git is available (install if missing)
        if not shutil.which("git"):
            logs.append("git not found — installing...")
            pkg_mgr = env.get("pkg_manager", "apt")
            cmds = {
                "apt":    ["apt-get", "install", "-y", "git"],
                "pacman": ["pacman", "-S", "--noconfirm", "git"],
                "dnf":    ["dnf", "install", "-y", "git"],
            }
            r = subprocess.run(cmds.get(pkg_mgr, cmds["apt"]), capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"failed to install git: {r.stderr.strip()}")
            logs.append("git installed.")

        # 2. Prefer gh CLI if available; install if not
        use_gh = bool(shutil.which("gh"))
        if not use_gh:
            logs.append("gh CLI not found — installing...")
            r = subprocess.run(
                ["bash", "-c",
                 "type -p curl >/dev/null || apt-get install -y curl; "
                 "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg "
                 "| dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && "
                 "chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && "
                 "echo 'deb [arch=$(dpkg --print-architecture) "
                 "signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] "
                 "https://cli.github.com/packages stable main' "
                 "| tee /etc/apt/sources.list.d/github-cli.list > /dev/null && "
                 "apt-get update && apt-get install -y gh"],
                capture_output=True, text=True
            )
            use_gh = r.returncode == 0 and bool(shutil.which("gh"))
            logs.append("gh installed." if use_gh else "gh install failed — falling back to git.")

        # 3. Init repo dir and copy backup paths into it
        os.makedirs(GITHUB_REPO_DIR, exist_ok=True)
        for src in GITHUB_BACKUP_PATHS:
            if not os.path.exists(src):
                continue
            dst = os.path.join(GITHUB_REPO_DIR, src.lstrip("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("*.sock", "*.pid"))
            else:
                shutil.copy2(src, dst)

        # 4. Dump package list
        pkg_mgr = env.get("pkg_manager", "apt")
        pkg_cmds = {
            "apt":    ["dpkg", "--get-selections"],
            "pacman": ["pacman", "-Qq"],
            "dnf":    ["rpm", "-qa", "--qf", "%{NAME}\\n"],
        }
        r = subprocess.run(pkg_cmds.get(pkg_mgr, pkg_cmds["apt"]), capture_output=True, text=True)
        with open(os.path.join(GITHUB_REPO_DIR, "packages.txt"), "w") as f:
            f.write(r.stdout)

        def git(args, cwd=GITHUB_REPO_DIR):
            return subprocess.run(["git"] + args, capture_output=True, text=True, cwd=cwd)

        # 5. Init git repo if needed
        if not os.path.isdir(os.path.join(GITHUB_REPO_DIR, ".git")):
            git(["init"])
            git(["checkout", "-b", "main"])

        # 6. Create private GitHub repo if using gh and remote not set
        remote = git(["remote", "get-url", "origin"]).stdout.strip()
        if not remote and use_gh:
            repo_name = "mend-system-backup"
            r = subprocess.run(
                ["gh", "repo", "create", repo_name, "--private", "--source", GITHUB_REPO_DIR, "--push"],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                logs.append(f"gh repo create failed: {r.stderr.strip()} — skipping push")
                return "\n".join(logs)
            logs.append(f"Created private repo: {repo_name}")
        elif not remote:
            logs.append("No remote set and gh not available — skipping push. Set origin manually.")
            return "\n".join(logs)

        # 7. Commit and push
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        git(["add", "-A"])
        git(["commit", "-m", f"backup: {stamp}"])
        r = git(["push", "-u", "origin", "main"])
        if r.returncode != 0:
            raise RuntimeError(f"git push failed: {r.stderr.strip()}")
        logs.append(f"Pushed backup to GitHub ({stamp})")
        return "\n".join(logs)

    return Operation(
        name="backup_push_github",
        description="Push configs/dotfiles/package list to private GitHub repo",
        preconditions=[lambda env: (env.get("internet", False), "no internet")],
        action=action,
        postconditions=[],
        failure_class="recoverable",
        max_retries=1,
        tags=["backup", "github"],
    )


def _make_recover_op() -> Operation:
    def action(env) -> str:
        if not os.path.isdir(BACKUP_DIR):
            raise RuntimeError(f"No backup found at {BACKUP_DIR}")
        cmd = ["rsync", "-aAXH", "--delete", "--info=progress2",
               "--exclude=/proc/*", "--exclude=/sys/*", "--exclude=/dev/*",
               "--exclude=/run/*", "--exclude=/tmp/*",
               BACKUP_DIR + "/", "/"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()[-4000:]
        if r.returncode not in (0, 24):
            raise RuntimeError(f"rsync restore failed: {out}")
        return out

    return Operation(
        name="recover_system",
        description=f"Restore full system from {BACKUP_DIR}",
        preconditions=[
            lambda env: (shutil.which("rsync") is not None, "rsync not found"),
            lambda env: (os.path.isdir(BACKUP_DIR), f"no backup at {BACKUP_DIR}"),
        ],
        action=action,
        postconditions=[],
        failure_class="system",
        max_retries=0,
        tags=["recover", "destructive"],
    )


def _make_schedule_op() -> Operation:
    """Install a systemd timer that runs backup every 3 days."""
    def action(env) -> str:
        mend_path = os.path.abspath(__file__)
        service = f"""[Unit]
Description=mend auto backup

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {mend_path} --task Backup
"""
        timer = """[Unit]
Description=mend auto backup — every 3 days

[Timer]
OnCalendar=*-*-1/3
Persistent=true

[Install]
WantedBy=timers.target
"""
        svc_path = "/etc/systemd/system/mend-backup.service"
        tmr_path = "/etc/systemd/system/mend-backup.timer"
        with open(svc_path, "w") as f:
            f.write(service)
        with open(tmr_path, "w") as f:
            f.write(timer)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", "mend-backup.timer"], check=True)
        return f"Timer installed: {tmr_path}"

    return Operation(
        name="setup_backup_schedule",
        description="Install systemd timer: backup every 3 days",
        preconditions=[lambda env: (shutil.which("systemctl") is not None, "systemctl not found")],
        action=action,
        postconditions=[lambda env: (os.path.exists("/etc/systemd/system/mend-backup.timer"), "timer file missing")],
        failure_class="recoverable",
        max_retries=1,
        tags=["backup", "schedule"],
    )


# ── default health checks ─────────────────────────────────────────────────────

def build_watchdog(interval: int = 3600) -> Watchdog:
    wd = Watchdog(interval=interval)

    wd.register(HealthCheck(
        name="disk_space",
        description="No mounted filesystem is over 90% full",
        check=lambda env: next(
            ((False, f"{e['mount']} at {e['use%']}") for e in env.get("disk", [])
             if int(e.get("use%", "0%").rstrip("%") or 0) >= 90),
            (True, "ok")
        ),
        severity="critical",
        ai_correct=True,
        tags=["disk"],
    ))

    wd.register(HealthCheck(
        name="memory_pressure",
        description="Available memory is above 10% of total",
        check=lambda env: (
            (True, "ok") if not env.get("memory", {}).get("total_mb")
            else (
                env["memory"]["available_mb"] / env["memory"]["total_mb"] >= 0.10,
                f"{env['memory']['available_mb']}MB free of {env['memory']['total_mb']}MB"
            )
        ),
        severity="warn",
        ai_correct=True,
        tags=["memory"],
    ))

    wd.register(HealthCheck(
        name="internet_connectivity",
        description="System has internet access",
        check=lambda env: (env.get("internet", False), "no internet"),
        severity="warn",
        ai_correct=False,
        tags=["network"],
    ))

    wd.register(HealthCheck(
        name="fstab_valid",
        description="/etc/fstab entries are all mountable",
        check=lambda env: (
            subprocess.run(["findmnt", "--verify"], capture_output=True).returncode == 0,
            "fstab verification failed"
        ),
        severity="critical",
        ai_correct=True,
        tags=["disk"],
    ))

    wd.register(HealthCheck(
        name="backup_exists",
        description=f"A backup exists at {BACKUP_DIR}",
        check=lambda env: (os.path.isdir(BACKUP_DIR), f"no backup at {BACKUP_DIR}"),
        severity="warn",
        ai_correct=False,
        tags=["backup"],
    ))

    return wd


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
    reg.register(_make_backup_op())
    reg.register(_make_github_push_op())
    reg.register(_make_recover_op())
    reg.register(_make_schedule_op())

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
    "Backup": {
        "ops":  ["backup_system"],
        "deps": [],
    },
    "Backup + GitHub": {
        "ops":  ["backup_system", "backup_push_github"],
        "deps": [],
    },
    "Recover": {
        "ops":  ["recover_system"],
        "deps": [],
    },
    "Setup Backup Schedule": {
        "ops":  ["setup_backup_schedule"],
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
    parser.add_argument("--dry-run",    action="store_true", help="show what would run")
    parser.add_argument("--task",       metavar="NAME",      help="run task non-interactively")
    parser.add_argument("--shell",      action="store_true", help="drop to shell")
    parser.add_argument("--history",    action="store_true", help="print run history")
    parser.add_argument("--watch",      action="store_true", help="start continuous health check loop")
    parser.add_argument("--watch-once", action="store_true", help="run one health check cycle and exit")
    parser.add_argument("--watch-interval", type=int, default=3600, metavar="SECS",
                        help="seconds between watch cycles (default: 3600)")
    parser.add_argument("--rollback",   metavar="SNAPSHOT",  help="rollback to a named snapshot")
    parser.add_argument("--snapshots",  action="store_true", help="list available rollback snapshots")
    parser.add_argument("--lkg",        action="store_true", help="rollback to last known good state")
    parser.add_argument("--external",   metavar="TARGET", action="append", default=[],
                        help="external sync target for verified snapshots (repeatable)")
    args = parser.parse_args()

    if args.shell:
        drop_to_shell()
        return

    if args.snapshots:
        snaps = list_snapshots()
        if not snaps:
            print("No rollback snapshots found.")
        for s in snaps:
            size_mb = s["size"] // (1024 * 1024)
            print(f"  {s['name']}  ({size_mb} MB)  {s['path']}")
        return

    if args.rollback:
        import glob as _glob
        # allow partial name match
        snaps = list_snapshots()
        match = next((s for s in snaps if args.rollback in s["name"]), None)
        if not match:
            print(f"No snapshot matching '{args.rollback}'. Run --snapshots to list.")
            sys.exit(1)
        print(f"⚠️  Rolling back to: {match['name']}")
        confirm = input("This will overwrite your current system. Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)
        ok, out = rollback_to(match["path"])
        print(out)
        sys.exit(0 if ok else 1)

    if args.watch or args.watch_once:
        wd = build_watchdog(interval=args.watch_interval)
        if args.watch_once:
            wd.run_cycle(verbose=True)
        else:
            wd.start(verbose=True)
        return

    reg   = build_registry()
    state = StateManager()
    logger = Logger()

    # register external sync targets
    for target in args.external:
        state.external_targets.append(target)

    if args.lkg:
        state.rollback_to_last_known_good()
        return

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
            logger=logger,
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
        elif choice == "__watch_cycle__":
            wd = build_watchdog()
            wd.run_cycle(verbose=True)
            input("\nPress Enter to return...")
        elif choice == "__snapshots__":
            from ui import pick_snapshot
            snaps = list_snapshots()
            path  = pick_snapshot(snaps)
            if path:
                confirm = input(f"\nRoll back to {os.path.basename(path)}? [y/N] ").strip().lower()
                if confirm == "y":
                    ok, out = rollback_to(path)
                    print(out[:400])
                    input("Press Enter to continue...")
        elif choice in TASKS:
            ok = run_task(choice, reg, state, logger,
                          dry_run=args.dry_run, interactive=True)
            # brief result shown in status line on next menu render


if __name__ == "__main__":
    main()
