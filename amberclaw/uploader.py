import json
import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]


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
    tags = list(dict.fromkeys(
        [t.lstrip('#') for t in (hashtags or [])] + list(default_tags or []) + [creator]
    ))
    desc = render_description(
        description_template, creator=creator, title=source_title or title or "",
        url=source_url or "", caption=caption or "",
        hashtags=" ".join(hashtags or []),
    )
    full_title = f"{title} | {creator}"
    if len(full_title) > 100:
        full_title = title
    return full_title[:100], desc[:4900], tags[:30]


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
            "tags": tags[:30],
            "categoryId": str(category_id or "20"),
        },
        "status": {
            "privacyStatus": privacy or "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
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
        title = c.get("title") or caption or src_title or vpath.stem
        full_title, desc, tags = build_upload_fields(
            caption=caption, hashtags=hashtags, creator=creator,
            source_title=src_title, source_url=src_url, title=title,
            default_tags=default_tags, description_template=description_template,
        )
        if on_progress:
            on_progress(f"Uploading {vpath.name} ...")
        resp = upload_video(vpath, title=full_title, description=desc, tags=tags,
                            privacy=privacy, category_id=category_id,
                            client_secrets=client_secrets, token_file=token_file)
        vid = resp.get("id") if isinstance(resp, dict) else None
        results.append({"file": vpath.name, "video_id": vid, "url": f"https://youtu.be/{vid}" if vid else None})
        if on_progress:
            on_progress(f"Uploaded {vpath.name} -> {vid}")
    return results
