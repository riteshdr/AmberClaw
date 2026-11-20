import os
import secrets
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from amberclaw.runner import start_job_async
from amberclaw.store import JobStore
from amberclaw.uploader import UploadError, upload_clips_from_dir

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AMBERCLAW_DATA_DIR", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
store = JobStore(DATA_DIR / "jobs")

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
    except Exception:
        pass


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    cfg = os.environ.get("AMBERCLAW_BASIC_AUTH", "").strip()
    if not cfg:
        return True
    if not credentials:
        raise HTTPException(status_code=401, detail="Auth required", headers={"WWW-Authenticate": "Basic"})
    allowed = [tuple(x.split(":", 1)) for x in cfg.split(",") if ":" in x]
    for user, pwd in allowed:
        if secrets.compare_digest(credentials.username, user) and secrets.compare_digest(credentials.password, pwd):
            return True
    raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


def _safe_data_path(p: str | Path) -> Path:
    target = Path(p).resolve()
    if DATA_DIR in target.parents or target == DATA_DIR:
        return target
    raise HTTPException(status_code=403, detail="Access outside data directory is not allowed")


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _=Depends(require_auth)):
    jobs = store.list(50)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "jobs": jobs,
        "defaults": {
            "brain": os.environ.get("STREAMCLIPS_BRAIN", "groq"),
            "auto_upload": os.environ.get("AUTO_UPLOAD", "false").lower() in {"1", "true", "yes", "on"},
        },
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
        "count": int(count or 5),
        "clips": int(clips or 3),
        "when": (when or "today").strip(),
        "brain": (brain or os.environ.get("STREAMCLIPS_BRAIN", "groq")).strip(),
        "language": (language or "").strip() or None,
        "extra_args": (extra_args or "").strip(),
        "auto_upload": bool(auto_upload),
    }
    job = store.create(payload)
    start_job_async(store, job["id"])
    return RedirectResponse(url=f"/jobs/{job['id']}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, _=Depends(require_auth)):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse("job.html", {"request": request, "job": job})


@app.get("/api/jobs")
def jobs_json(_=Depends(require_auth)):
    jobs = store.list(50)
    slim = []
    for j in jobs:
        slim.append({
            "id": j["id"],
            "status": j["status"],
            "updated_at": j.get("updated_at"),
            "created_at": j.get("created_at"),
            "error": j.get("error"),
            "topic": (j.get("payload") or {}).get("topic") or (j.get("payload") or {}).get("prompt") or "",
            "last_log": (j.get("log") or [""])[-1],
        })
    return JSONResponse({"jobs": slim})


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


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("AMBERCLAW_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("AMBERCLAW_PORT") or 10000)
    uvicorn.run("app:app", host=host, port=port, reload=False)
