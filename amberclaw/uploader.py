import json
import os
import time
from pathlib import Path

from . import metadata as meta

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]

# Guaranteed GTA 6 trending tag set. Every short gets at least these 15 tags,
# regardless of what the source clip or .env happens to contain.
GTA6_TAGS = [
    "gta6", "gta 6", "grand theft auto 6", "gta vi", "grand theft auto vi",
    "gta6 gameplay", "gta 6 gameplay", "gta6 shorts", "gta 6 shorts",
    "gta6 funny", "gta 6 funny", "gta6 moments", "gta 6 moments",
    "gta6 hindi", "gta6 hinglish",
]
MIN_TAGS = 15
MAX_TAG_CHARS = 500  # YouTube's total tags length limit


class UploadError(RuntimeError):
    pass


def _build_service(client_secrets: str, token_file: str):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except Exception as e:  # noqa: BLE001
        raise UploadError(f"YouTube upload dependencies are missing: {e}") from e

    creds = None
    token_path = Path(token_file)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secrets = Path(client_secrets)
            if not secrets.exists():
                raise UploadError(
                    f"Missing YouTube auth. Provide {client_secrets} and complete OAuth to create {token_file}.")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            # Console flow works for local/token generation; for web-hosted setup user may run helper locally.
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def render_description(template: str, *, creator: str, title: str, url: str, caption: str, hashtags: str):
    # python-dotenv keeps \n literal in unquoted .env values; turn it into real newlines.
    template = (template or "").replace("\\n", "\n")
    values = {
        "creator": creator or "Unknown creator",
        "title": title or "",
        "url": url or "",
        "caption": caption or "",
        "hashtags": hashtags or "",
    }
    try:
        return template.format(**values)
    except Exception:
        return f"Clipped from {creator}\nSource: {url}\n\n{caption}\n\n{hashtags}"


def build_upload_fields(*, caption: str, hashtags: list[str], creator: str,
                        source_title: str, source_url: str, title: str,
                        default_tags: list[str], description_template: str):
    """Build the YouTube title/description/tags shared by immediate and scheduled uploads."""
    creator = creator or "Unknown creator"
    # Order matters: the 15 GTA6 tags always come first so the minimum survives
    # YouTube's 500-character total limit even with long creator/hashtag names.
    raw_tags = (
        GTA6_TAGS
        + [t.lstrip('#').strip() for t in (hashtags or [])]
        + [t.lstrip('#').strip() for t in (default_tags or [])]
        + [creator]
    )
    tags = []
    used = 0
    for t in raw_tags:
        t = str(t).strip().lstrip('#')
        if not t or len(t) > 30 or t.lower() in {x.lower() for x in tags}:
            continue
        extra = len(t) + (1 if tags else 0)
        if used + extra > MAX_TAG_CHARS:
            continue
        tags.append(t)
        used += extra
    try:
        min_tags = max(MIN_TAGS, int(os.environ.get("YOUTUBE_MIN_TAGS", MIN_TAGS)))
    except Exception:
        min_tags = MIN_TAGS
    if len(tags) < min_tags:
        raise UploadError(f"Only {len(tags)} tags built; need at least {min_tags} GTA6 tags")
    desc = render_description(
        description_template, creator=creator, title=source_title or title or "",
        url=source_url or "", caption=caption or "",
        hashtags=" ".join(hashtags or []),
    )
    full_title = f"{title} | {creator}"
    if len(full_title) > 100:
        full_title = title
    return full_title[:100], desc[:4900], tags


def upload_video(video_path: str | Path, *, title: str, description: str, tags: list[str],
                 privacy: str = "private", category_id: str = "20",
                 client_secrets: str = "client_secrets.json", token_file: str = "token.json"):
    from googleapiclient.http import MediaFileUpload

    video_path = Path(video_path)
    if not video_path.exists():
        raise UploadError(f"Video not found: {video_path}")
    youtube = _build_service(client_secrets, token_file)
    body = {
        "snippet": {
            "title": (title or video_path.stem)[:100],
            "description": (description or "")[:4900],
            "tags": tags[:60],
            "categoryId": str(category_id or "20"),
        },
        "status": {
            "privacyStatus": privacy or "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        timeout = max(60, int(os.environ.get("YOUTUBE_UPLOAD_TIMEOUT_SECONDS", "1800")))
    except (TypeError, ValueError):
        timeout = 1800
    deadline = time.monotonic() + timeout
    response = None
    while response is None:
        if time.monotonic() > deadline:
            raise UploadError(f"Upload exceeded {timeout}s timeout")
        status, response = request.next_chunk()
        # status progress could be surfaced; caller handles final response
    return response


def upload_clips_from_dir(outdir: str | Path, *, privacy: str, category_id: str,
                          client_secrets: str, token_file: str, description_template: str,
                          default_tags: list[str], on_progress=None):
    outdir = Path(outdir)
    clips_json = outdir / "clips.json"
    if not clips_json.exists():
        raise UploadError(f"clips.json not found in {outdir}")
    data = json.loads(clips_json.read_text(encoding="utf-8-sig"))
    source = data.get("source") or {}
    creator = source.get("name") or "Unknown creator"
    src_title = source.get("title") or ""
    src_url = source.get("url") or ""
    results = []
    for c in data.get("clips") or []:
        vpath = outdir / c.get("file", "")
        caption = c.get("caption") or ""
        hashtags = c.get("hashtags") or []
        hinglish_title, hinglish_desc = meta.generate(
            caption=caption, source_title=src_title, creator=creator,
            topic=c.get("title") or src_title or "",
        )
        full_title, desc, tags = build_upload_fields(
            caption=hinglish_desc, hashtags=hashtags, creator=creator,
            source_title=src_title, source_url=src_url, title=hinglish_title,
            default_tags=default_tags, description_template=description_template,
        )
        if on_progress:
            on_progress(f"Uploading {vpath.name} -> {full_title}")
        resp = upload_video(vpath, title=full_title, description=desc, tags=tags,
                            privacy=privacy, category_id=category_id,
                            client_secrets=client_secrets, token_file=token_file)
        vid = resp.get("id") if isinstance(resp, dict) else None
        results.append({"file": vpath.name, "video_id": vid, "url": f"https://youtu.be/{vid}" if vid else None})
        if on_progress:
            on_progress(f"Uploaded {vpath.name} -> {vid}")
    return results
