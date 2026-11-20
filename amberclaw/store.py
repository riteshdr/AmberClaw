import json
import os
import threading
import time
import uuid
from pathlib import Path

_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.json"
        if not self.path.exists():
            self._write({"jobs": []})

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"jobs": []}

    def _write(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def create(self, payload: dict) -> dict:
        job = {
            "id": uuid.uuid4().hex[:12],
            "status": "queued",
            "created_at": _now(),
            "updated_at": _now(),
            "payload": payload,
            "log": [],
            "result": None,
            "uploads": [],
            "error": None,
        }
        with _lock:
            data = self._read()
            data.setdefault("jobs", []).insert(0, job)
            self._write(data)
        return job

    def list(self, limit=50):
        with _lock:
            jobs = self._read().get("jobs", [])
        return jobs[:limit]

    def get(self, job_id: str):
        with _lock:
            for j in self._read().get("jobs", []):
                if j["id"] == job_id:
                    return j
        return None

    def update(self, job_id: str, **fields):
        with _lock:
            data = self._read()
            for j in data.get("jobs", []):
                if j["id"] == job_id:
                    j.update(fields)
                    j["updated_at"] = _now()
                    self._write(data)
                    return j
        return None

    def append_log(self, job_id: str, line: str, limit=400):
        line = (line or "").rstrip()
        if not line:
            return
        with _lock:
            data = self._read()
            for j in data.get("jobs", []):
                if j["id"] == job_id:
                    logs = j.setdefault("log", [])
                    logs.append(line)
                    if len(logs) > limit:
                        del logs[: len(logs) - limit]
                    j["updated_at"] = _now()
                    self._write(data)
                    return j
        return None

    def reconcile_interrupted(self, reason="Process interrupted (server restarted or worker stopped). No active heartbeat."):
        changed = 0
        with _lock:
            data = self._read()
            for j in data.get("jobs", []):
                if j.get("status") in {"queued", "running"}:
                    j["status"] = "failed"
                    j["error"] = (j.get("error") or reason)
                    logs = j.setdefault("log", [])
                    logs.append(f"[system] {reason}")
                    if len(logs) > 400:
                        del logs[: len(logs) - 400]
                    j["updated_at"] = _now()
                    changed += 1
            if changed:
                self._write(data)
        return changed
