"""
Environment inspector — full system snapshot for pre/postcondition evaluation.
"""

import os, subprocess, shutil, platform, hashlib, socket
from typing import Optional


def _run(args) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ── mounts & partitions ───────────────────────────────────────────────────────

def _mounts() -> list:
    out = _run(["findmnt", "-rn", "-o", "TARGET"])
    return out.splitlines() if out else []


def _partitions() -> list:
    out = _run(["lsblk", "-rn", "-o", "NAME,SIZE,TYPE,MOUNTPOINT"])
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append({
                "name": parts[0], "size": parts[1], "type": parts[2],
                "mountpoint": parts[3] if len(parts) > 3 else "",
            })
    return rows


def _disk_usage(path: str = "/") -> dict:
    try:
        s = shutil.disk_usage(path)
        return {"total": s.total, "used": s.used, "free": s.free}
    except Exception:
        return {}


# ── packages ──────────────────────────────────────────────────────────────────

def _detect_pkg_manager() -> str:
    for m in ("pacman", "apt", "dnf"):
        if shutil.which(m):
            return m
    return "unknown"


def _installed_packages(manager: str) -> list:
    cmds = {
        "apt":    ["dpkg", "--get-selections"],
        "pacman": ["pacman", "-Qq"],
        "dnf":    ["rpm", "-qa", "--qf", "%{NAME}\n"],
    }
    if manager not in cmds:
        return []
    out = _run(cmds[manager])
    return [l.split()[0] for l in out.splitlines() if l]


# ── services ──────────────────────────────────────────────────────────────────

def _service_status(services: list) -> dict:
    """Return {service: 'active'|'inactive'|'failed'|'unknown'} for each."""
    if not shutil.which("systemctl"):
        return {s: "unknown" for s in services}
    result = {}
    for svc in services:
        out = _run(["systemctl", "is-active", svc])
        result[svc] = out if out in ("active", "inactive", "failed") else "unknown"
    return result


# ── processes ─────────────────────────────────────────────────────────────────

def _process_list() -> list:
    """Return list of running process names (deduplicated)."""
    out = _run(["ps", "-eo", "comm="])
    return list(dict.fromkeys(l.strip() for l in out.splitlines() if l.strip()))


def _process_running(name: str) -> bool:
    return name in _process_list()


# ── config hashing ────────────────────────────────────────────────────────────

def hash_file(path: str) -> Optional[str]:
    """SHA256 of a file, or None if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def hash_configs(paths: list) -> dict:
    """Return {path: sha256} for each path."""
    return {p: hash_file(p) for p in paths}


# ── network ───────────────────────────────────────────────────────────────────

def _network_interfaces() -> list:
    out = _run(["ip", "-br", "addr"])
    ifaces = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            ifaces.append({"name": parts[0], "state": parts[1] if len(parts) > 1 else "?",
                           "addr": parts[2] if len(parts) > 2 else ""})
    return ifaces


def _has_internet(host="8.8.8.8", port=53, timeout=2) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except Exception:
        return False


# ── main snapshot ─────────────────────────────────────────────────────────────

def snapshot(services: list = None) -> dict:
    pkg_mgr = _detect_pkg_manager()
    return {
        "os":         platform.system(),
        "distro":     _run(["lsb_release", "-ds"]) or platform.version(),
        "hostname":   platform.node(),
        "arch":       platform.machine(),
        "mounts":     _mounts(),
        "partitions": _partitions(),
        "disk":       _disk_usage(),
        "pkg_manager": pkg_mgr,
        "packages":   _installed_packages(pkg_mgr),
        "services":   _service_status(services or []),
        "processes":  _process_list(),
        "network":    _network_interfaces(),
        "internet":   _has_internet(),
        "env":        dict(os.environ),
    }


def check_file(path: str) -> dict:
    exists = os.path.exists(path)
    return {
        "path":     path,
        "exists":   exists,
        "is_dir":   os.path.isdir(path) if exists else False,
        "size":     os.path.getsize(path) if exists and os.path.isfile(path) else None,
        "readable": os.access(path, os.R_OK) if exists else False,
        "writable": os.access(path, os.W_OK) if exists else False,
        "hash":     hash_file(path) if exists and os.path.isfile(path) else None,
    }
