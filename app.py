import calendar
import os
import platform
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from amberclaw import llm_stats
from amberclaw import presets as preset_lib
from amberclaw import settings as settings_lib
from amberclaw.queue_store import UploadQueue
from amberclaw.runner import start_job_async
from amberclaw.scheduler import UploadScheduler
from amberclaw.store import JobStore
from amberclaw.uploader import UploadError, upload_clips_from_dir

load_dotenv()
# In containers the dashboard-managed .env lives on the persistent volume.
# Load it last so keys added from the UI survive restarts.
_persist_env = os.environ.get("AMBERCLAW_ENV_FILE")
if _persist_env:
    load_dotenv(_persist_env, override=True)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AMBERCLAW_DATA_DIR", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
store = JobStore(DATA_DIR / "jobs")

_queue_path = os.environ.get("UPLOAD_QUEUE_PATH") or str(DATA_DIR / "jobs" / "upload_queue.json")
upload_queue = UploadQueue(_queue_path)
scheduler = UploadScheduler(upload_queue, store=store)

app = FastAPI(title="AmberClaw")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
security = HTTPBasic(auto_error=False)

static_dir = BASE_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
def _reconcile_on_startup():
    try:
        store.reconcile_interrupted()
    except Exception as e:  # noqa: BLE001
        scheduler._log(f"[startup] reconcile failed: {e}")
    if os.environ.get("UPLOAD_SCHEDULER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            scheduler.start()
        except Exception as e:  # noqa: BLE001
            scheduler._log(f"[startup] scheduler failed to start: {e}")


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    cfg = os.environ.get("AMBERCLAW_BASIC_AUTH", "").strip()
    if not cfg:
        return True
    if not credentials:
        raise HTTPException(status_code=401, detail="Auth required", headers={"WWW-Authenticate": "Basic"})
    allowed = [tuple(x.split(":", 1)) for x in cfg.split(",") if ":" in x]
    supplied_u = credentials.username.encode("utf-8")
    supplied_p = credentials.password.encode("utf-8")
    for user, pwd in allowed:
        if secrets.compare_digest(supplied_u, user.encode("utf-8")) and secrets.compare_digest(supplied_p, pwd.encode("utf-8")):
            return True
    raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


def _safe_data_path(p: str | Path) -> Path:
    try:
        target = Path(p).resolve()
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid path")
    if DATA_DIR in target.parents or target == DATA_DIR:
        return target
    raise HTTPException(status_code=403, detail="Access outside data directory is not allowed")


def _age_seconds(iso):
    if not iso:
        return None
    try:
        t = calendar.timegm(time.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ"))
        return max(0, int(time.time() - t))
    except Exception:  # noqa: BLE001
        return None


def _form_int(value, name: str, default: int, low: int, high: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be a whole number")
    return max(low, min(parsed, high))


_STALL_SECONDS = {"running": 20 * 60, "queued": 5 * 60}


def _is_stalled(job):
    age = _age_seconds(job.get("updated_at"))
    limit = _STALL_SECONDS.get(job.get("status"))
    return bool(age is not None and limit is not None and age > limit)


def _job_stats(jobs):
    out = {"total": len(jobs), "queued": 0, "running": 0, "succeeded": 0,
           "failed": 0, "needs_attention": 0, "clips": 0, "stuck": 0}
    for j in jobs:
        s = j.get("status")
        if s in out:
            out[s] += 1
        if _is_stalled(j):
            out["stuck"] += 1
        for stream in ((j.get("result") or {}).get("streams") or []):
            out["clips"] += len(stream.get("clips") or [])
    return out


def _job_problems(jobs):
    """Short, UI-ready list of why a job is failed or looks stuck."""
    problems = []
    for j in jobs[:60]:
        status = j.get("status")
        reason = ""
        if status in {"failed", "needs_attention"}:
            reason = j.get("error") or "No error message was recorded"
        elif _is_stalled(j):
            age = _age_seconds(j.get("updated_at")) or 0
            reason = (f"No heartbeat/log for {age // 60} min while status is '{status}'. "
                      "The worker may have been interrupted; check the log tail.")
        if reason:
            problems.append({
                "id": j.get("id"), "status": status, "reason": str(reason)[:400],
                "updated_at": j.get("updated_at"), "last_log": (j.get("log") or [])[-1:] or [""],
            })
    return problems[:12]


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _=Depends(require_auth)):
    # The tables load through the JSON APIs so they can poll and paginate.
    return templates.TemplateResponse("index.html", {
        "request": request,
        "defaults": {
            "brain": os.environ.get("STREAMCLIPS_BRAIN", "groq"),
            "auto_upload": os.environ.get("AUTO_UPLOAD", "false").lower() in {"1", "true", "yes", "on"},
            "upload_mode": os.environ.get("UPLOAD_MODE", "queue").strip().lower(),
        },
        "presets": preset_lib.PRESETS,
        "settings": settings_lib.view(),
    })


@app.post("/jobs")
def create_job(
    request: Request,
    prompt: str = Form(""),
    topic: str = Form(""),
    urls: str = Form(""),
    focus: str = Form(""),
    find: Optional[str] = Form(None),
    count: str = Form("5"),
    clips: str = Form("3"),
    when: str = Form("today"),
    brain: str = Form(""),
    language: str = Form(""),
    extra_args: str = Form(""),
    auto_upload: Optional[str] = Form(None),
    _=Depends(require_auth),
):
    payload = {
        "prompt": (prompt or "").strip(),
        "topic": (topic or "").strip() or (prompt or "").strip(),
        "urls": (urls or "").strip(),
        "focus": (focus or "").strip() or (prompt or "").strip(),
        "find": bool(find) or not (urls or "").strip(),
        "count": _form_int(count, "count", 5, 1, 100),
        "clips": _form_int(clips, "clips", 3, 1, 100),
        "when": (when or "today").strip(),
        "brain": (brain or os.environ.get("STREAMCLIPS_BRAIN", "groq")).strip(),
        "language": (language or "").strip() or None,
        "extra_args": (extra_args or "").strip(),
        "auto_upload": bool(auto_upload),
    }
    active = [j for j in store.list(1000)
              if j.get("status") in {"queued", "running"} and not _is_stalled(j)]
    if len(active) >= 2:
        raise HTTPException(status_code=429, detail="Two jobs are already running; wait for one to finish (Whisper models use ~1 GB each).")
    job = store.create(payload)
    start_job_async(store, job["id"], upload_queue)
    return RedirectResponse(url=f"/jobs/{job['id']}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, _=Depends(require_auth)):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse("job.html", {"request": request, "job": job})


@app.get("/api/jobs")
def jobs_json(limit: int = 10, offset: int = 0, all: bool = False, _=Depends(require_auth)):
    """Most recently active jobs first. Default top 10; the UI loads more on demand."""
    jobs = store.list(100000)
    jobs = sorted(jobs, key=lambda j: str(j.get("updated_at") or j.get("created_at") or ""), reverse=True)
    total = len(jobs)
    if not all:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        jobs = jobs[offset:offset + limit]
    slim = []
    for j in jobs:
        result = j.get("result") or {}
        payload = j.get("payload") or {}
        log = j.get("log") or []
        slim.append({
            "id": j.get("id", ""),
            "status": j.get("status", ""),
            "updated_at": j.get("updated_at"),
            "created_at": j.get("created_at"),
            "error": j.get("error"),
            "stalled": _is_stalled(j),
            "progress": result.get("progress") or {},
            "diagnosis": result.get("last_problem") or "",
            "topic": payload.get("topic") or payload.get("prompt") or "",
            "last_log": str(log[-1]) if log else "",
        })
    return JSONResponse({"jobs": slim, "total": total, "limit": limit, "offset": offset, "all": all})


@app.get("/api/jobs/{job_id}")
def job_json(job_id: str, _=Depends(require_auth)):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(job)


@app.post("/jobs/{job_id}/upload")
def upload_job(job_id: str, outdir: str = Form(""), _=Depends(require_auth)):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = job.get("result") or {}
    outdirs = [outdir] if outdir else (result.get("outdirs") or [])
    if not outdirs:
        raise HTTPException(status_code=400, detail="No output directories found for this job")
    for od in outdirs:
        _safe_data_path(od)  # 400/403 if outside DATA_DIR or otherwise invalid
    privacy = os.environ.get("YOUTUBE_PRIVACY", "private")
    category = os.environ.get("YOUTUBE_CATEGORY_ID", "20")
    client_secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secrets.json")
    token_file = os.environ.get("YOUTUBE_TOKEN_FILE", "token.json")
    desc_tmpl = os.environ.get("YOUTUBE_DESCRIPTION_TEMPLATE",
                               "Clipped from {creator}: {title}\nSource: {url}\n\n{caption}\n\n{hashtags}\n\nCredit: {creator} ({url})")
    default_tags = [t.strip() for t in os.environ.get("YOUTUBE_TAGS_DEFAULT", "shorts,clips").split(",") if t.strip()]
    uploads = list(job.get("uploads") or [])
    for od in outdirs:
        try:
            res = upload_clips_from_dir(od, privacy=privacy, category_id=category,
                                        client_secrets=client_secrets, token_file=token_file,
                                        description_template=desc_tmpl, default_tags=default_tags,
                                        on_progress=lambda msg: store.append_log(job_id, msg))
            uploads.append({"outdir": od, "status": "uploaded", "items": res})
        except Exception as e:  # noqa: BLE001
            store.append_log(job_id, f"Upload failed for {od}: {e}")
            uploads.append({"outdir": od, "status": "failed", "error": str(e)})
    store.update(job_id, uploads=uploads)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/download")
def download(path: str, _=Depends(require_auth)):
    target = _safe_data_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(target), filename=target.name)


@app.get("/media")
def media(path: str, _=Depends(require_auth)):
    """Serve a generated clip inline so the browser's <video> player can stream it.

    FileResponse supports HTTP Range requests, so seeking works on large clips.
    """
    target = _safe_data_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = {
        ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
        ".mov": "video/quicktime", ".m4v": "video/x-m4v",
    }.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(str(target), media_type=media_type)


@app.get("/view", response_class=HTMLResponse)
def view_clip(request: Request, path: str, title: str = "", _=Depends(require_auth)):
    target = _safe_data_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return templates.TemplateResponse("view.html", {
        "request": request,
        "title": (title or "").strip() or target.name,
        "name": target.name,
        "path": path,
    })


@app.get("/health")
def health():
    return {"ok": True}


# ------------------------- daily upload queue -------------------------

@app.get("/api/queue")
def queue_json(_=Depends(require_auth)):
    status = scheduler.status()
    items = [i for i in upload_queue.list_items(limit=300) if i.get("status") != "removed"]
    return JSONResponse({"status": status, "items": items})


@app.post("/api/queue/run")
def queue_run(force: bool = False, _=Depends(require_auth)):
    return JSONResponse(scheduler.run_once(force=force))


@app.post("/api/queue/pause")
def queue_pause(_=Depends(require_auth)):
    upload_queue.set_paused(True)
    scheduler._log("[scheduler] paused by user")
    return JSONResponse({"paused": True})


@app.post("/api/queue/resume")
def queue_resume(_=Depends(require_auth)):
    upload_queue.set_paused(False)
    scheduler._log("[scheduler] resumed by user")
    return JSONResponse({"paused": False})


@app.post("/api/queue/scan")
def queue_scan(_=Depends(require_auth)):
    """Enqueue clips from every existing job output (for clips generated before queuing existed)."""
    added = 0
    errors = 0
    for job in store.list(500):
        result = job.get("result") or {}
        for od in result.get("outdirs") or []:
            try:
                added += len(upload_queue.add_clips(
                    outdir=od, job_id=job.get("id", ""),
                    topic=(job.get("payload") or {}).get("topic") or "",
                ))
            except Exception as e:  # noqa: BLE001
                errors += 1
                scheduler._log(f"[queue] scan skipped {od}: {e}")
    scheduler._log(f"[queue] scan complete; {added} new clip(s) queued, {errors} skipped")
    return JSONResponse({"added": added, "errors": errors, "counts": upload_queue.counts()})


@app.post("/api/queue/item/{item_id}/retry")
def queue_retry(item_id: str, _=Depends(require_auth)):
    item = upload_queue.retry(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")
    return JSONResponse(item)


@app.post("/api/queue/item/{item_id}/remove")
def queue_remove(item_id: str, _=Depends(require_auth)):
    item = upload_queue.remove(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")
    return JSONResponse(item)


# ------------------------- monitoring / settings -------------------------

@app.get("/api/dashboard")
def dashboard_json(_=Depends(require_auth)):
    jobs = store.list(500)
    return JSONResponse({
        "jobs": _job_stats(jobs),
        "problems": _job_problems(jobs),
        "llm": llm_stats.snapshot(),
        "queue": scheduler.status(),
        "settings": settings_lib.view(),
        "system": {
            "ok": True,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "disk_free_gb": round(shutil.disk_usage(DATA_DIR).free / 1e9, 1),
            "disk_total_gb": round(shutil.disk_usage(DATA_DIR).total / 1e9, 1),
            "data_dir": str(DATA_DIR),
        },
    })


@app.get("/api/llm")
def llm_json(_=Depends(require_auth)):
    return JSONResponse({
        "stats": llm_stats.snapshot(),
        "configured": settings_lib.groq_keys(),
        "model": settings_lib.view().get("model"),
    })


@app.get("/api/settings")
def settings_json(_=Depends(require_auth)):
    return JSONResponse(settings_lib.view())


@app.post("/api/settings/groq-keys")
def add_groq_keys(keys: str = Form(""), _=Depends(require_auth)):
    if not (keys or "").strip():
        raise HTTPException(status_code=400, detail="No keys provided")
    added = settings_lib.add_groq_keys(keys)
    scheduler._log(f"[settings] {added} Groq key(s) added to .env (masked in UI)")
    return JSONResponse({"added": added, "groq": settings_lib.groq_keys()})


@app.post("/api/settings/groq-keys/remove")
def remove_groq_keys(index: int = Form(-1), _=Depends(require_auth)):
    removed = settings_lib.remove_groq_keys(index)
    scheduler._log(f"[settings] {removed} Groq key(s) removed from .env")
    return JSONResponse({"removed": removed, "groq": settings_lib.groq_keys()})


@app.post("/api/settings/controls")
def update_controls(
    daily_limit: Optional[str] = Form(None),
    tick_minutes: Optional[str] = Form(None),
    dry_run: Optional[str] = Form(None),
    privacy: Optional[str] = Form(None),
    upload_mode: Optional[str] = Form(None),
    _=Depends(require_auth),
):
    kwargs = {}
    if daily_limit is not None:
        kwargs["daily_limit"] = _form_int(daily_limit, "daily_limit", 20, 0, 1000)
    if tick_minutes is not None:
        kwargs["tick_minutes"] = _form_int(tick_minutes, "tick_minutes", 10, 1, 1440)
    if dry_run is not None:
        kwargs["dry_run"] = str(dry_run).strip().lower() in {"1", "true", "yes", "on"}
    if privacy is not None:
        kwargs["privacy"] = privacy
    if upload_mode is not None:
        kwargs["upload_mode"] = upload_mode
    view = settings_lib.update_controls(**kwargs)
    scheduler._log("[settings] upload controls updated in .env")
    return JSONResponse(view)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("AMBERCLAW_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("AMBERCLAW_PORT") or 10000)
    uvicorn.run("app:app", host=host, port=port, reload=False)
