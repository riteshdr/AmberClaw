"""Who reads the transcript and picks the moments. Each backend is a coding-agent CLI the
user is already signed in to, so it runs on their own subscription (no API keys):

  claude    Claude Code       claude -p                       (prompt on stdin)
  codex     OpenAI Codex CLI  codex exec -                    (prompt on stdin)
  gemini    Gemini CLI        gemini -p "..."                 (prompt on stdin)
  opencode  OpenCode          opencode run "read <file> ..."  (prompt in a file)
  openai    OpenAI-compatible API (OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL)
  groq      Groq OpenAI-compatible API with free open-weight GPT-OSS models
            (GROQ_API_KEY/GROQ_BASE_URL/GROQ_MODEL)
  agent     no CLI call: prompts are written to files and the agent running this
            skill answers them itself, then runs the same command again.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import Problem

# AmberClaw dashboard: record LLM health without leaking keys. stream-clips may
# also be used standalone, where the amberclaw package is not importable.
try:
    from amberclaw.llm_stats import record as _llm_record
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    try:
        from amberclaw.llm_stats import record as _llm_record
    except Exception:
        _llm_record = None


def _record_llm(provider, **kwargs):
    if _llm_record is None:
        return
    try:
        _llm_record(provider, **kwargs)
    except Exception:  # noqa: BLE001 - monitoring must never block a job
        pass


ORDER = ("claude", "codex", "gemini", "opencode")

# Environment variables these tools set for the commands they run, used to prefer the
# CLI of whichever agent launched us.
HOST_HINTS = {
    "claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    "codex": ("CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM", "CODEX_HOME"),
    "gemini": ("GEMINI_CLI",),
    "opencode": ("OPENCODE", "OPENCODE_BIN_PATH"),
}

TIMEOUT = 900


class NeedAgent(Exception):
    """Agent mode: some prompt files still need an answer file."""

    def __init__(self, waiting):
        super().__init__(f"{len(waiting)} part(s) waiting for answers")
        self.waiting = waiting


class SignedOut(Problem):
    """The brain CLI is installed but not logged in: stop instead of retrying every part."""


def choose(wanted="auto"):
    if wanted and wanted != "auto":
        if wanted == "agent":
            return wanted
        if wanted in ("openai", "groq"):
            key = "GROQ_API_KEY" if wanted == "groq" else "OPENAI_API_KEY"
            if os.environ.get(key):
                return wanted
            # no key: fall back to any available CLI brain, else agent
            for name, hints in HOST_HINTS.items():
                if any(os.environ.get(h) for h in hints) and shutil.which(name):
                    return name
            for name in ORDER:
                if shutil.which(name):
                    return name
            return "agent"
        if shutil.which(wanted):
            return wanted
        # requested CLI missing: fall back to auto resolution
        wanted = "auto"
    for name, hints in HOST_HINTS.items():
        if any(os.environ.get(h) for h in hints) and shutil.which(name):
            return name
    for name in ORDER:
        if shutil.which(name):
            return name
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "agent"


def _child_env():
    env = dict(os.environ)
    # A nested `claude -p` refuses to start if it thinks it's inside another session.
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    env["NO_COLOR"] = "1"
    return env


def _call(cmd, stdin_text=None, cwd=None):
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        proc = subprocess.run([exe, *cmd[1:]], input=stdin_text, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=TIMEOUT,
                              env=_child_env(), cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        _record_llm(cmd[0], ok=False, error=str(exc), kind="cli")
        raise Problem(f"{cmd[0]} did not answer within {TIMEOUT // 60} min") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        text = " | ".join(detail[-4:])[:400]
        _record_llm(cmd[0], ok=False, error=text, kind="cli")
        low = text.lower()
        if any(k in low for k in ("authenticate", "login", "log in", "oauth", "unauthorized",
                                  "api key", "credentials", "not logged")):
            raise SignedOut(f"the {cmd[0]} CLI is not signed in ({text[:160]}). Sign in once by "
                            f"running `{cmd[0]}` in a terminal (Claude Code: /login), or use "
                            f"--brain agent to let the current agent pick the moments.")
        raise Problem(f"{cmd[0]} failed: " + text)
    _record_llm(cmd[0], ok=True, kind="cli")
    return proc.stdout


_API_LOCK = threading.Lock()
_LAST_CALL = {"t": 0.0}
_API_KEY_INDEX = 0
_MIN_INTERVAL = 6.0  # seconds between API calls; avoids free-tier RPM/TPM bursts
_MAX_RETRIES = 10


def _api_key(key_name):
    """Rotate through keys added from the dashboard (comma/newline separated in .env)."""
    global _API_KEY_INDEX
    raw = os.environ.get(key_name, "")
    keys = [k.strip() for k in re.split(r"[\s,]+", str(raw)) if k.strip()]
    if not keys:
        raise Problem(f"{key_name} is not set")
    with _API_LOCK:
        key = keys[_API_KEY_INDEX % len(keys)]
        _API_KEY_INDEX += 1
    return key


def _throttle():
    """Serialize API calls so a batch of transcript parts cannot burst the free tier."""
    with _API_LOCK:
        wait = _MIN_INTERVAL - (time.monotonic() - _LAST_CALL["t"])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL["t"] = time.monotonic()


def _retry_delay(exc, raw):
    headers = getattr(exc, "headers", None)
    header = headers.get("Retry-After") if headers else None
    if header:
        try:
            return min(90.0, float(header))
        except (TypeError, ValueError):
            pass
    try:
        obj = json.loads(raw)
        err = obj.get("error", {}) if isinstance(obj, dict) else {}
        value = err.get("retry_after") or obj.get("retry_after")
        if value:
            return min(90.0, float(value))
    except (TypeError, ValueError, AttributeError):
        pass
    return 2.0 + random.random() * 2.0


def _ask_api(brain, key_name, base_name, default_base, model_name, default_model, prompt, model=None):
    if not os.environ.get(key_name):
        raise Problem(f"--brain {brain} requires {key_name} to be set.")
    base = os.environ.get(base_name, default_base).rstrip("/")
    model = model or os.environ.get(model_name) or default_model
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Follow the user's instructions exactly. Reply with only the JSON array requested."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # gpt-oss is a reasoning model: it spends tokens on hidden reasoning before
        # emitting the JSON array. A small budget leaves the content empty.
        "max_tokens": 1800,
    }
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(_MAX_RETRIES + 1):
        _throttle()
        # Rebuild each attempt so a 429 rotates to the next key added in the UI.
        try:
            key = _api_key(key_name)
        except Problem as exc:
            _record_llm(brain, model=model, ok=False, error=str(exc), kind="picker")
            raise
        req = urllib.request.Request(
            f"{base}/chat/completions", data=data,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Groq's edge blocks urllib's default Python-urllib User-Agent (403 / 1010).
                "User-Agent": "openai-python/1.0",
            },
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            _record_llm(brain, model=model, key=key, ok=True, http_status=200, kind="picker")
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            detail = raw[:400]
            _record_llm(brain, model=model, key=key, ok=False, http_status=exc.code,
                        error=detail, kind="picker")
            if exc.code in {429, 500, 502, 503, 504} and attempt < _MAX_RETRIES:
                delay = _retry_delay(exc, raw)
                print(f"{brain} API {exc.code}; retrying in {delay:.1f}s "
                      f"({attempt + 1}/{_MAX_RETRIES})", file=sys.stderr)
                time.sleep(delay)
                continue
            raise Problem(f"{brain} API failed ({exc.code}): {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            _record_llm(brain, model=model, key=key, ok=False, error=str(exc), kind="picker")
            raise Problem(f"{brain} API request failed: {exc}") from exc
    try:
        return body["choices"][0]["message"]["content"] or ""
    except Exception as exc:  # noqa: BLE001
        _record_llm(brain, model=model, ok=False, error=str(body)[:300], kind="picker")
        raise Problem(f"{brain} API returned an unexpected response: {str(body)[:300]}") from exc


def _ask_openai(prompt, model=None):
    return _ask_api("openai", "OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com/v1",
                    "OPENAI_MODEL", "gpt-4o-mini", prompt, model)


def _ask_groq(prompt, model=None):
    return _ask_api("groq", "GROQ_API_KEY", "GROQ_BASE_URL", "https://api.groq.com/openai/v1",
                    "GROQ_MODEL", "openai/gpt-oss-20b", prompt, model)


def ask(brain, prompt, model=None, scratch: Path = None):
    """Send one prompt, return the raw text reply."""
    scratch = scratch or Path(tempfile.gettempdir())
    if brain == "claude":
        return _call(["claude", "-p", *(["--model", model] if model else [])], prompt)
    if brain == "codex":
        out = scratch / f"codex_reply_{os.getpid()}_{id(prompt)}.txt"
        _call(["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "--color", "never", "--output-last-message", str(out),
               *(["-m", model] if model else []), "-"], prompt, cwd=str(scratch))
        try:
            return out.read_text(encoding="utf-8")
        finally:
            out.unlink(missing_ok=True)
    if brain == "gemini":
        return _call(["gemini", *(["-m", model] if model else []), "-p",
                      "Follow the instructions in the text above and reply with only the JSON array"],
                     prompt, cwd=str(scratch))
    if brain == "opencode":
        task = scratch / f"opencode_task_{os.getpid()}_{id(prompt)}.txt"
        task.write_text(prompt, encoding="utf-8")
        try:
            return _call(["opencode", "run", *(["-m", model] if model else []),
                          f"Read the file {task} and do exactly what it asks. "
                          f"Reply with only the JSON array it asks for"], cwd=str(scratch))
        finally:
            task.unlink(missing_ok=True)
    if brain == "openai":
        return _ask_openai(prompt, model)
    if brain == "groq":
        return _ask_groq(prompt, model)
    raise Problem(f"unknown brain '{brain}'")
