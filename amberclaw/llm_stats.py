"""Cross-process, privacy-safe LLM usage statistics.

Both the dashboard process and the stream-clips worker append counters to one
small JSON file on the persistent data volume. Only counters, model names,
HTTP statuses, truncated error text and the last four characters of a key are
stored. Raw API keys never touch this file.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX: serialize writes from the web process and worker processes.
    import fcntl
except Exception:  # pragma: no cover - Windows fallback uses the thread lock only.
    fcntl = None

_LOCK = threading.Lock()

_DEFAULTS = {
    "calls": 0,
    "success": 0,
    "errors": 0,
    "rate_limits": 0,
    "last_success": None,
    "last_error": None,
    "last_error_at": None,
    "models": {},
    "keys": {},
    "kinds": {},
    "day": None,
    "today": {},
}

_ERROR_LIMIT = 320


def path():
    root = os.environ.get("AMBERCLAW_DATA_DIR") or str(Path(__file__).resolve().parent.parent / "data")
    return Path(root) / "llm_stats.json"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fresh_provider():
    out = dict(_DEFAULTS)
    out["models"] = {}
    out["keys"] = {}
    out["kinds"] = {}
    out["today"] = {}
    return out


def read():
    p = path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("providers", {})
    data["updated_at"] = data.get("updated_at")
    for name, entry in list(data["providers"].items()):
        if not isinstance(entry, dict):
            data["providers"][name] = _fresh_provider()
            continue
        for key, default in _DEFAULTS.items():
            entry.setdefault(key, dict(default) if isinstance(default, dict) else default)
    return data


def _write(p, data):
    tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _mutate(fn):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        if fcntl is not None:
            with open(str(p) + ".lock", "a+") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    data = read()
                    result = fn(data)
                    data["updated_at"] = _now()
                    _write(p, data)
                    return result
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        data = read()
        result = fn(data)
        data["updated_at"] = _now()
        _write(p, data)
        return result


def record(provider, *, model="", key="", ok=True, http_status=None, error="", kind="api"):
    """Record one LLM request attempt. Never pass a raw key to this function."""
    provider = str(provider or "unknown").strip().lower()
    status = None
    try:
        status = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        status = None

    def fn(data):
        entry = data["providers"].setdefault(provider, _fresh_provider())
        day = _day()
        if entry.get("day") != day:
            entry["day"] = day
            entry["today"] = {"calls": 0, "success": 0, "errors": 0, "rate_limits": 0}
        today = entry["today"]
        now = _now()

        entry["calls"] = int(entry.get("calls") or 0) + 1
        today["calls"] = int(today.get("calls") or 0) + 1
        if model:
            m = str(model)
            entry["models"][m] = int(entry["models"].get(m) or 0) + 1
        if key:
            # Summary only. Enough to see key rotation, never enough to recover a key.
            suffix = "..." + str(key)[-4:]
            entry["keys"][suffix] = int(entry["keys"].get(suffix) or 0) + 1
        if kind:
            k = str(kind)
            entry["kinds"][k] = int(entry["kinds"].get(k) or 0) + 1
        if status == 429:
            entry["rate_limits"] = int(entry.get("rate_limits") or 0) + 1
            today["rate_limits"] = int(today.get("rate_limits") or 0) + 1

        if ok:
            entry["success"] = int(entry.get("success") or 0) + 1
            today["success"] = int(today.get("success") or 0) + 1
            entry["last_success"] = now
        else:
            entry["errors"] = int(entry.get("errors") or 0) + 1
            today["errors"] = int(today.get("errors") or 0) + 1
            entry["last_error"] = str(error or "")[:_ERROR_LIMIT] or None
            entry["last_error_at"] = now

    return _mutate(fn)


def snapshot():
    return read()
