#!/usr/bin/env python3
"""Watch-only agent hook: forward a *small summary* of each hook event to cADRe.

Installed by `python3 -m cadre --install-hooks` into every agent that has hooks
(Claude Code, Codex, Gemini CLI, Copilot CLI, Cursor, Windsurf). Each install passes
`--agent <key> --event <name>`, because some agents (Copilot) don't name the event in
the payload, and every agent spells events and fields differently. Those are mapped
onto cADRe's names: SessionStart, UserPromptSubmit, PostToolUse, PostToolUseFailure,
Notification, Stop, SessionEnd.

Each event is appended as one JSON line to ~/.cadre/events.jsonl, which the dashboard reads
(cadre/events.py), so events are kept even while the dashboard isn't running.

Guarantees (this script must never disturb the agent):
  * never prints anything (stdout of some hooks is injected into the agent's context)
  * always exits 0, whatever happens
  * records only short summaries - never file contents, tool output or full prompts
  * no network by default; with CADRE_URL set it also POSTs there, giving up after 0.5s

Off switch: set CADRE_HOOKS=0 in the environment, or create ~/.cadre/paused (the menu bar
app's "Pause" does that; delete the file to resume).
Log dir:    CADRE_HOME (default ~/.cadre)
"""
import json
import os
import re
import sys
import time
import urllib.request

URL = os.environ.get("CADRE_URL")
LOG_DIR = os.environ.get("CADRE_HOME") or os.path.join(os.path.expanduser("~"), ".cadre")
LOG_FILE = os.path.join(LOG_DIR, "events.jsonl")
PAUSED = os.path.join(LOG_DIR, "paused")   # created by "Pause" in the menu bar app
LOG_MAX_BYTES = 20 * 1024 * 1024      # same rotation rule as cadre/events.py
MAX_IN = 4 * 1024 * 1024

# agent-specific event name -> cADRe event name (anything else: first letter upper-cased)
EVENT_MAP = {
    "gemini": {"BeforeAgent": "UserPromptSubmit", "AfterTool": "PostToolUse", "AfterAgent": "Stop"},
    "copilot": {"userPromptSubmitted": "UserPromptSubmit", "agentStop": "Stop",
                "errorOccurred": "ErrorOccurred"},
    "cursor": {"beforeSubmitPrompt": "UserPromptSubmit", "stop": "Stop"},
    "windsurf": {"pre_user_prompt": "UserPromptSubmit", "post_cascade_response": "Stop"},
}
# Copilot notifications that aren't "waiting for you"
COPILOT_DONE = {"agent_completed", "agent_idle"}
COPILOT_INFO = {"shell_completed", "shell_detached_completed"}


# Secrets are replaced by "[redacted:<kind>]" before anything is written, so the log never holds
# them; cADRe's "secret" detection rule looks for these markers. A group named v marks the part
# to replace (the rest is kept). Same table as cadre/detect/rules/secrets.py (a test checks).
SECRETS = [
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|[\s\S]*)"),
    ("aws-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("api-key", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("google-key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("password", r"(?i)(?:(?<![\w:.])(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[=:]\s*"
                 r"|--(?:password|token|api-key|secret)[= ])['\"]?(?P<v>(?![$%<{\[|(])[^\s'\"|`,;)]{8,})"),
]
_SECRETS = [(k, re.compile(p)) for k, p in SECRETS]


def redact(s):
    for kind, rx in _SECRETS:
        mark = "[redacted:%s]" % kind
        s = rx.sub(lambda m: m.group(0)[: m.start("v") - m.start()] + mark + m.group(0)[m.end("v") - m.start():]
                   if "v" in rx.groupindex else mark, s)
    return s


def short(v, n):
    s = " ".join(redact(str(v or "")).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def clip(v, n):
    """Like short(), but keeps line breaks (a command's heredoc must stay recognisable)."""
    s = redact(str(v or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_input(inp):
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            return inp
    return inp


def summarise_tool(inp):
    inp = _tool_input(inp)
    if isinstance(inp, str):
        return short(inp, 160)
    if not isinstance(inp, dict):
        return ""
    for k in ("description", "command", "command_line", "file_path", "filePath", "path",
              "notebook_path", "pattern", "url", "query", "skill", "mcp_tool_name"):
        if inp.get(k):
            return short(inp[k], 160)
    return ""


def tool_target(inp):
    """(command, path) of a tool call, for cADRe's detection rules: the shell command as run and
    the file it touched. Both short (300 chars) and with secrets redacted, like every summary."""
    inp = _tool_input(inp)
    if not isinstance(inp, dict):
        return None, None
    cmd = first(inp.get("command"), inp.get("command_line"), inp.get("cmd"))
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    path = first(inp.get("file_path"), inp.get("filePath"), inp.get("path"), inp.get("notebook_path"))
    return (clip(cmd, 300) if isinstance(cmd, str) and cmd else None,
            short(path, 300) if isinstance(path, str) and path else None)


def arg(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None


def first(*vals):
    return next((v for v in vals if v), None)


def build(p, agent, event):
    raw = event or p.get("hook_event_name") or p.get("agent_action_name") or ""
    name = EVENT_MAP.get(agent, {}).get(raw) or (raw[:1].upper() + raw[1:])
    # Devin Desktop/CLI also reads ~/.claude/settings.json, so Claude Code hooks fire there too
    if agent == "claude-code" and os.environ.get("DEVIN_PROJECT_DIR"):
        agent = "devin"
    roots = p.get("workspace_roots") if isinstance(p.get("workspace_roots"), list) else []
    info = p.get("tool_info") if isinstance(p.get("tool_info"), dict) else {}
    ev = {
        "ts": time.time(),
        "agent": agent,
        "event": name,
        "session_id": first(p.get("session_id"), p.get("sessionId"), p.get("conversation_id"),
                            p.get("trajectory_id"), os.environ.get("GEMINI_SESSION_ID")),
        "cwd": first(p.get("cwd"), roots[0] if roots else None, info.get("cwd"),
                     os.environ.get("DEVIN_PROJECT_DIR"), os.environ.get("GEMINI_CWD")),
        "transcript_path": first(p.get("transcript_path"), p.get("transcriptPath")),
        "permission_mode": p.get("permission_mode"),
    }
    if agent == "windsurf" and raw.startswith("post_") and raw != "post_cascade_response":
        ev["event"] = "PostToolUse"
        ev["tool"] = raw[len("post_"):]
        ev["detail"] = summarise_tool(info)
        ev["command"], ev["path"] = tool_target(info)
    elif name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        ev["tool"] = first(p.get("tool_name"), p.get("toolName")) or "tool"
        ev["detail"] = summarise_tool(first(p.get("tool_input"), p.get("toolArgs")))
        ev["command"], ev["path"] = tool_target(first(p.get("tool_input"), p.get("toolArgs")))
        if name == "PostToolUseFailure":
            ev["failed"] = True
    elif name == "UserPromptSubmit":
        ev["detail"] = short(first(p.get("prompt"), info.get("user_prompt")), 200)
    elif name == "Notification":
        ev["detail"] = short(first(p.get("message"), p.get("title")), 200)
        ev["notification_type"] = p.get("notification_type")
        if agent == "copilot" and p.get("notification_type") in COPILOT_DONE:
            ev["event"] = "Stop"
        elif agent == "copilot" and p.get("notification_type") in COPILOT_INFO:
            ev["event"] = "Info"
    elif name in ("SessionStart", "SessionEnd"):
        ev["detail"] = short(first(p.get("source"), p.get("reason")), 200) or None
    elif name == "ErrorOccurred":
        ev["detail"] = short(first(p.get("message"), p.get("error")), 200)
        ev["failed"] = True
    if not ev["session_id"]:
        ev["session_id"] = "%s:%s" % (agent, ev["cwd"] or "unknown")
    return {k: v for k, v in ev.items() if v is not None}


LOCK_WAIT = 0.1                       # longest a write waits for the Windows lock (seconds)


def _lock(path):
    """A short lock on <log>.lock. Windows takes it for every write: O_APPEND is a seek followed
    by a write there, so hooks firing together could overwrite each other's lines. POSIX appends
    are atomic, so there it is only taken to rotate (see _full). Never blocks: it retries for
    LOCK_WAIT at most, then gives up (None) and the caller writes anyway, because a hook must
    not stall its agent."""
    try:
        if os.name == "nt":
            import msvcrt
            take = lambda fd: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            take = lambda fd: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except (ImportError, OSError):
        return None
    end = time.monotonic() + LOCK_WAIT
    while True:
        try:
            take(fd)
            return fd
        except OSError:
            if time.monotonic() >= end:
                os.close(fd)
                return None
        # yield, don't sleep: the lock is held for well under a millisecond, and a real sleep
        # here (1-15 ms on Windows) let busy writers starve a waiting one past the cap
        time.sleep(0)


def _unlock(fd):
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    os.close(fd)


def _full(path):
    """The log is due to rotate. Writers can see that at the same moment: only the first may
    move it, or the next one moves the fresh log over the first one's .1 and a whole log is
    lost. So the writer takes the lock and asks again before it rotates."""
    try:
        return os.path.getsize(path) > LOG_MAX_BYTES
    except OSError:
        return False


def append(ev):
    """Append one event as a single write. The log rotates here too, under the same lock (same
    code as cadre/events.py: this script can't import the package)."""
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
    lock = _lock(LOG_FILE) if os.name == "nt" or _full(LOG_FILE) else None
    try:
        # only with the lock in hand: the log can fill up between the two looks, and a writer
        # that waited too long for the lock has none (two unlocked rotations lose a whole log)
        if (lock is not None or os.name == "nt") and _full(LOG_FILE):
            try:
                os.replace(LOG_FILE, LOG_FILE + ".1")
            except OSError:
                pass
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    finally:
        _unlock(lock)


def main():
    if os.environ.get("CADRE_HOOKS") == "0" or os.path.exists(PAUSED):
        return
    # utf-8-sig: a payload piped through PowerShell can start with a BOM, which json rejects
    payload = json.loads(sys.stdin.buffer.read(MAX_IN).decode("utf-8-sig", "replace") or "{}")
    if not isinstance(payload, dict):
        payload = {}
    ev = build(payload, arg("--agent") or "claude-code", arg("--event"))
    try:
        append(ev)
    finally:
        if URL:                         # optional: also push to a dashboard elsewhere
            req = urllib.request.Request(URL, data=json.dumps(ev).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=0.5).close()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
