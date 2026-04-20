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
    "op_output": {op_name: str},        # captured stdout/stderr per op
    "timings":  {op_name: float},       # duration in seconds per op
    "started_at": float,                # task start timestamp
    "log":      [{op, status, reason, ts, duration}, ...],
    "history":  [{task, status, started_at, finished_at, summary}, ...]
  }
"""

import json, os, signal, time
from typing import Optional

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
