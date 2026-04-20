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
Action = Callable[[Env], str]   # returns captured output string


@dataclass
class Operation:
    name: str
    description: str
    preconditions: List[Check]
    action: Action
    postconditions: List[Check]
    failure_class: str = "recoverable"   # 'fatal' | 'recoverable'
    max_retries: int = 2
    tags: List[str] = field(default_factory=list)
    timeout: Optional[int] = None        # seconds; None = no limit
    parallel: bool = False               # safe for concurrent execution


# ── subprocess helpers ────────────────────────────────────────────────────────

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


# ── factory functions ─────────────────────────────────────────────────────────

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
        failure_class="fatal" if fatal else "recoverable",
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
        failure_class="fatal" if fatal else "recoverable",
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
        failure_class="fatal" if fatal else "recoverable",
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
        failure_class="fatal" if fatal else "recoverable",
        tags=["service"],
    )


# ── registry ──────────────────────────────────────────────────────────────────

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
