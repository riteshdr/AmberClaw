import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import metadata as meta
from .queue_store import UploadQueue, next_quota_reset
from .uploader import UploadError, build_upload_fields, upload_video

_DEFAULT_TEMPLATE = "Clipped from {creator}: {title}\nSource: {url}\n\n{caption}\n\n{hashtags}\n\nCredit: {creator} ({url})"


def _bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _age_seconds(iso):
    if not iso:
        return None
    try:
        t = datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        return max(0, int(time.time() - t))
    except Exception:  # noqa: BLE001
        return None


class UploadScheduler:
    """Uploads queued clips in small daily batches to respect YouTube quota."""

    def __init__(self, queue: UploadQueue, store=None, log=None):
        self.queue = queue
        self.store = store
        self.logs = []
        self._external_log = log
        self._stop = threading.Event()
        self._thread = None
        self.lock_path = Path(str(queue.path) + ".lock")

    # ---------- logging ----------

    def _log(self, msg):
        line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
        self.logs.insert(0, line)
        del self.logs[100:]
        if self._external_log:
            try:
                self._external_log(line)
            except Exception:
                pass

    # ---------- cross-process lock ----------

    def _acquire_lock(self):
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                # 24h: a slow resumable batch must never look "stale" mid-upload.
                if time.time() - self.lock_path.stat().st_mtime > 86400:
                    self.lock_path.unlink(missing_ok=True)
                    return self._acquire_lock()
            except Exception:
                pass
            return False
        except Exception:
            return False

    def _touch_lock(self):
        """Heartbeat so a long-running batch is never considered stale."""
        try:
            os.utime(self.lock_path, None)
        except Exception:
            pass

    def _release_lock(self):
        try:
            self.lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ---------- lifecycle ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                # Read the interval each cycle so UI control changes apply
                # without a container restart.
                try:
                    minutes = max(1, int(os.environ.get("UPLOAD_TICK_MINUTES", "10")))
                except (TypeError, ValueError):
                    minutes = 10
                self._stop.wait(minutes * 60)
                if self._stop.is_set():
                    break
                try:
                    self.run_once()
                except Exception as e:  # noqa: BLE001
                    self._log(f"[scheduler] tick error: {e}")

        self._thread = threading.Thread(target=loop, name="upload-scheduler", daemon=True)
        self._thread.start()
        self._log("[scheduler] started")
        return self._thread

    def stop(self):
        self._stop.set()

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def status(self):
        st = self.queue.state()
        limit = max(0, int(os.environ.get("YOUTUBE_DAILY_UPLOAD_LIMIT", "20")))
        used = int(st.get("uploaded") or 0)
        counts = self.queue.counts()
        failed = self.queue.list_items(status="failed", limit=8)
        uploading = self.queue.list_items(status="uploading", limit=8)
        return {
            "alive": self.is_alive(),
            "paused": bool(st.get("paused")),
            "quota_day": st.get("day"),
            "uploaded_today": used,
            "daily_limit": limit,
            "remaining": max(0, limit - used),
            "quota_reset_at": next_quota_reset(),
            "quota_timezone": "America/Los_Angeles (midnight Pacific)",
            "tick_minutes": max(1, int(os.environ.get("UPLOAD_TICK_MINUTES", "10"))),
            "last_tick": st.get("last_tick"),
            "last_tick_age_seconds": _age_seconds(st.get("last_tick")),
            "last_error": st.get("last_error"),
            "cooldown_until": st.get("cooldown_until"),
            "cooldown_remaining": self.queue.cooldown_remaining(),
            "counts": counts,
            "failures": [{
                "id": i.get("id"), "file": i.get("file"), "error": i.get("error"),
                "updated_at": i.get("updated_at"), "attempts": i.get("attempts"),
            } for i in failed],
            "uploading": [{
                "id": i.get("id"), "file": i.get("file"), "updated_at": i.get("updated_at"),
                "age_seconds": _age_seconds(i.get("updated_at")),
            } for i in uploading],
            "logs": self.logs[:20],
        }

    # ---------- the batch ----------

    def run_once(self, force=False):
        if not self._acquire_lock():
            self._log("[scheduler] another batch is already running; skipping this tick")
            return {"ran": False, "reason": "locked"}

        result = {"ran": True, "uploaded": 0, "failed": 0, "skipped": 0}
        try:
            recovered = self.queue.reset_stale()
            if recovered:
                self._log(f"[scheduler] recovered {recovered} interrupted upload(s)")

            cooldown = self.queue.cooldown_remaining()
            if cooldown > 0:
                self.queue.touch_tick()
                self._log(f"[scheduler] YouTube account limit active; cooldown {cooldown // 3600}h {(cooldown % 3600) // 60}m left")
                result["reason"] = "youtube_cooldown"
                return result

            if self.queue.is_paused() and not force:
                self.queue.touch_tick()
                self._log("[scheduler] paused; no uploads this tick")
                result["reason"] = "paused"
                return result

            limit = max(0, int(os.environ.get("YOUTUBE_DAILY_UPLOAD_LIMIT", "20")))
            remaining = limit - self.queue.uploaded_today()
            if remaining <= 0:
                self.queue.touch_tick()
                self._log(f"[scheduler] daily limit reached ({limit}); quota resets at midnight Pacific Time")
                result["reason"] = "limit_reached"
                return result

            items = self.queue.claim(remaining)
            if not items:
                self.queue.touch_tick()
                result["reason"] = "empty"
                return result

            privacy = os.environ.get("YOUTUBE_PRIVACY", "private")
            category = os.environ.get("YOUTUBE_CATEGORY_ID", "20")
            client_secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secrets.json")
            token_file = os.environ.get("YOUTUBE_TOKEN_FILE", "token.json")
            desc_tmpl = os.environ.get("YOUTUBE_DESCRIPTION_TEMPLATE", _DEFAULT_TEMPLATE)
            default_tags = [t.strip() for t in
                            os.environ.get("YOUTUBE_TAGS_DEFAULT", "shorts,clips").split(",") if t.strip()]
            dry_run = _bool(os.environ.get("UPLOAD_DRY_RUN"), False)

            self._log(f"[scheduler] processing {len(items)} clip(s) (dry_run={dry_run})")
            for it in items:
                if self.queue.is_cancel_requested(it["id"]):
                    self.queue.finish(it["id"])  # honours cancel flag -> removed
                    self._log(f"[scheduler] skipped {it.get('file')}: removed by user")
                    result["skipped"] += 1
                    continue
                vpath = Path(it["outdir"]) / it["file"]
                try:
                    if not vpath.exists():
                        raise UploadError(f"Video not found: {vpath}")
                    hinglish_title = it.get("meta_title") or ""
                    hinglish_desc = it.get("meta_description") or ""
                    if not hinglish_title or not hinglish_desc:
                        hinglish_title, hinglish_desc = meta.generate(
                            caption=it.get("caption") or "",
                            source_title=it.get("source_title") or "",
                            creator=it.get("creator") or "",
                            topic=it.get("topic") or it.get("title") or "",
                        )
                        self.queue.set_metadata(
                            it["id"], title=hinglish_title, description=hinglish_desc)
                    title, desc, tags = build_upload_fields(
                        caption=hinglish_desc,
                        hashtags=it.get("hashtags") or [],
                        creator=it.get("creator") or "Unknown creator",
                        source_title=it.get("source_title") or "",
                        source_url=it.get("source_url") or "",
                        title=hinglish_title,
                        default_tags=default_tags,
                        description_template=desc_tmpl,
                    )
                    if dry_run:
                        self._log(f"[scheduler][dry-run] would upload {vpath.name} -> {title}")
                        self.queue.release(it["id"])
                        result["skipped"] += 1
                        continue

                    self._touch_lock()  # heartbeat for long resumable uploads
                    resp = upload_video(
                        vpath, title=title, description=desc, tags=tags,
                        privacy=privacy, category_id=category,
                        client_secrets=client_secrets, token_file=token_file,
                    )
                    vid = resp.get("id") if isinstance(resp, dict) else None
                    if not vid:
                        raise UploadError("YouTube did not return a video id")
                    self.queue.finish(it["id"], video_id=vid)
                    result["uploaded"] += 1
                    line = f"[scheduler] uploaded {vpath.name} -> https://youtu.be/{vid}"
                    self._log(line)
                    if self.store and it.get("job_id"):
                        self.store.append_log(it["job_id"], line)
                except Exception as e:  # noqa: BLE001
                    self.queue.finish(it["id"], error=e)
                    result["failed"] += 1
                    line = f"[scheduler] upload failed for {it.get('file')}: {e}"
                    self._log(line)
                    if "uploadLimitExceeded" in str(e):
                        try:
                            cooldown_h = max(1, int(os.environ.get("YOUTUBE_LIMIT_COOLDOWN_HOURS", "6")))
                        except (TypeError, ValueError):
                            cooldown_h = 6
                        self.queue.set_upload_cooldown(cooldown_h)
                        self._log(f"[scheduler] YouTube account upload limit hit; backing off for {cooldown_h}h. "
                                  "Verify the channel's phone number to raise the real limit.")
                    if self.store and it.get("job_id"):
                        self.store.append_log(it["job_id"], line)

            self.queue.touch_tick()
        except Exception as e:  # noqa: BLE001
            self.queue.touch_tick(error=e)
            result["error"] = str(e)
            self._log(f"[scheduler] tick failed: {e}")
            return result
        finally:
            self._release_lock()
        return result
