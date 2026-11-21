import json
import os
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path

from .store import JobStore
from .uploader import UploadError, upload_clips_from_dir


def _bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _env():
    env = dict(os.environ)
    data_dir = Path(env.get("AMBERCLAW_DATA_DIR", "./data")).resolve()
    env.setdefault("STREAMCLIPS_HOME", str(data_dir / "StreamClips"))
    # ensure consistent utf-8 output
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def build_command(payload: dict):
    root = Path(__file__).resolve().parent.parent
    streamclips_py = Path(os.environ.get("STREAMCLIPS_PY", root / "vendor/stream-clips/scripts/streamclips.py"))
    python = os.environ.get("STREAMCLIPS_PYTHON") or sys.executable
    cmd = [str(python), str(streamclips_py), "clip"]

    urls = payload.get("urls") or []
    if isinstance(urls, str):
        urls = [u.strip() for u in re.split(r"[\s,]+", urls) if u.strip()]
    find_mode = _bool(payload.get("find"), default=not urls)
    topic = (payload.get("topic") or "").strip()
    focus = (payload.get("focus") or payload.get("prompt") or "").strip()

    if find_mode:
        cmd.append("--find")
        if payload.get("count"):
            cmd += ["--count", str(int(payload["count"]))]
    cmd += [u for u in urls if u]

    if topic:
        cmd += ["--topic", topic]
    elif not find_mode and urls:
        # stream-clips works better with a topic; derive a simple fallback
        cmd += ["--topic", "clips"]
    if focus and focus != topic:
        cmd += ["--focus", focus]
    if payload.get("clips"):
        cmd += ["--clips", str(int(payload["clips"]))]
    if payload.get("when"):
        cmd += ["--when", str(payload["when"])]
    if payload.get("keywords"):
        cmd += ["--keywords", str(payload["keywords"])]
    if payload.get("search"):
        cmd += ["--search", str(payload["search"])]
    if payload.get("section"):
        cmd += ["--section", str(payload["section"])]
    if _bool(payload.get("new_only")):
        cmd.append("--new-only")
    if payload.get("layout"):
        cmd += ["--layout", str(payload["layout"])]
    if payload.get("language"):
        cmd += ["--language", str(payload["language"])]
    if _bool(payload.get("fast")):
        cmd.append("--fast")

    brain = (payload.get("brain") or os.environ.get("STREAMCLIPS_BRAIN") or "groq").strip()
    cmd += ["--brain", brain]
    model = (payload.get("model") or os.environ.get("STREAMCLIPS_MODEL") or "").strip()
    if model:
        cmd += ["--model", model]

    extra = (payload.get("extra_args") or os.environ.get("STREAMCLIPS_EXTRA_ARGS") or "").strip()
    if extra:
        cmd += shlex.split(extra, posix=(os.name != "nt"))
    return cmd, env_with_brain(_env(), brain, payload)


_key_counters = {"GROQ": 0, "OPENAI": 0}
_key_counter_lock = threading.Lock()


def _pick_api_key(raw, prefix):
    """Round-robin across several free keys supplied comma/newline separated in .env."""
    keys = [k.strip() for k in re.split(r"[\s,]+", str(raw or "")) if k.strip()]
    if not keys:
        return None
    with _key_counter_lock:
        i = _key_counters[prefix] % len(keys)
        _key_counters[prefix] += 1
    return keys[i]


def env_with_brain(env, brain, payload):
    if brain in ("openai", "groq"):
        prefix = "GROQ" if brain == "groq" else "OPENAI"
        # allow per-job override, else fall back to env/.env already loaded
        key = payload.get(f"{brain}_api_key") or _pick_api_key(os.environ.get(f"{prefix}_API_KEY"), prefix)
        if key:
            env[f"{prefix}_API_KEY"] = str(key)
        base = payload.get(f"{brain}_base_url") or os.environ.get(f"{prefix}_BASE_URL")
        if base:
            env[f"{prefix}_BASE_URL"] = str(base)
        model = payload.get(f"{brain}_model") or os.environ.get(f"{prefix}_MODEL")
        if model:
            env[f"{prefix}_MODEL"] = str(model)
    return env


def _scan_clip_dirs_from_log(lines):
    dirs = []
    for line in lines:
        m = re.search(r"folder:\s*(.+)$", line.strip(), flags=re.IGNORECASE)
        if m:
            p = m.group(1).strip().strip('"')
            if p:
                dirs.append(p)
    # de-dup preserve order
    seen = set()
    out = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _summarize_dirs(dirs):
    results = []
    for d in dirs:
        p = Path(d)
        item = {"outdir": str(p), "clips": []}
        cj = p / "clips.json"
        if cj.exists():
            try:
                data = json.loads(cj.read_text(encoding="utf-8-sig"))
                item["source"] = data.get("source")
                item["clips"] = data.get("clips") or []
            except Exception:
                pass
        results.append(item)
    return results


def _progress_from_log(lines):
    """Extract per-stream transcription/download progress for the job UI."""
    out = {}
    for line in lines:
        m = re.search(r"(?:\[\d+:\d+\]\s*)?(.+?):\s*heard\s+(\d+)\s*/\s*(\d+)", line)
        if m:
            name = m.group(1).strip()
            out[name] = {"done": int(m.group(2)), "total": int(m.group(3)), "stage": "transcribing"}
            continue
        m = re.search(r"(?:\[\d+:\d+\]\s*)?(.+?):\s*(downloading|audio ready|rendering|cutting)", line)
        if m:
            name = m.group(1).strip()
            out.setdefault(name, {"done": 0, "total": 0, "stage": m.group(2)})
    return out


def _failure_details(lines):
    """Return the most useful log lines when a job exits with an error."""
    problems = [ln.strip() for ln in lines
                if re.match(r"^\s*(?:\[\d+:\d+\]\s*)?x\s+", ln)]
    if problems:
        return " | ".join(problems[-3:])[:500]
    for ln in reversed(lines):
        low = ln.lower()
        if any(k in low for k in ("error", "failed", "not a bot", "age-restricted", "cookie")):
            return ln.strip()[:500]
    return (lines[-1].strip()[:500] if lines else "no log output")


def run_job(store: JobStore, job_id: str, queue=None):
    """Run a job, guaranteeing an unexpected crash never leaves it stuck 'running'."""
    try:
        return _run_job_impl(store, job_id, queue)
    except Exception as e:  # noqa: BLE001
        try:
            store.update(job_id, status="failed", error=f"Job crashed: {e}")
        except Exception:
            pass


def _run_job_impl(store: JobStore, job_id: str, queue=None):
    job = store.get(job_id)
    if not job:
        return
    payload = job.get("payload") or {}
    store.update(job_id, status="running", error=None)
    try:
        cmd, env = build_command(payload)
    except Exception as e:  # noqa: BLE001
        store.update(job_id, status="failed", error=f"Failed to build command: {e}")
        return
    store.append_log(job_id, "$ " + " ".join(shlex.quote(c) for c in cmd))

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
            cwd=str(Path(__file__).resolve().parent.parent), bufsize=1)
    except Exception as e:  # noqa: BLE001
        store.update(job_id, status="failed", error=f"Failed to start stream-clips: {e}")
        return

    lines = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        lines.append(line)
        store.append_log(job_id, line)
    code = proc.wait()
    outdirs = _scan_clip_dirs_from_log(lines)
    streams = _summarize_dirs(outdirs)
    has_clips = any((s.get("clips") or []) for s in streams)
    result = {
        "exit_code": code,
        "outdirs": outdirs,
        "streams": streams,
        "progress": _progress_from_log(lines),
        "last_problem": _failure_details(lines),
        "last_log": (lines[-1] if lines else ""),
    }

    if code == 3:
        # agent mode waiting for answers
        result["needs"] = "agent_answers"
        store.update(job_id, status="needs_attention", result=result,
                     error="stream-clips is waiting for agent answers (brain=agent). Use brain=groq or a configured CLI brain for automated runs.")
        return
    if not has_clips:
        # stream-clips can exit 0 even when every stream was filtered or every
        # clip encode failed. Never report that as a successful job.
        detail = result["last_problem"]
        store.update(job_id, status="failed", result=result,
                     error=f"stream-clips produced no clips (exit {code}). Reason: {detail}")
        return

    uploads = []
    auto_upload = _bool(payload.get("auto_upload"), default=_bool(os.environ.get("AUTO_UPLOAD"), False))
    upload_mode = str(os.environ.get("UPLOAD_MODE", "queue")).strip().lower()

    # Queue mode: generate now, upload a few each day. No immediate API calls.
    if auto_upload and outdirs and has_clips and upload_mode == "queue" and queue is not None:
        for od in outdirs:
            if not (Path(od) / "clips.json").exists():
                continue
            try:
                added = queue.add_clips(
                    outdir=od, job_id=job_id,
                    topic=payload.get("topic") or payload.get("prompt") or "",
                    job_log=lambda msg: store.append_log(job_id, msg),
                )
                uploads.append({"outdir": od, "status": "queued", "clips": len(added)})
            except Exception as e:  # noqa: BLE001
                store.append_log(job_id, f"Queue failed for {od}: {e}")
                uploads.append({"outdir": od, "status": "failed", "error": str(e)})
        store.update(job_id, uploads=uploads)
        store.append_log(job_id, "[system] Clips queued for daily upload; generate more any time.")
        # Skip the immediate-upload block below.
        outdirs_for_upload = []
    else:
        outdirs_for_upload = outdirs

    if auto_upload and outdirs_for_upload and has_clips:
        privacy = payload.get("youtube_privacy") or os.environ.get("YOUTUBE_PRIVACY", "private")
        category = payload.get("youtube_category_id") or os.environ.get("YOUTUBE_CATEGORY_ID", "20")
        client_secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secrets.json")
        token_file = os.environ.get("YOUTUBE_TOKEN_FILE", "token.json")
        desc_tmpl = os.environ.get("YOUTUBE_DESCRIPTION_TEMPLATE",
                                   "Clipped from {creator}: {title}\nSource: {url}\n\n{caption}\n\n{hashtags}\n\nCredit: {creator} ({url})")
        default_tags = [t.strip() for t in os.environ.get("YOUTUBE_TAGS_DEFAULT", "shorts,clips").split(",") if t.strip()]
        for od in outdirs_for_upload:
            try:
                p = Path(od)
                if not (p / "clips.json").exists():
                    continue
                # only upload if this outdir has clips
                data = _summarize_dirs([od])
                if not data or not (data[0].get("clips") or []):
                    continue
                res = upload_clips_from_dir(
                    od, privacy=privacy, category_id=category,
                    client_secrets=client_secrets, token_file=token_file,
                    description_template=desc_tmpl, default_tags=default_tags,
                    on_progress=lambda msg: store.append_log(job_id, msg))
                uploads.append({"outdir": od, "status": "uploaded", "items": res})
            except Exception as e:  # noqa: BLE001
                store.append_log(job_id, f"Upload failed for {od}: {e}")
                uploads.append({"outdir": od, "status": "failed", "error": str(e)})

    final_status = "succeeded"
    final_error = None
    if code != 0 and has_clips:
        final_error = (f"stream-clips exited with code {code} but produced clips (partial success). "
                       f"Reason: {result['last_problem']}")
        store.append_log(job_id, f"[system] {final_error}")
    if auto_upload and has_clips and any(u.get("status") == "failed" for u in uploads) and not any(u.get("status") == "uploaded" for u in uploads):
        final_status = "failed"
        final_error = (final_error + "; " if final_error else "") + "auto-upload failed for all outputs"
    store.update(job_id, status=final_status, result=result, uploads=uploads, error=final_error)


def start_job_async(store: JobStore, job_id: str, queue=None):
    t = threading.Thread(target=run_job, args=(store, job_id, queue), daemon=True)
    t.start()
    return t
