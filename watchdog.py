"""
watchdog.py — routine validation cycles, periodic system checks,
              AI-assisted correction, and rollback to previous valid states.

Architecture:
  - HealthCheck: a named check function + severity + optional AI correction
  - Watchdog: runs all checks on a schedule, diffs snapshots, triggers correction
  - Rollback: rsync from a timestamped snapshot in ~/backup/snapshots/

Rollback snapshots are taken before any correction is applied, so you can
always go back to the last known-good state.
"""

import os, json, time, shutil, subprocess, threading, signal
from dataclasses import dataclass, field
from typing import Callable, Optional
from inspector import snapshot

HOME        = os.path.expanduser("~")
WATCH_STATE = os.path.join(os.path.dirname(__file__), "watchdog.json")
SNAPSHOTS   = os.path.join(HOME, "backup", "snapshots")

# Keys tracked for drift detection between cycles
_DRIFT_KEYS = ("packages", "services", "mounts", "processes", "disk", "network")


# ── data types ────────────────────────────────────────────────────────────────

@dataclass
class HealthCheck:
    name: str
    description: str
    check: Callable[[dict], tuple[bool, str]]   # (env) -> (ok, reason)
    severity: str = "warn"                       # "warn" | "critical"
    ai_correct: bool = False                     # ask Kiro to fix on failure
    tags: list = field(default_factory=list)


@dataclass
class CheckResult:
    name: str
    ok: bool
    reason: str
    severity: str
    corrected: bool = False
    correction_log: str = ""
    ts: float = field(default_factory=time.time)


# ── rollback ──────────────────────────────────────────────────────────────────

def _take_rollback_snapshot(label: str) -> str:
    """rsync / → SNAPSHOTS/<timestamp>-<label>/ before applying a correction."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest  = os.path.join(SNAPSHOTS, f"{stamp}-{label}")
    os.makedirs(dest, exist_ok=True)
    excludes = [
        "--exclude=/proc/*", "--exclude=/sys/*", "--exclude=/dev/*",
        "--exclude=/run/*",  "--exclude=/tmp/*", "--exclude=/lost+found",
        f"--exclude={SNAPSHOTS}",
    ]
    subprocess.run(
        ["rsync", "-aAXH", "--delete"] + excludes + ["/", dest + "/"],
        capture_output=True,
    )
    return dest


def rollback_to(snapshot_path: str) -> tuple[bool, str]:
    """Restore system from a rollback snapshot."""
    if not os.path.isdir(snapshot_path):
        return False, f"snapshot not found: {snapshot_path}"
    r = subprocess.run(
        ["rsync", "-aAXH", "--delete",
         "--exclude=/proc/*", "--exclude=/sys/*", "--exclude=/dev/*",
         "--exclude=/run/*",  "--exclude=/tmp/*",
         snapshot_path + "/", "/"],
        capture_output=True, text=True,
    )
    ok = r.returncode in (0, 24)
    return ok, (r.stdout + r.stderr).strip()[-2000:]


def list_snapshots() -> list[dict]:
    """Return available rollback snapshots sorted newest-first."""
    if not os.path.isdir(SNAPSHOTS):
        return []
    entries = []
    for name in sorted(os.listdir(SNAPSHOTS), reverse=True):
        path = os.path.join(SNAPSHOTS, name)
        if os.path.isdir(path):
            entries.append({"name": name, "path": path,
                            "size": shutil.disk_usage(path).used})
    return entries


# ── AI correction ─────────────────────────────────────────────────────────────

def _kiro_correct(check: HealthCheck, reason: str, env: dict) -> tuple[bool, str]:
    """Ask Kiro CLI to fix a failed health check. Returns (fixed, log)."""
    kiro = shutil.which("kiro-cli")
    if not kiro:
        return False, "kiro-cli not available"

    mend_dir = os.path.dirname(os.path.abspath(__file__))
    env_summary = {k: env[k] for k in ("distro", "kernel", "init", "pkg_manager",
                                        "internet", "disk", "memory", "services")
                   if k in env}
    prompt = (
        f"System health check '{check.name}' failed: {reason}\n"
        f"Check description: {check.description}\n"
        f"Environment: {json.dumps(env_summary, indent=2)}\n\n"
        "Diagnose and fix the issue using available tools. "
        "Be concise. Confirm what you did."
    )
    r = subprocess.run(
        [kiro, "chat", "--trust-all-tools", "--no-interactive", prompt],
        capture_output=True, text=True, cwd=mend_dir,
    )
    return r.returncode == 0, r.stdout.strip()[-2000:]


# ── snapshot diffing ──────────────────────────────────────────────────────────

def _diff_snapshots(prev: dict, curr: dict) -> list[str]:
    """Return human-readable list of meaningful changes between two env snapshots."""
    diffs = []

    # packages added/removed
    prev_pkgs = set(prev.get("packages", []))
    curr_pkgs = set(curr.get("packages", []))
    added   = curr_pkgs - prev_pkgs
    removed = prev_pkgs - curr_pkgs
    if added:   diffs.append(f"packages added: {', '.join(sorted(added)[:10])}")
    if removed: diffs.append(f"packages removed: {', '.join(sorted(removed)[:10])}")

    # mounts changed
    prev_mounts = set(prev.get("mounts", []))
    curr_mounts = set(curr.get("mounts", []))
    if prev_mounts != curr_mounts:
        diffs.append(f"mounts changed: +{curr_mounts - prev_mounts} -{prev_mounts - curr_mounts}")

    # disk usage — flag if any mount crossed 90% used
    for entry in curr.get("disk", []):
        pct = entry.get("use%", "0%").rstrip("%")
        try:
            if int(pct) >= 90:
                diffs.append(f"disk {entry['mount']} at {entry['use%']} used")
        except ValueError:
            pass

    # memory — flag if available < 10% of total
    mem = curr.get("memory", {})
    total = mem.get("total_mb", 0)
    avail = mem.get("available_mb", 0)
    if total and avail / total < 0.10:
        diffs.append(f"low memory: {avail}MB available of {total}MB")

    return diffs


# ── watchdog ──────────────────────────────────────────────────────────────────

class Watchdog:
    """
    Runs registered HealthChecks on a schedule.
    On failure: logs result, optionally asks Kiro to correct.
    Before any correction: takes a rollback snapshot.
    Persists state to watchdog.json.
    """

    def __init__(self, interval: int = 3600):
        self.interval   = interval   # seconds between full check cycles
        self.checks: list[HealthCheck] = []
        self._state     = self._load()
        self._stop      = threading.Event()
        self._prev_env: Optional[dict] = None

    # ── registration ─────────────────────────────────────────────────────────

    def register(self, check: HealthCheck):
        self.checks.append(check)

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(WATCH_STATE):
            with open(WATCH_STATE) as f:
                return json.load(f)
        return {"cycles": 0, "last_run": None, "results": [], "corrections": []}

    def _save(self):
        with open(WATCH_STATE, "w") as f:
            json.dump(self._state, f, indent=2)

    # ── single cycle ─────────────────────────────────────────────────────────

    def run_cycle(self, verbose: bool = True) -> list[CheckResult]:
        env = snapshot()
        results: list[CheckResult] = []

        # drift detection
        if self._prev_env:
            diffs = _diff_snapshots(self._prev_env, env)
            for d in diffs:
                print(f"  📊 drift: {d}")
        self._prev_env = env

        for check in self.checks:
            ok, reason = check.check(env)
            result = CheckResult(name=check.name, ok=ok, reason=reason,
                                 severity=check.severity)

            if not ok:
                icon = "🔴" if check.severity == "critical" else "🟡"
                if verbose:
                    print(f"  {icon} {check.name}: {reason}")

                if check.ai_correct:
                    # snapshot before touching anything
                    snap_path = _take_rollback_snapshot(check.name)
                    if verbose:
                        print(f"     📸 rollback snapshot: {snap_path}")

                    fixed, log = _kiro_correct(check, reason, env)
                    result.corrected      = fixed
                    result.correction_log = log
                    if verbose:
                        status = "✅ corrected" if fixed else "❌ correction failed"
                        print(f"     {status}: {log[:200]}")

                    self._state["corrections"].append({
                        "check": check.name, "reason": reason,
                        "fixed": fixed, "snapshot": snap_path,
                        "ts": time.time(),
                    })
            else:
                if verbose:
                    print(f"  ✅ {check.name}")

            results.append(result)

        self._state["cycles"] += 1
        self._state["last_run"] = time.time()
        self._state["results"] = [
            {"name": r.name, "ok": r.ok, "reason": r.reason,
             "severity": r.severity, "corrected": r.corrected, "ts": r.ts}
            for r in results
        ]
        self._save()
        return results

    # ── continuous loop ───────────────────────────────────────────────────────

    def start(self, verbose: bool = True):
        """Run check cycles in a loop until stop() is called."""
        signal.signal(signal.SIGINT,  lambda s, f: self.stop())
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())

        print(f"🔍 Watchdog started — {len(self.checks)} checks, interval {self.interval}s")
        while not self._stop.is_set():
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Running health checks...")
            self.run_cycle(verbose=verbose)
            self._stop.wait(self.interval)

        print("Watchdog stopped.")

    def stop(self):
        self._stop.set()

    # ── status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "cycles":       self._state["cycles"],
            "last_run":     self._state["last_run"],
            "checks":       len(self.checks),
            "last_results": self._state.get("results", []),
            "corrections":  len(self._state.get("corrections", [])),
            "snapshots":    list_snapshots(),
        }
