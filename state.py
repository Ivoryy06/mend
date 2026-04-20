"""
State manager — persists run state and full history across sessions.

state.json schema:
  {
    "task":     str,
    "status":   "idle"|"running"|"paused"|"interrupted"|"done"|"failed",
    "pending":  [op_name, ...],
    "done":     [op_name, ...],
    "failed":   [op_name, ...],
    "skipped":  [op_name, ...],
    "retries":  {op_name: count},
    "op_output": {op_name: str},
    "timings":  {op_name: float},
    "started_at": float,
    "log":      [{op, status, reason, ts, duration}, ...],
    "history":  [{task, status, started_at, finished_at, summary}, ...],
    "last_known_good": {
        "snapshot": str,          # path to last verified local snapshot
        "ts": float,
        "task": str,
        "external_copies": [str]  # paths where this snapshot was also synced
    }
  }
"""

import json, os, signal, time, subprocess, shutil
from typing import Optional, Callable, List

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

_DEFAULTS = {
    "task": None,
    "status": "idle",
    "pending": [],
    "done": [],
    "failed": [],
    "skipped": [],
    "retries": {},
    "op_output": {},
    "timings": {},
    "started_at": None,
    "log": [],
    "history": [],
    "last_known_good": None,
}

# Verifier: callable(env) -> (ok: bool, reason: str)
Verifier = Callable[[dict], tuple[bool, str]]


class StateManager:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self._state = self._load()
        self._paused = False
        self._stop = False
        self._op_start: dict[str, float] = {}
        # Verifiers run before a task result is accepted as "done"
        self._verifiers: List[Verifier] = []
        # External paths to sync snapshots to (USB, NAS, remote rsync target)
        self.external_targets: List[str] = []
        signal.signal(signal.SIGINT, self._handle_interrupt)
        signal.signal(signal.SIGTERM, self._handle_interrupt)

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
                return {**_DEFAULTS, **data}
        return dict(_DEFAULTS)

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self._state, f, indent=2)

    def reset(self):
        history = self._state.get("history", [])
        lkg     = self._state.get("last_known_good")
        self._state = {**dict(_DEFAULTS), "history": history, "last_known_good": lkg}
        self.save()

    # ── signal handling ───────────────────────────────────────────────────────

    def _handle_interrupt(self, sig, frame):
        self._state["status"] = "interrupted"
        self.save()
        self._stop = True

    # ── verifier registration ─────────────────────────────────────────────────

    def add_verifier(self, fn: Verifier):
        """Register a verification function run before accepting task completion."""
        self._verifiers.append(fn)

    # ── task lifecycle ────────────────────────────────────────────────────────

    def start_task(self, task: str, ops: list):
        if self._state["task"] == task and self._state["status"] == "interrupted":
            self._state["status"] = "running"
        else:
            self._state.update({
                "task": task,
                "status": "running",
                "pending": list(ops),
                "done": [],
                "failed": [],
                "skipped": [],
                "retries": {},
                "op_output": {},
                "timings": {},
                "started_at": time.time(),
                "log": [],
            })
        self.save()

    def finish_task(self) -> bool:
        """
        Verify state before accepting completion.
        If verification passes: mark done, take a verified snapshot, update last_known_good.
        If verification fails: reject, roll back to last_known_good, mark failed.
        Returns True if accepted, False if rolled back.
        """
        from inspector import snapshot as _snapshot
        env = _snapshot()

        failures = []
        for fn in self._verifiers:
            try:
                ok, reason = fn(env)
                if not ok:
                    failures.append(reason)
            except Exception as e:
                failures.append(f"verifier error: {e}")

        if failures:
            reason = "; ".join(failures)
            self._log_entry("__verify__", "failed", reason)
            self._state["status"] = "failed"
            self._archive_to_history("failed")
            self.save()
            print(f"\n⚠️  State verification failed: {reason}")
            self._do_rollback()
            return False

        # Verification passed — take a snapshot and record as last_known_good
        snap_path = self._take_verified_snapshot()
        external  = self._sync_to_external(snap_path)
        self._state["last_known_good"] = {
            "snapshot": snap_path,
            "ts":       time.time(),
            "task":     self._state["task"],
            "external_copies": external,
        }
        self._state["status"] = "done"
        self._archive_to_history("done")
        self.save()
        print(f"✅ State verified and accepted. Snapshot: {snap_path}")
        if external:
            print(f"   External copies: {', '.join(external)}")
        return True

    def fail_task(self, reason: str):
        self._state["status"] = "failed"
        self._log_entry("__task__", "failed", reason)
        self._archive_to_history("failed")
        self.save()

    def pause(self):
        self._paused = True
        self._state["status"] = "paused"
        self.save()

    def resume(self):
        self._paused = False
        self._state["status"] = "running"
        self.save()

    # ── verified snapshot ─────────────────────────────────────────────────────

    def _take_verified_snapshot(self) -> str:
        """rsync / → snapshots/verified-<timestamp>/ and return the path."""
        home      = os.path.expanduser("~")
        snap_root = os.path.join(home, "backup", "snapshots")
        stamp     = time.strftime("%Y%m%d-%H%M%S")
        dest      = os.path.join(snap_root, f"verified-{stamp}")
        os.makedirs(dest, exist_ok=True)
        excludes  = [
            "--exclude=/proc/*", "--exclude=/sys/*", "--exclude=/dev/*",
            "--exclude=/run/*",  "--exclude=/tmp/*", "--exclude=/lost+found",
            f"--exclude={snap_root}",
        ]
        subprocess.run(
            ["rsync", "-aAXH", "--delete"] + excludes + ["/", dest + "/"],
            capture_output=True,
        )
        return dest

    # ── external sync ─────────────────────────────────────────────────────────

    def _sync_to_external(self, snap_path: str) -> List[str]:
        """
        Sync the verified snapshot to all registered external targets.
        Targets can be local paths (USB: /mnt/usb/mend-backup) or
        rsync remote targets (user@host:/path).
        Returns list of successfully synced targets.
        """
        synced = []
        for target in self.external_targets:
            try:
                dest = target.rstrip("/") + "/" + os.path.basename(snap_path) + "/"
                r = subprocess.run(
                    ["rsync", "-aAXH", "--delete", snap_path + "/", dest],
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode in (0, 24):
                    synced.append(target)
                else:
                    print(f"   ⚠️  External sync to {target} failed: {r.stderr.strip()[:200]}")
            except Exception as e:
                print(f"   ⚠️  External sync to {target} error: {e}")
        return synced

    # ── rollback ──────────────────────────────────────────────────────────────

    def _do_rollback(self):
        """Roll back to last_known_good, trying external copies if local is missing."""
        lkg = self._state.get("last_known_good")
        if not lkg:
            print("   No last_known_good recorded — cannot roll back automatically.")
            return

        # Try local snapshot first
        snap = lkg.get("snapshot", "")
        if snap and os.path.isdir(snap):
            print(f"   Rolling back to: {snap}")
            self._rsync_restore(snap)
            return

        # Try external copies
        for ext in lkg.get("external_copies", []):
            candidate = ext.rstrip("/") + "/" + os.path.basename(snap) + "/"
            print(f"   Local snapshot missing — trying external: {candidate}")
            r = subprocess.run(
                ["rsync", "-aAXH", "--delete",
                 "--exclude=/proc/*", "--exclude=/sys/*", "--exclude=/dev/*",
                 "--exclude=/run/*", "--exclude=/tmp/*",
                 candidate, "/"],
                capture_output=True, text=True,
            )
            if r.returncode in (0, 24):
                print(f"   ✅ Rolled back from external copy: {ext}")
                return
            print(f"   ❌ External copy failed: {r.stderr.strip()[:200]}")

        print("   ❌ All rollback sources exhausted.")

    def _rsync_restore(self, snap_path: str):
        r = subprocess.run(
            ["rsync", "-aAXH", "--delete",
             "--exclude=/proc/*", "--exclude=/sys/*", "--exclude=/dev/*",
             "--exclude=/run/*", "--exclude=/tmp/*",
             snap_path + "/", "/"],
            capture_output=True, text=True,
        )
        if r.returncode in (0, 24):
            print("   ✅ Rollback complete.")
        else:
            print(f"   ❌ Rollback rsync failed: {r.stderr.strip()[:200]}")

    def rollback_to_last_known_good(self):
        """Public: manually trigger rollback to last_known_good."""
        lkg = self._state.get("last_known_good")
        if not lkg:
            print("No last_known_good state recorded.")
            return False
        ts   = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lkg["ts"]))
        task = lkg.get("task", "unknown")
        print(f"Rolling back to last known good state: {task} @ {ts}")
        self._do_rollback()
        return True

    # ── op tracking ───────────────────────────────────────────────────────────

    def op_start(self, op: str):
        self._op_start[op] = time.time()

    def op_end(self, op: str) -> float:
        duration = time.time() - self._op_start.pop(op, time.time())
        self._state["timings"][op] = round(duration, 3)
        return duration

    def record_output(self, op: str, output: str):
        if output:
            self._state["op_output"][op] = output[-4000:]

    def mark_done(self, op: str, output: str = ""):
        s = self._state
        duration = self.op_end(op)
        if op in s["pending"]: s["pending"].remove(op)
        if op not in s["done"]: s["done"].append(op)
        self.record_output(op, output)
        self._log_entry(op, "done", duration=duration)
        self.save()

    def mark_failed(self, op: str, reason: str, output: str = ""):
        s = self._state
        duration = self.op_end(op)
        if op in s["pending"]: s["pending"].remove(op)
        if op not in s["failed"]: s["failed"].append(op)
        self.record_output(op, output)
        self._log_entry(op, "failed", reason, duration=duration)
        self.save()

    def mark_skipped(self, op: str, reason: str):
        s = self._state
        if op in s["pending"]: s["pending"].remove(op)
        if op not in s["skipped"]: s["skipped"].append(op)
        self._log_entry(op, "skipped", reason)
        self.save()

    def increment_retry(self, op: str) -> int:
        self._state["retries"][op] = self._state["retries"].get(op, 0) + 1
        self.save()
        return self._state["retries"][op]

    def retry_count(self, op: str) -> int:
        return self._state["retries"].get(op, 0)

    # ── queries ───────────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._state["status"]

    @property
    def task(self) -> Optional[str]:
        return self._state["task"]

    @property
    def pending(self) -> list:
        return list(self._state["pending"])

    @property
    def is_interrupted(self) -> bool:
        return self._state["status"] == "interrupted"

    @property
    def should_stop(self) -> bool:
        return self._stop or self._paused

    @property
    def last_known_good(self) -> Optional[dict]:
        return self._state.get("last_known_good")

    def get_output(self, op: str) -> str:
        return self._state["op_output"].get(op, "")

    def get_timing(self, op: str) -> Optional[float]:
        return self._state["timings"].get(op)

    def summary(self) -> dict:
        s = self._state
        elapsed = round(time.time() - s["started_at"], 1) if s["started_at"] else 0
        return {
            "task":    s["task"],
            "status":  s["status"],
            "done":    len(s["done"]),
            "failed":  len(s["failed"]),
            "skipped": len(s["skipped"]),
            "pending": len(s["pending"]),
            "elapsed": elapsed,
        }

    def history(self) -> list:
        return list(self._state.get("history", []))

    def log(self) -> list:
        return list(self._state.get("log", []))

    # ── internal ──────────────────────────────────────────────────────────────

    def _log_entry(self, op: str, status: str, reason: str = "", duration: float = None):
        entry = {"op": op, "status": status, "reason": reason, "ts": time.time()}
        if duration is not None:
            entry["duration"] = duration
        self._state["log"].append(entry)

    def _archive_to_history(self, status: str):
        s = self._state
        self._state["history"].append({
            "task":        s["task"],
            "status":      status,
            "started_at":  s.get("started_at"),
            "finished_at": time.time(),
            "done":        len(s["done"]),
            "failed":      len(s["failed"]),
            "skipped":     len(s["skipped"]),
        })
        self._state["history"] = self._state["history"][-50:]


STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

_DEFAULTS = {
    "task": None,
    "status": "idle",
    "pending": [],
    "done": [],
    "failed": [],
    "skipped": [],
    "retries": {},
    "op_output": {},
    "timings": {},
    "started_at": None,
    "log": [],
    "history": [],
}


class StateManager:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self._state = self._load()
        self._paused = False
        self._stop = False
        self._op_start: dict[str, float] = {}
        signal.signal(signal.SIGINT, self._handle_interrupt)
        signal.signal(signal.SIGTERM, self._handle_interrupt)

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
                return {**_DEFAULTS, **data}
        return dict(_DEFAULTS)

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self._state, f, indent=2)

    def reset(self):
        history = self._state.get("history", [])
        self._state = {**dict(_DEFAULTS), "history": history}
        self.save()

    # ── signal handling ───────────────────────────────────────────────────────

    def _handle_interrupt(self, sig, frame):
        self._state["status"] = "interrupted"
        self.save()
        self._stop = True

    # ── task lifecycle ────────────────────────────────────────────────────────

    def start_task(self, task: str, ops: list):
        if self._state["task"] == task and self._state["status"] == "interrupted":
            self._state["status"] = "running"
        else:
            self._state.update({
                "task": task,
                "status": "running",
                "pending": list(ops),
                "done": [],
                "failed": [],
                "skipped": [],
                "retries": {},
                "op_output": {},
                "timings": {},
                "started_at": time.time(),
                "log": [],
            })
        self.save()

    def finish_task(self):
        self._state["status"] = "done"
        self._archive_to_history("done")
        self.save()

    def fail_task(self, reason: str):
        self._state["status"] = "failed"
        self._log_entry("__task__", "failed", reason)
        self._archive_to_history("failed")
        self.save()

    def pause(self):
        self._paused = True
        self._state["status"] = "paused"
        self.save()

    def resume(self):
        self._paused = False
        self._state["status"] = "running"
        self.save()

    # ── op tracking ───────────────────────────────────────────────────────────

    def op_start(self, op: str):
        self._op_start[op] = time.time()

    def op_end(self, op: str) -> float:
        duration = time.time() - self._op_start.pop(op, time.time())
        self._state["timings"][op] = round(duration, 3)
        return duration

    def record_output(self, op: str, output: str):
        if output:
            self._state["op_output"][op] = output[-4000:]  # cap at 4k chars

    def mark_done(self, op: str, output: str = ""):
        s = self._state
        duration = self.op_end(op)
        if op in s["pending"]: s["pending"].remove(op)
        if op not in s["done"]: s["done"].append(op)
        self.record_output(op, output)
        self._log_entry(op, "done", duration=duration)
        self.save()

    def mark_failed(self, op: str, reason: str, output: str = ""):
        s = self._state
        duration = self.op_end(op)
        if op in s["pending"]: s["pending"].remove(op)
        if op not in s["failed"]: s["failed"].append(op)
        self.record_output(op, output)
        self._log_entry(op, "failed", reason, duration=duration)
        self.save()

    def mark_skipped(self, op: str, reason: str):
        s = self._state
        if op in s["pending"]: s["pending"].remove(op)
        if op not in s["skipped"]: s["skipped"].append(op)
        self._log_entry(op, "skipped", reason)
        self.save()

    def increment_retry(self, op: str) -> int:
        self._state["retries"][op] = self._state["retries"].get(op, 0) + 1
        self.save()
        return self._state["retries"][op]

    def retry_count(self, op: str) -> int:
        return self._state["retries"].get(op, 0)

    # ── queries ───────────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._state["status"]

    @property
    def task(self) -> Optional[str]:
        return self._state["task"]

    @property
    def pending(self) -> list:
        return list(self._state["pending"])

    @property
    def is_interrupted(self) -> bool:
        return self._state["status"] == "interrupted"

    @property
    def should_stop(self) -> bool:
        return self._stop or self._paused

    def get_output(self, op: str) -> str:
        return self._state["op_output"].get(op, "")

    def get_timing(self, op: str) -> Optional[float]:
        return self._state["timings"].get(op)

    def summary(self) -> dict:
        s = self._state
        elapsed = round(time.time() - s["started_at"], 1) if s["started_at"] else 0
        return {
            "task":    s["task"],
            "status":  s["status"],
            "done":    len(s["done"]),
            "failed":  len(s["failed"]),
            "skipped": len(s["skipped"]),
            "pending": len(s["pending"]),
            "elapsed": elapsed,
        }

    def history(self) -> list:
        return list(self._state.get("history", []))

    def log(self) -> list:
        return list(self._state.get("log", []))

    # ── internal ──────────────────────────────────────────────────────────────

    def _log_entry(self, op: str, status: str, reason: str = "", duration: float = None):
        entry = {"op": op, "status": status, "reason": reason, "ts": time.time()}
        if duration is not None:
            entry["duration"] = duration
        self._state["log"].append(entry)

    def _archive_to_history(self, status: str):
        s = self._state
        self._state["history"].append({
            "task":        s["task"],
            "status":      status,
            "started_at":  s.get("started_at"),
            "finished_at": time.time(),
            "done":        len(s["done"]),
            "failed":      len(s["failed"]),
            "skipped":     len(s["skipped"]),
        })
        # keep last 50 runs
        self._state["history"] = self._state["history"][-50:]
