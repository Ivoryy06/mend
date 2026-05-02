"""
Operation registry.

Each Operation defines:
  preconditions : list[Check]   — must pass before action runs
  action        : Action        — idempotent, receives env + captures output
  postconditions: list[Check]   — must pass after action runs
  failure_class : 'fatal' | 'recoverable'
  tags          : list[str]     — for filtering (e.g. 'network', 'disk', 'pkg')
  timeout       : int | None    — seconds before action is killed
  parallel      : bool          — safe to run concurrently with other parallel ops
"""

from dataclasses import dataclass, field
from typing import Callable, List, Tuple, Optional
import subprocess, os, shutil, threading, time

Env = dict
Check = Callable[[Env], Tuple[bool, str]]
Action = Callable[[Env], str]   




DeepValidate = Callable[[Env, str], Tuple[bool, str, str]]


@dataclass
class Operation:
    name: str
    description: str
    preconditions: List[Check]
    action: Action
    postconditions: List[Check]
    failure_class: str = "recoverable"   
    max_retries: int = 2
    tags: List[str] = field(default_factory=list)
    timeout: Optional[int] = None        
    parallel: bool = False               
    deep_validate: Optional[DeepValidate] = None  




def _run(args: list, timeout: int = None) -> tuple[bool, str]:
    """Run a command, return (success, combined_output)."""
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except FileNotFoundError:
        return False, f"command not found: {args[0]}"


def _has_cmd(name: str) -> Check:
    return lambda env: (shutil.which(name) is not None, f"command '{name}' not found")

def _file_exists(path: str) -> Check:
    return lambda env: (os.path.exists(path), f"{path} missing")

def _partition_mounted(mp: str) -> Check:
    return lambda env: (mp in env.get("mounts", []), f"{mp} not mounted")

def _pkg_installed(pkg: str) -> Check:
    return lambda env: (pkg in env.get("packages", []), f"package '{pkg}' not installed")

def _service_active(svc: str) -> Check:
    return lambda env: (
        env.get("services", {}).get(svc) == "active",
        f"service '{svc}' not active"
    )




def make_mount_op(name: str, device: str, mountpoint: str, fatal=True) -> Operation:
    def action(env) -> str:
        os.makedirs(mountpoint, exist_ok=True)
        ok, out = _run(["mount", device, mountpoint])
        if not ok:
            raise RuntimeError(f"mount failed: {out}")
        return out

    return Operation(
        name=name,
        description=f"Mount {device} at {mountpoint}",
        preconditions=[lambda env: (os.path.exists(device), f"device {device} not found")],
        action=action,
        postconditions=[lambda env: (os.path.ismount(mountpoint), f"{mountpoint} not a mountpoint")],
        failure_class="system" if fatal else "recoverable",
        max_retries=1,
        tags=["disk"],
    )


def make_pkg_op(name: str, packages: list, manager="apt") -> Operation:
    cmds = {
        "apt":    ["apt-get", "install", "-y"],
        "pacman": ["pacman", "-S", "--noconfirm"],
        "dnf":    ["dnf", "install", "-y"],
    }

    def action(env) -> str:
        ok, out = _run(cmds[manager] + packages)
        if not ok:
            raise RuntimeError(f"install failed: {out}")
        return out

    return Operation(
        name=name,
        description=f"Install: {', '.join(packages)}",
        preconditions=[_has_cmd(manager)],
        action=action,
        postconditions=[_pkg_installed(packages[0])],
        failure_class="recoverable",
        max_retries=2,
        tags=["pkg"],
    )


def make_copy_op(name: str, src: str, dst: str, fatal=False) -> Operation:
    def action(env) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return f"copied {src} -> {dst}"

    return Operation(
        name=name,
        description=f"Copy {src} → {dst}",
        preconditions=[_file_exists(src)],
        action=action,
        postconditions=[_file_exists(dst)],
        failure_class="system" if fatal else "recoverable",
        tags=["files"],
    )


def make_shell_op(name: str, description: str, cmd: list,
                  pre: List[Check] = None, post: List[Check] = None,
                  fatal=False, timeout: int = None,
                  tags: List[str] = None, parallel=False) -> Operation:
    def action(env) -> str:
        ok, out = _run(cmd, timeout=timeout)
        if not ok:
            raise RuntimeError(out or f"command failed: {' '.join(cmd)}")
        return out

    return Operation(
        name=name,
        description=description,
        preconditions=pre or [],
        action=action,
        postconditions=post or [],
        failure_class="system" if fatal else "recoverable",
        timeout=timeout,
        tags=tags or [],
        parallel=parallel,
    )


def make_health_op(name: str, description: str,
                   check_fn: Callable[[Env], Tuple[bool, str]],
                   tags: List[str] = None, fatal=False) -> Operation:
    """
    Pure health-check operation — no action, only a postcondition.
    Used for validation passes without side effects.
    """
    def action(env) -> str:
        return "health check (no action)"

    return Operation(
        name=name,
        description=description,
        preconditions=[],
        action=action,
        postconditions=[check_fn],
        failure_class="fatal" if fatal else "recoverable",
        max_retries=0,
        tags=(tags or []) + ["health"],
        parallel=True,
    )


def make_service_op(name: str, service: str, ensure: str = "active",
                    fatal=False) -> Operation:
    """Start/stop/restart a systemd service and verify its state."""
    verb = {"active": "start", "inactive": "stop"}.get(ensure, "restart")

    def action(env) -> str:
        ok, out = _run(["systemctl", verb, service])
        if not ok:
            raise RuntimeError(out)
        return out

    return Operation(
        name=name,
        description=f"Ensure {service} is {ensure}",
        preconditions=[_has_cmd("systemctl")],
        action=action,
        postconditions=[_service_active(service) if ensure == "active"
                        else lambda env: (True, "")],
        failure_class="system" if fatal else "recoverable",
        tags=["service"],
    )




def make_ai_validator(context: str) -> "DeepValidate":
    """
    Returns a deep_validate function that asks Kiro CLI to inspect the result
    of an operation and decide: done | retry | reapply | reinstall.

    context: plain-English description of what correctness looks like,
             e.g. "nginx config is valid and port 80 is listening"
    """
    import json as _json

    def validator(env: dict, op_output: str) -> tuple[bool, str, str]:
        kiro = shutil.which("kiro-cli")
        if not kiro:
            
            return True, "kiro-cli not available, skipping deep validation", "done"

        mend_dir = os.path.dirname(os.path.abspath(__file__))
        env_summary = {
            k: env[k] for k in
            ("distro", "kernel", "init", "pkg_manager", "internet",
             "disk", "memory", "services", "processes")
            if k in env
        }

        prompt = (
            "You are a system validation agent for the 'mend' recovery tool. "
            "An operation just completed. Inspect the system and decide if it succeeded correctly.\n\n"
            f"What correctness means for this operation:\n{context}\n\n"
            f"Operation output:\n{op_output or '(none)'}\n\n"
            f"Current environment snapshot:\n{_json.dumps(env_summary, indent=2)}\n\n"
            "Use available tools to inspect files, run commands, and check behavior. "
            "Then respond with ONLY a JSON object on a single line, like:\n"
            '{"ok": true, "reason": "nginx is running and port 80 responds", "verdict": "done"}\n\n'
            "verdict must be one of:\n"
            "  done      — everything is correct, continue\n"
            "  retry     — transient issue, retry the operation as-is\n"
            "  reapply   — config/state is wrong, reapply the operation with fixes\n"
            "  reinstall — package/binary is broken, remove and reinstall\n\n"
            "Respond with only the JSON line, nothing else."
        )

        result = subprocess.run(
            [kiro, "chat", "--trust-all-tools", "--no-interactive", prompt],
            capture_output=True, text=True, cwd=mend_dir,
        )

        
        for line in reversed(result.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = _json.loads(line)
                    ok      = bool(data.get("ok", True))
                    reason  = str(data.get("reason", ""))
                    verdict = str(data.get("verdict", "done"))
                    if verdict not in ("done", "retry", "reapply", "reinstall"):
                        verdict = "done" if ok else "retry"
                    return ok, reason, verdict
                except _json.JSONDecodeError:
                    continue

        
        return True, "deep validation response unparseable, assuming ok", "done"

    return validator




def make_app_test_op(
    name: str,
    cmd: list,
    *,
    run_for: float = 3.0,
    expect_in_output: list = None,
    expect_exit_zero: bool = False,
    install_cmd: list = None,
    uninstall_cmd: list = None,
    error_threshold: int = 3,
    tags: list = None,
) -> Operation:
    """
    Multi-pass test for an external application.

    Launches `cmd`, lets it run for `run_for` seconds, then inspects:
      - exit code (if expect_exit_zero)
      - stdout/stderr for expected strings (expect_in_output)
      - crash signals (returncode < 0)

    A persistent _error_counter tracks failures across retries.
    At error_threshold failures, Kiro is asked to diagnose; if it
    recommends reinstall, uninstall_cmd + install_cmd are run before
    the next attempt. After that, the counter resets.

    max_retries is set to error_threshold * 2 to allow retry + reinstall cycles.
    """
    _counter: dict = {"errors": 0, "reinstalled": False}

    def _detect_misbehavior(proc: subprocess.Popen, stdout: str, stderr: str) -> Optional[str]:
        combined = (stdout + stderr).strip()
        if proc.returncode is not None and proc.returncode < 0:
            return f"crashed with signal {-proc.returncode}"
        if expect_exit_zero and proc.returncode not in (None, 0):
            return f"exited with code {proc.returncode}"
        if expect_in_output:
            missing = [s for s in expect_in_output if s not in combined]
            if missing:
                return f"expected output missing: {missing}"
        
        for pattern in ("segfault", "core dumped", "killed", "error:", "fatal:", "exception"):
            if pattern in combined.lower():
                return f"misbehavior detected in output: '{pattern}'"
        return None

    def _reinstall(env: Env) -> str:
        logs = []
        if uninstall_cmd:
            ok, out = _run(uninstall_cmd, timeout=60)
            logs.append(f"uninstall: {out}")
        if install_cmd:
            ok, out = _run(install_cmd, timeout=120)
            if not ok:
                raise RuntimeError(f"reinstall failed: {out}")
            logs.append(f"install: {out}")
        return "\n".join(logs)

    def _kiro_diagnose(error: str, stdout: str, stderr: str) -> str:
        """Ask Kiro to diagnose and return 'reinstall' or 'retry'."""
        kiro = shutil.which("kiro-cli")
        if not kiro:
            return "retry"
        mend_dir = os.path.dirname(os.path.abspath(__file__))
        prompt = (
            f"Application test failed for command: {' '.join(cmd)}\n"
            f"Error: {error}\n"
            f"stdout: {stdout[-1000:]}\nstderr: {stderr[-1000:]}\n\n"
            "Diagnose the failure. Respond with ONLY one word: reinstall or retry."
        )
        r = subprocess.run(
            [kiro, "chat", "--trust-all-tools", "--no-interactive", prompt],
            capture_output=True, text=True, cwd=mend_dir,
        )
        out = r.stdout.strip().lower()
        return "reinstall" if "reinstall" in out else "retry"

    def action(env: Env) -> str:
        binary = cmd[0]
        if not shutil.which(binary):
            raise RuntimeError(f"binary not found: {binary}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            stdout, stderr = proc.communicate(timeout=run_for)
        except subprocess.TimeoutExpired:
            
            proc.kill()
            stdout, stderr = proc.communicate()
            proc.returncode = 0  

        error = _detect_misbehavior(proc, stdout, stderr)

        if error:
            _counter["errors"] += 1
            if _counter["errors"] >= error_threshold:
                verdict = _kiro_diagnose(error, stdout, stderr)
                if verdict == "reinstall" and (install_cmd or uninstall_cmd):
                    reinstall_out = _reinstall(env)
                    _counter["errors"] = 0
                    _counter["reinstalled"] = True
                    raise RuntimeError(
                        f"reinstalled after {error_threshold} failures ({error})\n{reinstall_out}"
                    )
            raise RuntimeError(
                f"[pass {_counter['errors']}/{error_threshold}] {error}\n"
                f"stdout: {stdout[-500:]}\nstderr: {stderr[-500:]}"
            )

        _counter["errors"] = 0  
        return (
            f"app test passed (errors reset to 0, reinstalled={_counter['reinstalled']})\n"
            f"stdout: {stdout[:500]}"
        )

    return Operation(
        name=name,
        description=f"Multi-pass test: {' '.join(cmd)}",
        preconditions=[lambda env, b=cmd[0]: (shutil.which(b) is not None, f"{b} not found")],
        action=action,
        postconditions=[],
        failure_class="external",
        max_retries=error_threshold * 2,
        tags=(tags or []) + ["app-test"],
    )




class Registry:
    def __init__(self):
        self._ops: dict[str, Operation] = {}

    def register(self, op: Operation):
        self._ops[op.name] = op

    def get(self, name: str) -> Operation:
        if name not in self._ops:
            raise KeyError(f"unknown operation: {name!r}")
        return self._ops[name]

    def all(self) -> List[Operation]:
        return list(self._ops.values())

    def names(self) -> List[str]:
        return list(self._ops.keys())

    def by_tag(self, tag: str) -> List[Operation]:
        return [op for op in self._ops.values() if tag in op.tags]
