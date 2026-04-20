"""
Executor — convergence loop with parallel execution, exponential backoff, dry-run.

Flow per operation:
  1. Check preconditions → skip (recoverable) or halt (fatal)
  2. [dry-run: stop here, report what would run]
  3. Run action (with timeout enforcement)
  4. Check postconditions → retry with backoff, or fail
  5. Fatal failure halts task; recoverable failure skips after max_retries

Parallel execution:
  Operations marked parallel=True are batched and run concurrently via threads.
  Sequential ops run one at a time in order.
"""

from __future__ import annotations
from registry import Operation
from state import StateManager
from inspector import snapshot
import time, threading
from typing import List


class Executor:
    def __init__(self, state: StateManager, on_event=None, dry_run: bool = False):
        self.state = state
        self.on_event = on_event or (lambda ev, op, msg: None)
        self.dry_run = dry_run
        self._fatal = threading.Event()  # signals parallel workers to abort

    def _emit(self, event: str, op_name: str, msg: str = ""):
        self.on_event(event, op_name, msg)

    def _check_all(self, checks, env) -> tuple[bool, str]:
        for check in checks:
            ok, reason = check(env)
            if not ok:
                return False, reason
        return True, ""

    def _backoff(self, attempt: int):
        """Exponential backoff: 1s, 2s, 4s, capped at 16s."""
        time.sleep(min(2 ** attempt, 16))

    def _run_one(self, op: Operation, env: dict):
        """Execute a single operation with retry/backoff. Thread-safe."""
        s = self.state

        if self._fatal.is_set() or s.should_stop:
            return

        if op.name not in s.pending:
            return  # already handled

        # ── preconditions ─────────────────────────────────────────────────────
        ok, reason = self._check_all(op.preconditions, env)
        if not ok:
            if op.failure_class == "fatal":
                self._emit("fatal", op.name, f"precondition failed: {reason}")
                s.fail_task(f"{op.name}: {reason}")
                self._fatal.set()
            else:
                self._emit("skip", op.name, f"precondition unmet: {reason}")
                s.mark_skipped(op.name, reason)
            return

        # ── dry-run ───────────────────────────────────────────────────────────
        if self.dry_run:
            self._emit("dry", op.name, f"would run: {op.description}")
            s.mark_skipped(op.name, "dry-run")
            return

        # ── action + retry loop ───────────────────────────────────────────────
        s.op_start(op.name)
        success = False
        last_err = ""
        output = ""
        attempt = 0

        while s.retry_count(op.name) <= op.max_retries:
            if self._fatal.is_set() or s.should_stop:
                break
            try:
                self._emit("run", op.name, f"attempt {attempt + 1}")
                output = op.action(env) or ""

                # postconditions on fresh env
                fresh = snapshot()
                ok, reason = self._check_all(op.postconditions, fresh)
                if ok:
                    success = True
                    break
                last_err = f"postcondition failed: {reason}"
                self._emit("retry", op.name, last_err)

            except Exception as e:
                last_err = str(e)
                self._emit("retry", op.name, last_err)

            s.increment_retry(op.name)
            attempt += 1
            self._backoff(attempt)

        if self._fatal.is_set() or s.should_stop:
            return

        if success:
            self._emit("done", op.name)
            s.mark_done(op.name, output)
        else:
            if op.failure_class == "fatal":
                self._emit("fatal", op.name, last_err)
                s.mark_failed(op.name, last_err, output)
                s.fail_task(f"fatal failure at {op.name}")
                self._fatal.set()
            else:
                self._emit("failed", op.name, last_err)
                s.mark_failed(op.name, last_err, output)

    def run(self, ops: List[Operation]) -> bool:
        """
        Run operations in order, batching consecutive parallel ops together.
        Returns True if task completed (or dry-run), False on fatal failure.
        """
        s = self.state
        self._fatal.clear()

        # group into sequential batches: [[seq], [par, par, par], [seq], ...]
        batches: list[list[Operation]] = []
        for op in ops:
            if op.parallel and batches and all(o.parallel for o in batches[-1]):
                batches[-1].append(op)
            else:
                batches.append([op])

        for batch in batches:
            if self._fatal.is_set() or s.should_stop:
                break

            env = snapshot()

            if len(batch) == 1 or not batch[0].parallel:
                self._run_one(batch[0], env)
            else:
                # run parallel batch concurrently
                threads = [
                    threading.Thread(target=self._run_one, args=(op, env), daemon=True)
                    for op in batch
                ]
                for t in threads: t.start()
                for t in threads: t.join()

            if self._fatal.is_set():
                return False

        if not s.should_stop and not self._fatal.is_set():
            s.finish_task()
        return not self._fatal.is_set()
