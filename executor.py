"""
Executor — convergence loop with parallel execution, exponential backoff, dry-run.

On recoverable failure the executor pauses and offers:
  [A] Automated fix  — Kiro CLI analyses the error and attempts a fix, then retries
  [M] Manual fix     — drops to a shell so the user can fix it themselves, then retries
  [S] Skip           — mark op skipped and continue
  [X] Abort          — halt the task (state preserved for resume)

Parallel execution:
  Operations marked parallel=True are batched and run concurrently via threads.
  Sequential ops run one at a time in order.
"""

from __future__ import annotations
from registry import Operation
from state import StateManager
from inspector import snapshot
import time, threading, sys, os, subprocess, shutil
from typing import List
from ui import failure_menu

# Patterns that flag an operation as requiring explicit user confirmation
_RISKY_PATTERNS = (
    "rm ", "rm\t", "rm\n", "rmdir",
    "mv ", "mv\t",
    "dd ", "dd\t",
    "mkfs", "fdisk", "parted", "wipefs",
    "shred", "> /",
    "chmod 777", "chown root",
    "DROP ", "TRUNCATE ",
)

def _is_risky(op: Operation) -> bool:
    if "destructive" in op.tags:
        return True
    text = (op.name + " " + op.description).lower()
    return any(p.lower() in text for p in _RISKY_PATTERNS)

def _confirm(op: Operation) -> bool:
    print(f"\n⚠️  Risky operation: {op.name}")
    print(f"   {op.description}")
    try:
        answer = input("   Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer == "y"


# ── failure recovery ──────────────────────────────────────────────────────────

def _kiro_autofix(op: Operation, error: str, output: str) -> bool:
    """
    Ask Kiro CLI to diagnose and fix the failure, then return True so the
    executor retries the op. Returns False if Kiro is unavailable.
    """
    kiro = shutil.which("kiro-cli")
    if not kiro:
        print("   kiro-cli not found — cannot auto-fix.")
        return False

    mend_dir = os.path.dirname(os.path.abspath(__file__))
    prompt = (
        f"You are assisting with the 'mend' system recovery tool at {mend_dir}. "
        f"Operation '{op.name}' ({op.description}) failed with this error:\n\n"
        f"{error}\n\nCaptured output:\n{output or '(none)'}\n\n"
        f"Diagnose the problem and fix it using available tools. "
        f"Be concise. After fixing, confirm what you did in plain English."
    )
    print("\n🤖 Kiro is analysing and attempting a fix...\n")
    result = subprocess.run(
        [kiro, "chat", "--trust-all-tools", "--no-interactive", prompt],
        cwd=mend_dir,
    )
    return result.returncode == 0


def _pause_on_failure(op: Operation, error: str, output: str, state: StateManager) -> str:
    """
    Enforce failure handling rules by failure_class.
    Uses curses failure_menu when interactive, plain text fallback otherwise.
    Returns: 'retry' | 'skip' | 'abort'
    """
    fc = op.failure_class
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()

    if is_tty:
        choice = failure_menu(op.name, fc, error, output)
    else:
        # non-interactive fallback (pipe / CI)
        print(f"\n❌ [{fc}] {op.name}: {error}")
        choice = "abort"

    if choice == "autofix":
        fixed = _kiro_autofix(op, error, output)
        if fixed:
            return "retry"
        # autofix failed — for system class still no skip
        if fc == "system":
            return _pause_on_failure(op, error + " [autofix failed]", output, state)
        # fall through to another menu pass
        return _pause_on_failure(op, error + " [autofix failed]", output, state)

    elif choice == "manual":
        print("\n🔧 Dropping to shell. Fix the issue, then type 'exit' to retry.\n")
        os.system(os.environ.get("SHELL", "/bin/bash"))
        return "retry"

    elif choice == "skip":
        if fc == "system":
            # system failures cannot be skipped — loop back
            return _pause_on_failure(op, error, output, state)
        return "skip"

    else:  # abort
        state._state["status"] = "interrupted"
        state.save()
        return "abort"


class Executor:
    def __init__(self, state: StateManager, on_event=None, dry_run: bool = False):
        self.state = state
        self.on_event = on_event or (lambda ev, op, msg: None)
        self.dry_run = dry_run
        self._fatal = threading.Event()

    def _emit(self, event: str, op_name: str, msg: str = ""):
        self.on_event(event, op_name, msg)

    def _check_all(self, checks, env) -> tuple[bool, str]:
        for check in checks:
            ok, reason = check(env)
            if not ok:
                return False, reason
        return True, ""

    def _backoff(self, attempt: int):
        time.sleep(min(2 ** attempt, 16))

    def _run_one(self, op: Operation, env: dict):
        s = self.state

        if self._fatal.is_set() or s.should_stop:
            return
        if op.name not in s.pending:
            return

        # ── preconditions ─────────────────────────────────────────────────────
        ok, reason = self._check_all(op.preconditions, env)
        if not ok:
            if op.failure_class in ("system", "fatal"):
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

        # ── authorization gate ────────────────────────────────────────────────
        if _is_risky(op) and not _confirm(op):
            self._emit("skip", op.name, "declined by user")
            s.mark_skipped(op.name, "declined by user")
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
                fresh = snapshot()
                ok, reason = self._check_all(op.postconditions, fresh)
                if ok:
                    # ── deep validation (AI-driven) ───────────────────────────
                    if op.deep_validate:
                        dv_ok, dv_reason, verdict = op.deep_validate(fresh, output)
                        if not dv_ok:
                            self._emit("retry", op.name, f"deep validation: {dv_reason} [{verdict}]")
                            last_err = f"deep validation failed ({verdict}): {dv_reason}"
                            # map verdict to a retry signal; reinstall/reapply
                            # are handled the same as retry here — the AI already
                            # applied fixes during validation before returning
                            s.increment_retry(op.name)
                            attempt += 1
                            self._backoff(attempt)
                            continue
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
            return

        # ── failure: pause and offer recovery options ─────────────────────────
        is_interactive = sys.stdin.isatty()

        if op.failure_class in ("system", "fatal") and not is_interactive:
            self._emit("fatal", op.name, last_err)
            s.mark_failed(op.name, last_err, output)
            s.fail_task(f"fatal failure at {op.name}")
            self._fatal.set()
            return

        decision = _pause_on_failure(op, last_err, output, s)

        if decision == "retry":
            # reset retry counter and re-queue at front for a fresh attempt
            s._state["retries"][op.name] = 0
            if op.name not in s._state["pending"]:
                s._state["pending"].insert(0, op.name)
            s.save()
            self._run_one(op, snapshot())
        elif decision == "skip":
            self._emit("skip", op.name, "skipped after failure")
            s.mark_skipped(op.name, f"skipped after failure: {last_err}")
        else:  # abort
            self._emit("fatal", op.name, "aborted by user")
            s.mark_failed(op.name, last_err, output)
            self._fatal.set()

    def run(self, ops: List[Operation]) -> bool:
        s = self.state
        self._fatal.clear()

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
