"""
Environment inspector — deep system snapshot for pre/postcondition evaluation.
Covers: OS, kernel, hardware, CPU, memory, swap, disk, partitions, network,
        services, processes, packages, environment, locale, shell, display,
        GPU, temperatures, battery, init system, uptime, users.
"""

import os, subprocess, shutil, platform, hashlib, socket, re
from typing import Optional


def _run(args, timeout=5) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


# ── OS / kernel ───────────────────────────────────────────────────────────────

def _os_info() -> dict:
    info = {
        "os":       platform.system(),
        "hostname": platform.node(),
        "arch":     platform.machine(),
        "kernel":   platform.release(),
        "kernel_full": _run(["uname", "-r"]),
        "distro":   _run(["lsb_release", "-ds"]) or _read("/etc/os-release").split("\n")[0],
        "uptime":   _run(["uptime", "-p"]),
        "init":     _detect_init(),
        "shell":    os.environ.get("SHELL", ""),
        "locale":   _run(["locale"]),
        "timezone": _run(["timedatectl", "show", "--property=Timezone", "--value"])
                    or _read("/etc/timezone"),
        "users_logged_in": _run(["who"]),
    }
    # pretty distro from /etc/os-release
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            info["distro"] = line.split("=", 1)[1].strip('"')
            break
    return info


def _detect_init() -> str:
    if shutil.which("systemctl") and _run(["systemctl", "--version"]):
        return "systemd"
    if os.path.exists("/sbin/openrc"):
        return "openrc"
    if os.path.exists("/sbin/runit"):
        return "runit"
    return "unknown"


# ── CPU ───────────────────────────────────────────────────────────────────────

def _cpu_info() -> dict:
    info = {"model": "", "cores_physical": 0, "cores_logical": 0, "freq_mhz": 0, "load": []}
    cpuinfo = _read("/proc/cpuinfo")
    for line in cpuinfo.splitlines():
        if line.startswith("model name") and not info["model"]:
            info["model"] = line.split(":", 1)[1].strip()
        if line.startswith("processor"):
            info["cores_logical"] += 1
    # physical cores
    siblings = re.findall(r"^siblings\s+:\s+(\d+)", cpuinfo, re.M)
    cpu_cores = re.findall(r"^cpu cores\s+:\s+(\d+)", cpuinfo, re.M)
    if cpu_cores:
        info["cores_physical"] = int(cpu_cores[0])
    # current freq
    freq = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if freq:
        info["freq_mhz"] = round(int(freq) / 1000)
    # load average
    loadavg = _read("/proc/loadavg").split()
    if loadavg:
        info["load"] = [float(x) for x in loadavg[:3]]
    return info


# ── memory ────────────────────────────────────────────────────────────────────

def _mem_info() -> dict:
    info = {"total_mb": 0, "available_mb": 0, "used_mb": 0, "free_mb": 0,
            "swap_total_mb": 0, "swap_used_mb": 0, "swap_free_mb": 0}
    for line in _read("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        kb = int(parts[1]) if parts[1].isdigit() else 0
        key = parts[0].rstrip(":")
        if key == "MemTotal":       info["total_mb"]      = kb // 1024
        elif key == "MemAvailable": info["available_mb"]  = kb // 1024
        elif key == "MemFree":      info["free_mb"]       = kb // 1024
        elif key == "SwapTotal":    info["swap_total_mb"] = kb // 1024
        elif key == "SwapFree":     info["swap_free_mb"]  = kb // 1024
    info["used_mb"] = info["total_mb"] - info["available_mb"]
    info["swap_used_mb"] = info["swap_total_mb"] - info["swap_free_mb"]
    return info


# ── disk ──────────────────────────────────────────────────────────────────────

def _mounts() -> list:
    out = _run(["findmnt", "-rn", "-o", "TARGET"])
    return out.splitlines() if out else []


def _partitions() -> list:
    out = _run(["lsblk", "-rn", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,UUID,MODEL"])
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append({
                "name":       parts[0],
                "size":       parts[1],
                "type":       parts[2],
                "mountpoint": parts[3] if len(parts) > 3 else "",
                "fstype":     parts[4] if len(parts) > 4 else "",
                "uuid":       parts[5] if len(parts) > 5 else "",
                "model":      parts[6] if len(parts) > 6 else "",
            })
    return rows


def _disk_usage() -> list:
    """Per-mountpoint disk usage."""
    out = _run(["df", "-h", "--output=target,size,used,avail,pcent,fstype"])
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            rows.append({
                "mount": parts[0], "size": parts[1], "used": parts[2],
                "avail": parts[3], "use%": parts[4],
                "fstype": parts[5] if len(parts) > 5 else "",
            })
    return rows


# ── GPU ───────────────────────────────────────────────────────────────────────

def _gpu_info() -> list:
    gpus = []
    # lspci
    out = _run(["lspci"])
    for line in out.splitlines():
        if any(k in line.lower() for k in ("vga", "3d", "display", "gpu")):
            gpus.append({"name": line.split(":", 2)[-1].strip(), "driver": ""})
    # nvidia-smi
    nsmi = _run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,temperature.gpu",
                 "--format=csv,noheader"])
    if nsmi:
        for line in nsmi.splitlines():
            parts = [p.strip() for p in line.split(",")]
            gpus.append({"name": parts[0], "driver": parts[1] if len(parts) > 1 else "",
                         "vram": parts[2] if len(parts) > 2 else "",
                         "temp_c": parts[3] if len(parts) > 3 else ""})
    return gpus


# ── temperatures ──────────────────────────────────────────────────────────────

def _temperatures() -> dict:
    temps = {}
    # sensors (lm-sensors)
    out = _run(["sensors", "-j"], timeout=3)
    if out:
        try:
            import json
            data = json.loads(out)
            for chip, readings in data.items():
                for sensor, vals in readings.items():
                    for k, v in vals.items():
                        if "input" in k and isinstance(v, (int, float)):
                            temps[f"{chip}/{sensor}"] = v
        except Exception:
            pass
    # fallback: /sys/class/thermal
    thermal = "/sys/class/thermal"
    if os.path.isdir(thermal):
        for zone in os.listdir(thermal):
            temp_file = os.path.join(thermal, zone, "temp")
            type_file = os.path.join(thermal, zone, "type")
            val = _read(temp_file)
            label = _read(type_file) or zone
            if val.isdigit():
                temps[label] = round(int(val) / 1000, 1)
    return temps


# ── battery ───────────────────────────────────────────────────────────────────

def _battery() -> dict:
    ps = "/sys/class/power_supply"
    if not os.path.isdir(ps):
        return {}
    for name in os.listdir(ps):
        base = os.path.join(ps, name)
        btype = _read(os.path.join(base, "type"))
        if btype.lower() != "battery":
            continue
        return {
            "name":     name,
            "status":   _read(os.path.join(base, "status")),
            "capacity": _read(os.path.join(base, "capacity")),
            "health":   _read(os.path.join(base, "health")),
        }
    return {}


# ── display ───────────────────────────────────────────────────────────────────

def _display_info() -> dict:
    return {
        "display_server": os.environ.get("WAYLAND_DISPLAY") and "wayland"
                          or os.environ.get("DISPLAY") and "x11" or "none",
        "desktop":        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session_type":   os.environ.get("XDG_SESSION_TYPE", ""),
        "resolution":     (lambda p: p[1].split()[0] if len(p) > 1 and p[1].split() else "")(
                              _run(["xdpyinfo"]).split("dimensions:"))
                          if shutil.which("xdpyinfo") and os.environ.get("DISPLAY") else "",
    }


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


def _dns_servers() -> list:
    out = _read("/etc/resolv.conf")
    return [l.split()[1] for l in out.splitlines()
            if l.startswith("nameserver") and len(l.split()) >= 2]


# ── packages ──────────────────────────────────────────────────────────────────

def _detect_pkg_manager() -> str:
    for m in ("pacman", "apt", "dnf", "zypper", "apk"):
        if shutil.which(m):
            return m
    return "unknown"


def _installed_packages(manager: str) -> list:
    cmds = {
        "apt":    ["dpkg", "--get-selections"],
        "pacman": ["pacman", "-Qq"],
        "dnf":    ["rpm", "-qa", "--qf", "%{NAME}\n"],
        "zypper": ["rpm", "-qa", "--qf", "%{NAME}\n"],
        "apk":    ["apk", "list", "--installed"],
    }
    if manager not in cmds:
        return []
    out = _run(cmds[manager], timeout=10)
    return [l.split()[0] for l in out.splitlines() if l]


# ── services ──────────────────────────────────────────────────────────────────

def _service_status(services: list) -> dict:
    if not shutil.which("systemctl"):
        return {s: "unknown" for s in services}
    result = {}
    for svc in services:
        out = _run(["systemctl", "is-active", svc])
        result[svc] = out if out in ("active", "inactive", "failed") else "unknown"
    return result


# ── processes ─────────────────────────────────────────────────────────────────

def _process_list() -> list:
    out = _run(["ps", "-eo", "comm="])
    return list(dict.fromkeys(l.strip() for l in out.splitlines() if l.strip()))


# ── config hashing ────────────────────────────────────────────────────────────

def hash_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def hash_configs(paths: list) -> dict:
    return {p: hash_file(p) for p in paths}


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


# ── main snapshot ─────────────────────────────────────────────────────────────

def snapshot(services: list = None) -> dict:
    pkg_mgr = _detect_pkg_manager()
    return {
        **_os_info(),
        "cpu":        _cpu_info(),
        "memory":     _mem_info(),
        "disk":       _disk_usage(),
        "partitions": _partitions(),
        "mounts":     _mounts(),
        "gpu":        _gpu_info(),
        "temps":      _temperatures(),
        "battery":    _battery(),
        "display":    _display_info(),
        "network":    _network_interfaces(),
        "dns":        _dns_servers(),
        "internet":   _has_internet(),
        "pkg_manager": pkg_mgr,
        "packages":   _installed_packages(pkg_mgr),
        "services":   _service_status(services or []),
        "processes":  _process_list(),
        "env":        dict(os.environ),
    }
