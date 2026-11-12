"""Read-only reader for OpenAI Codex CLI sessions.

Codex writes one JSONL "rollout" per session:
    $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<local start time>-<session id>.jsonl
(CODEX_HOME defaults to ~/.codex). The first line is `session_meta` (id, cwd, start time, CLI
version); later lines are `turn_context` (cwd, model, sandbox and approval policy),
`response_item` (messages and tool calls) and `event_msg` (task_started / task_complete /
turn_aborted around each turn). The file appears at the first prompt, not at launch, and a
resumed session keeps appending to its original file.

Files are opened read-only, only their first line and tail are read, and summaries are cached
by (mtime, size), so unchanged sessions cost one stat per snapshot.
"""
import glob
import json
import os
import re
from datetime import datetime

from .registry import HOME

TAIL_BYTES = 512 * 1024
TURN_STALE_SECS = 600      # a turn started but silent for this long is treated as dead
DEFAULT_HOME = os.path.join(HOME, ".codex")
_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)
_SHELL_FLAGS = ("-c", "-lc", "-Command", "-command", "/c", "/C")

_meta_cache = {}           # path -> session_meta summary (the first line never changes)
_summary_cache = {}        # path -> ((mtime, size), summary)


def homes(extra=()):
    """Codex config dirs with a sessions folder: ~/.codex, $CODEX_HOME and any `extra`."""
    cands = [DEFAULT_HOME, os.environ.get("CODEX_HOME")] + list(extra)
    out = []
    for h in cands:
        if h:
            h = os.path.normpath(os.path.expanduser(h))
            if h not in out and os.path.isdir(os.path.join(h, "sessions")):
                out.append(h)
    return out


def session_files(extra_homes=()):
    out = []
    for h in homes(extra_homes):
        out += glob.glob(os.path.join(h, "sessions", "*", "*", "*", "rollout-*.jsonl"))
    return out


def _ts(s):
    """ISO 8601 (with a trailing Z) -> epoch seconds; Python 3.9's fromisoformat has no Z."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _short(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def read_meta(path):
    """Session id, folder and start time from the first line (cached: it never changes)."""
    if path in _meta_cache:
        return _meta_cache[path]
    m = _ID.search(os.path.basename(path))
    meta = {"session_id": m.group(1) if m else os.path.basename(path), "cwd": None,
            "started": None, "source": None, "cli_version": None, "git_branch": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = json.loads(f.readline() or "{}")
    except (OSError, ValueError):
        return meta                    # don't cache: the file may still be being written
    if first.get("type") == "session_meta":
        p = first.get("payload") or {}
        meta.update({
            "session_id": p.get("id") or p.get("session_id") or meta["session_id"],
            "cwd": p.get("cwd"), "started": _ts(p.get("timestamp")),
            "source": p.get("source"), "cli_version": p.get("cli_version"),
            "git_branch": (p.get("git") or {}).get("branch"),
        })
    _meta_cache[path] = meta
    return meta


def _tail_lines(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()                   # drop the partial line
        data = f.read().decode("utf-8", "replace")
    return data.splitlines()


def _command(c):
    """A shell call's command as one string, without the shell wrapper (bash -lc / powershell -Command)."""
    if isinstance(c, list):
        c = [str(x) for x in c]
        for i, x in enumerate(c[:-1]):
            if x in _SHELL_FLAGS:
                return c[i + 1]
        return " ".join(c)
    return c if isinstance(c, str) else ""


def describe_tool(name, args):
    """'shell: git status', 'apply_patch: src/x.py', 'web_search: query' ... (short, no output)."""
    a = args
    if isinstance(args, str):
        try:
            a = json.loads(args)
        except ValueError:
            a = args
    detail = ""
    if isinstance(a, dict):
        detail = _command(a.get("command") or a.get("cmd")) or a.get("path") or a.get("file_path") \
            or a.get("query") or a.get("url") or ""
    elif isinstance(a, str) and a:
        m = re.search(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", a, re.M)   # apply_patch input
        call = re.search(r"\btools\.(\w+)\s*\(", a)       # 0.16x "exec": JS calling tools.exec_command({cmd:"..."})
        arg_ = re.search(r"\b(?:cmd|command|path|file_path|query|url)\s*:\s*\"((?:[^\"\\]|\\.)*)\"", a)
        if m:
            detail = m.group(1)
        elif call:
            name = call.group(1)
            detail = arg_.group(1).encode().decode("unicode_escape", "replace") if arg_ else ""
        else:
            detail = a.splitlines()[0]
    return _short("%s: %s" % (name, detail) if detail else name, 140)


def _user_text(payload):
    texts = [b.get("text", "") for b in payload.get("content") or []
             if isinstance(b, dict) and b.get("type") in ("input_text", "text")]
    t = " ".join(x for x in texts if x and not x.lstrip().startswith("<"))
    return _short(t, 240) if t.strip() else None


def read_session(path):
    """Summarise one session (first line + tail), in the same shape as Claude Code's reader."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    hit = _summary_cache.get(path)
    if hit and hit[0] == key:
        return dict(hit[1])
    meta = read_meta(path)
    info = {
        "session_id": meta["session_id"], "transcript": path, "last_activity": st.st_mtime,
        "started": meta["started"], "cwd": meta["cwd"], "git_branch": meta["git_branch"],
        "last_prompt": None, "last_tool": None, "model": None, "permission_mode": None,
        "approval_policy": None, "tool_calls": 0, "current_dir": None, "turn_open": False,
        "codex_source": meta["source"],
    }
    try:
        lines = _tail_lines(path)
    except OSError:
        lines = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t, p = e.get("type"), e.get("payload")
        if not isinstance(p, dict):
            continue
        pt = p.get("type")
        if t == "turn_context":
            info["current_dir"] = p.get("cwd") or info["current_dir"]
            info["model"] = p.get("model") or info["model"]
            sb = p.get("sandbox_policy")
            info["permission_mode"] = (sb.get("type") if isinstance(sb, dict) else sb) or info["permission_mode"]
            ap = p.get("approval_policy")
            if isinstance(ap, str):
                info["approval_policy"] = ap
        elif t == "response_item":
            if pt == "message" and p.get("role") == "user":
                info["last_prompt"] = _user_text(p) or info["last_prompt"]
            elif pt in ("function_call", "custom_tool_call"):
                info["tool_calls"] += 1
                info["last_tool"] = describe_tool(p.get("name") or pt, p.get("arguments", p.get("input")))
            elif pt == "local_shell_call":
                info["tool_calls"] += 1
                info["last_tool"] = describe_tool("shell", (p.get("action") or {}))
            elif pt == "web_search_call":
                info["tool_calls"] += 1
                info["last_tool"] = describe_tool("web_search", (p.get("action") or {}))
        elif t == "event_msg":
            if pt == "task_started":
                info["turn_open"] = True
            elif pt in ("task_complete", "turn_aborted"):
                info["turn_open"] = False
            elif pt == "user_message" and p.get("message"):        # older CLI versions
                msg = str(p["message"])
                if not msg.lstrip().startswith("<"):
                    info["last_prompt"] = _short(msg, 240)
    info["cwd"] = info["cwd"] or info["current_dir"]
    _summary_cache[path] = (key, info)
    return dict(info)


def turn_running(info, now):
    """A turn was started and the file is still being written (Codex records no 'running' state)."""
    return bool(info.get("turn_open")) and now - (info.get("last_activity") or 0) < TURN_STALE_SECS


def _same_dir(a, b):
    return bool(a and b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def session_dir(args, cwd):
    """The folder a Codex process works in: `-C/--cd <dir>` if given, else its cwd."""
    m = re.search(r"(?:^|\s)(?:-C|--cd)(?:=|\s+)(\"[^\"]+\"|'[^']+'|\S+)", args or "")
    if not m:
        return cwd
    d = m.group(1).strip("\"'")
    return os.path.normpath(os.path.join(cwd, d)) if cwd and not os.path.isabs(d) else d


def resume_id(args):
    m = re.search(r"\bresume\s+(?:--\S+\s+)*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  args or "", re.I)
    return m.group(1) if m else None


def find_live_session(files, cwd, proc_start, resume_id=None, claimed=()):
    """The session file a running Codex process is writing.

    The resumed id from its command line if there is one; otherwise the newest file written since
    the process started for its folder (see session_dir). Only when the folder is unknown
    (Windows without psutil) the newest file from any folder is used.
    """
    if resume_id:
        for f in files:
            if f not in claimed and read_meta(f)["session_id"] == resume_id:
                return f
    cands = []
    for f in files:
        if f in claimed:
            continue
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if proc_start is None or m >= proc_start - 2:
            cands.append((m, f))
    if cwd:
        cands = [c for c in cands if _same_dir(read_meta(c[1])["cwd"], cwd)]
    return max(cands)[1] if cands else None
