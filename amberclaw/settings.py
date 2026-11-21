"""Read and safely update the local .env file from the dashboard.

Secrets are only ever written to .env (gitignored). API responses return masked
values, and raw keys are never logged.
"""
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_lock = threading.Lock()
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def env_path():
    """Resolve lazily so .env-loaded values (e.g. in Docker) are visible."""
    return Path(os.environ.get("AMBERCLAW_ENV_FILE") or (ROOT / ".env"))


def _split_keys(value):
    return [k.strip() for k in re.split(r"[\s,]+", str(value or "")) if k.strip()]


def read_raw():
    path = env_path()
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def write_values(values: dict):
    """Atomically update/add keys in .env and apply them to this process."""
    with _lock:
        path = env_path()
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["# AmberClaw configuration"]
        seen = set()
        out = []
        for line in lines:
            m = _LINE.match(line)
            if m and m.group(1) in values:
                key = m.group(1)
                out.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                out.append(line)
        for key, val in values.items():
            if key not in seen:
                out.append(f"{key}={val}")
        tmp = path.parent / ".env.tmp"
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        for key, val in values.items():
            os.environ[key] = val


def mask_key(key: str):
    key = str(key or "")
    if len(key) <= 10:
        return "****"
    return f"{key[:5]}...{key[-4:]}"


def groq_keys():
    raw = read_raw().get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY", "")
    keys = _split_keys(raw)
    return {"count": len(keys), "masked": [mask_key(k) for k in keys]}


def add_groq_keys(value: str):
    """Append new Groq keys, de-duplicated. Returns number of keys added."""
    raw = read_raw().get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY", "")
    existing = _split_keys(raw)
    merged = list(existing)
    for k in _split_keys(value):
        if k not in merged:
            merged.append(k)
    write_values({"GROQ_API_KEY": ",".join(merged)})
    return len(merged) - len(existing)


def remove_groq_keys(index: int):
    """Remove the key at a position. The UI never sends or receives raw keys."""
    raw = read_raw().get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY", "")
    keys = _split_keys(raw)
    if index < 0 or index >= len(keys):
        return 0
    del keys[index]
    write_values({"GROQ_API_KEY": ",".join(keys)})
    return 1


def view():
    raw = read_raw()
    def get(key, default=""):
        return str(os.environ.get(key) or raw.get(key) or default)
    return {
        "groq": groq_keys(),
        "daily_limit": get("YOUTUBE_DAILY_UPLOAD_LIMIT", "20"),
        "tick_minutes": get("UPLOAD_TICK_MINUTES", "10"),
        "dry_run": get("UPLOAD_DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"},
        "privacy": get("YOUTUBE_PRIVACY", "private"),
        "upload_mode": get("UPLOAD_MODE", "queue"),
        "brain": get("STREAMCLIPS_BRAIN", "groq"),
        "model": get("GROQ_MODEL", "openai/gpt-oss-20b"),
        "scheduler_enabled": get("UPLOAD_SCHEDULER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        "env_path": str(env_path()),
        "env_exists": env_path().exists(),
    }


def update_controls(*, daily_limit=None, tick_minutes=None, dry_run=None, privacy=None, upload_mode=None):
    values = {}
    if daily_limit is not None:
        values["YOUTUBE_DAILY_UPLOAD_LIMIT"] = str(max(0, min(1000, int(daily_limit))))
    if tick_minutes is not None:
        values["UPLOAD_TICK_MINUTES"] = str(max(1, min(1440, int(tick_minutes))))
    if dry_run is not None:
        values["UPLOAD_DRY_RUN"] = "true" if dry_run else "false"
    if privacy is not None:
        values["YOUTUBE_PRIVACY"] = privacy if privacy in {"public", "unlisted", "private"} else "private"
    if upload_mode is not None:
        values["UPLOAD_MODE"] = upload_mode if upload_mode in {"queue", "immediate"} else "queue"
    if values:
        write_values(values)
    return view()
