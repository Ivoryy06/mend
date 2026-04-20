#!/usr/bin/env python3
"""
gen.py — generate web/data.json from mend state files.
Run from anywhere; paths are resolved relative to this file.
"""
import json, os, time, subprocess, shutil

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME     = os.path.expanduser("~")
BACKUP   = os.path.join(HOME, "backup")
SNAPS    = os.path.join(BACKUP, "snapshots")
OUT      = os.path.join(ROOT, "web", "data.json")

def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def _du(path):
    try:
        return subprocess.check_output(["du", "-sh", path], stderr=subprocess.DEVNULL,
                                       text=True).split()[0]
    except Exception:
        return None

def _timer_active():
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "mend-backup.timer"],
                           capture_output=True, text=True)
        return r.stdout.strip() == "active"
    except Exception:
        return False

def _gh_last_push():
    """Last commit date of the mend-system-backup repo if it exists locally."""
    repo = os.path.join(HOME, "mend-system-backup")
    if not os.path.isdir(os.path.join(repo, ".git")):
        return None
    try:
        return subprocess.check_output(
            ["git", "-C", repo, "log", "-1", "--format=%ci"],
            stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return None

def _packages():
    try:
        out = subprocess.check_output(["pacman", "-Q"], stderr=subprocess.DEVNULL, text=True)
        pkgs = [l.split() for l in out.strip().splitlines() if l]
        return {"count": len(pkgs), "sample": [p[0] for p in pkgs[:20]]}
    except Exception:
        pass
    try:
        out = subprocess.check_output(["dpkg", "--get-selections"], stderr=subprocess.DEVNULL, text=True)
        pkgs = [l.split()[0] for l in out.strip().splitlines() if "install" in l]
        return {"count": len(pkgs), "sample": pkgs[:20]}
    except Exception:
        return {"count": 0, "sample": []}

# ── assemble ──────────────────────────────────────────────────────────────────

state   = _load(os.path.join(ROOT, "state.json"))
wstate  = _load(os.path.join(ROOT, "watchdog.json"))

# last backup
lkg = state.get("last_known_good")
last_backup = {
    "ts":       lkg["ts"]   if lkg else None,
    "task":     lkg["task"] if lkg else None,
    "snapshot": lkg["snapshot"] if lkg else None,
    "size":     _du(lkg["snapshot"]) if lkg and lkg.get("snapshot") else None,
    "backup_dir_size": _du(BACKUP),
    "timer_active": _timer_active(),
}

# sync health
snapshots = []
if os.path.isdir(SNAPS):
    for name in sorted(os.listdir(SNAPS), reverse=True)[:10]:
        p = os.path.join(SNAPS, name)
        if os.path.isdir(p):
            snapshots.append({"name": name, "size": shutil.disk_usage(p).used})

sync = {
    "github_last_push": _gh_last_push(),
    "local_snapshots":  len(snapshots),
    "snapshots":        snapshots,
    "external_targets": [],   # populated at runtime by state.external_targets
}

# system health
results     = wstate.get("results", [])
corrections = wstate.get("corrections", [])
health = {
    "last_run":   wstate.get("last_run"),
    "cycles":     wstate.get("cycles", 0),
    "checks":     results,
    "passed":     sum(1 for r in results if r.get("ok")),
    "failed":     sum(1 for r in results if not r.get("ok")),
}

# recent errors / fixes from run history + corrections
history = state.get("history", [])[-10:]
errors  = [e for e in state.get("log", []) if e.get("status") == "failed"][-20:]

data = {
    "generated_at": time.time(),
    "last_backup":  last_backup,
    "sync":         sync,
    "packages":     _packages(),
    "health":       health,
    "corrections":  corrections[-20:],
    "errors":       errors,
    "history":      history,
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(data, f, indent=2)

print(f"wrote {OUT}")
