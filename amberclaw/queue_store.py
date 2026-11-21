import calendar
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# YouTube resets daily quota at midnight Pacific Time. zoneinfo needs tzdata on
# some Windows installs, so fall back to a fixed PST offset if it is missing.
try:
    from zoneinfo import ZoneInfo
    _QUOTA_TZ = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    _QUOTA_TZ = timezone(timedelta(hours=-8))

_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def next_quota_reset():
    """ISO timestamp of the next YouTube quota reset (midnight Pacific)."""
    now = datetime.now(_QUOTA_TZ)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.isoformat()


class UploadQueue:
    """Persistent queue of generated clips waiting to be uploaded to YouTube.

    Generation and uploading are decoupled: a job adds every finished clip to
    this queue, and UploadScheduler uploads a limited number per quota day.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"items": [], "state": {}})

    # ---------- storage ----------

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"items": [], "state": {}}

    def _write(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def _mutate(self, fn):
        with _lock:
            data = self._read()
            result = fn(data)
            self._write(data)
            return result

    def quota_day(self):
        return datetime.now(_QUOTA_TZ).strftime("%Y-%m-%d")

    # ---------- state ----------

    def state(self):
        with _lock:
            state = dict((self._read().get("state") or {}))
        if state.get("day") != self.quota_day():
            # New quota day: the counter is implicitly zero.
            state.update({"day": self.quota_day(), "uploaded": 0, "last_error": None})
        return state

    def uploaded_today(self):
        return int(self.state().get("uploaded") or 0)

    def is_paused(self):
        return bool(self.state().get("paused"))

    def set_paused(self, paused):
        def fn(data):
            st = data.setdefault("state", {})
            if st.get("day") != self.quota_day():
                st.update({"day": self.quota_day(), "uploaded": 0})
            st["paused"] = bool(paused)
            return st
        return self._mutate(fn)

    def touch_tick(self, error=None):
        def fn(data):
            st = data.setdefault("state", {})
            if st.get("day") != self.quota_day():
                st.update({"day": self.quota_day(), "uploaded": 0})
            st["last_tick"] = _now()
            st["last_error"] = str(error) if error else None
            return st
        return self._mutate(fn)

    def set_upload_cooldown(self, hours=24):
        """YouTube rejected uploads at the account level; back off for a rolling day."""
        def fn(data):
            st = data.setdefault("state", {})
            until = time.time() + max(1, int(hours)) * 3600
            st["cooldown_until"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
            return st
        return self._mutate(fn)

    def cooldown_remaining(self):
        """Seconds left on a YouTube-side upload cooldown (0 when none)."""
        st = self.state()
        until = st.get("cooldown_until")
        if not until:
            return 0
        try:
            t = calendar.timegm(time.strptime(str(until), "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            return 0
        return max(0, int(t - time.time()))

    # ---------- queue contents ----------

    def counts(self):
        with _lock:
            items = self._read().get("items", [])
        out = {"pending": 0, "uploading": 0, "uploaded": 0, "failed": 0,
               "removed": 0, "total": len(items)}
        for it in items:
            s = it.get("status")
            if s in out:
                out[s] += 1
        return out

    def add_clips(self, *, outdir, job_id="", topic="", job_log=None):
        """Enqueue every clip in a clips.json that is not already queued."""
        outdir = Path(outdir)
        clips_json = outdir / "clips.json"
        if not clips_json.exists():
            return []
        try:
            data = json.loads(clips_json.read_text(encoding="utf-8-sig"))
        except Exception:
            return []
        source = data.get("source") or {}
        creator = source.get("name") or "Unknown creator"
        src_title = source.get("title") or ""
        src_url = source.get("url") or ""
        added = []
        for c in data.get("clips") or []:
            file = c.get("file") or ""
            if not file or not (outdir / file).exists():
                continue
            item = {
                "id": uuid.uuid4().hex[:12],
                "status": "pending",
                "created_at": _now(),
                "updated_at": _now(),
                "job_id": job_id,
                "topic": topic,
                "outdir": str(outdir),
                "file": file,
                "title": c.get("title") or "",
                "caption": c.get("caption") or "",
                "hashtags": c.get("hashtags") or [],
                "creator": creator,
                "source_title": src_title,
                "source_url": src_url,
                "attempts": 0,
                "video_id": None,
                "url": None,
                "error": None,
            }

            def fn(d, item=item):
                for old in d.get("items", []):
                    if old.get("outdir") == item["outdir"] and old.get("file") == item["file"]:
                        return None
                d.setdefault("items", []).insert(0, item)
                return item

            res = self._mutate(fn)
            if res:
                added.append(res)
        if job_log:
            job_log(f"[queue] {len(added)} clip(s) added to the daily upload queue from {outdir}")
        return added

    def list_items(self, status=None, limit=100):
        with _lock:
            items = self._read().get("items", [])
        if status:
            items = [i for i in items if i.get("status") == status]
        return items[:limit]

    def claim(self, limit):
        """Atomically mark up to `limit` oldest pending items as uploading."""
        def fn(d):
            pending = [i for i in d.get("items", []) if i.get("status") == "pending"]
            pending.sort(key=lambda i: i.get("created_at") or "")
            chosen = pending[:limit]
            ids = {i["id"] for i in chosen}
            for it in d.get("items", []):
                if it.get("id") in ids:
                    it["status"] = "uploading"
                    it["updated_at"] = _now()
                    it["attempts"] = int(it.get("attempts") or 0) + 1
            return [dict(i) for i in chosen]
        return self._mutate(fn)

    def release(self, item_id):
        """Return a claimed item to pending without consuming quota (dry runs)."""
        def fn(d):
            for it in d.get("items", []):
                if it.get("id") == item_id:
                    it["status"] = "pending"
                    it["updated_at"] = _now()
                    return it
            return None
        return self._mutate(fn)

    def finish(self, item_id, *, video_id=None, error=None):
        status = "uploaded" if video_id and not error else "failed"
        def fn(d):
            for it in d.get("items", []):
                if it.get("id") != item_id:
                    continue
                previous = it.get("status")
                # A removal requested mid-upload wins for visibility, but a
                # video that really went live must still consume quota.
                if it.get("cancel_requested"):
                    status = "removed"
                it["status"] = status
                it["updated_at"] = _now()
                it["video_id"] = video_id
                it["url"] = f"https://youtu.be/{video_id}" if video_id else None
                it["uploaded_at"] = _now() if video_id else None
                it["error"] = str(error) if error else None
                it.pop("cancel_requested", None)
                if video_id and previous != "uploaded":
                    st = d.setdefault("state", {})
                    if st.get("day") != self.quota_day():
                        st.update({"day": self.quota_day(), "uploaded": 0})
                    st["uploaded"] = int(st.get("uploaded") or 0) + 1
                return it
            return None
        return self._mutate(fn)

    def reset_stale(self, max_age_seconds=3600):
        """Recover items left in 'uploading' by a crashed worker."""
        def fn(d):
            now = time.time()
            n = 0
            for it in d.get("items", []):
                if it.get("status") != "uploading":
                    continue
                try:
                    t = calendar.timegm(time.strptime(it.get("updated_at"), "%Y-%m-%dT%H:%M:%SZ"))
                except Exception:
                    t = 0
                if now - t > max_age_seconds:
                    it["status"] = "pending"
                    it["updated_at"] = _now()
                    n += 1
            return n
        return self._mutate(fn)

    def set_metadata(self, item_id, *, title, description):
        """Cache generated Hinglish metadata so retries don't regenerate it."""
        def fn(d):
            for it in d.get("items", []):
                if it.get("id") == item_id:
                    it["meta_title"] = title
                    it["meta_description"] = description
                    it["updated_at"] = _now()
                    return it
            return None
        return self._mutate(fn)

    def retry(self, item_id):
        """Requeue a failed/removed item. Never silently re-uploads a live video."""
        def fn(d):
            for it in d.get("items", []):
                if it.get("id") != item_id:
                    continue
                if it.get("status") not in {"failed", "removed"}:
                    return None
                # If a previously uploaded item was removed, undo its quota charge.
                if it.get("video_id") and it.get("uploaded_at"):
                    st = d.setdefault("state", {})
                    if st.get("day") == str(it["uploaded_at"])[:10]:
                        st["uploaded"] = max(0, int(st.get("uploaded") or 0) - 1)
                it["status"] = "pending"
                it["updated_at"] = _now()
                it["video_id"] = None
                it["url"] = None
                it["uploaded_at"] = None
                it["error"] = None
                it.pop("cancel_requested", None)
                return it
            return None
        return self._mutate(fn)

    def retry_rate_limited(self):
        """Auto-requeue items rejected by YouTube's account limit once cooldown clears."""
        def fn(d):
            n = 0
            for it in d.get("items", []):
                if it.get("status") == "failed" and "uploadLimitExceeded" in str(it.get("error") or ""):
                    it["status"] = "pending"
                    it["updated_at"] = _now()
                    it["error"] = None
                    n += 1
            return n
        return self._mutate(fn)

    def is_cancel_requested(self, item_id):
        with _lock:
            for it in self._read().get("items", []):
                if it.get("id") == item_id:
                    return bool(it.get("cancel_requested"))
        return False

    def remove(self, item_id):
        """Hide an item without letting a re-scan add it back."""
        def fn(d):
            for it in d.get("items", []):
                if it.get("id") == item_id:
                    if it.get("status") == "uploading":
                        # The scheduler holds a local copy; flag the cancellation
                        # so finish() won't resurrect it as uploaded.
                        it["cancel_requested"] = True
                    else:
                        it["status"] = "removed"
                    it["updated_at"] = _now()
                    return it
            return None
        return self._mutate(fn)
