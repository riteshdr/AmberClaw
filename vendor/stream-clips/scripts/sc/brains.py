"""Who reads the transcript and picks the moments. Each backend is a coding-agent CLI the
user is already signed in to, so it runs on their own subscription (no API keys):

  claude    Claude Code       claude -p                       (prompt on stdin)
  codex     OpenAI Codex CLI  codex exec -                    (prompt on stdin)
  gemini    Gemini CLI        gemini -p "..."                 (prompt on stdin)
  opencode  OpenCode          opencode run "read <file> ..."  (prompt in a file)
  openai    OpenAI-compatible API (OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL)
  agent     no CLI call: prompts are written to files and the agent running this
            skill answers them itself, then runs the same command again.
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .common import Problem

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
        if wanted == "openai":
            if os.environ.get("OPENAI_API_KEY"):
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
        raise Problem(f"{cmd[0]} did not answer within {TIMEOUT // 60} min") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        text = " | ".join(detail[-4:])[:400]
        low = text.lower()
        if any(k in low for k in ("authenticate", "login", "log in", "oauth", "unauthorized",
                                  "api key", "credentials", "not logged")):
            raise SignedOut(f"the {cmd[0]} CLI is not signed in ({text[:160]}). Sign in once by "
                            f"running `{cmd[0]}` in a terminal (Claude Code: /login), or use "
                            f"--brain agent to let the current agent pick the moments.")
        raise Problem(f"{cmd[0]} failed: " + text)
    return proc.stdout


def _ask_openai(prompt, model=None):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise Problem("--brain openai requires OPENAI_API_KEY to be set.")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Follow the user's instructions exactly. Reply with only the JSON array requested."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions", data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise Problem(f"openai API failed ({exc.code}): {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise Problem(f"openai API request failed: {exc}") from exc
    try:
        return body["choices"][0]["message"]["content"] or ""
    except Exception as exc:  # noqa: BLE001
        raise Problem(f"openai API returned an unexpected response: {str(body)[:300]}") from exc


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
    raise Problem(f"unknown brain '{brain}'")
