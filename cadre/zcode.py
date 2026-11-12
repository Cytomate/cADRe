"""Read-only reader for ZCode (Z.ai's agent app) sessions in ~/.zcode/cli/db/db.sqlite.

Only reads; opens the database with mode=ro so ZCode is never disturbed.
Timestamps in the database are milliseconds.
"""
import json
import os
import pathlib
import sqlite3
import time

ZCODE_DB = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
ZCODE_WORKSPACES = os.path.expanduser("~/.zcode/workspace")
RECENT_WINDOW = 24 * 3600
DESTRUCTIVE_WINDOW = 3600


def _ro_uri():
    # file:///C:/... on Windows, file:///Users/... elsewhere; read-only
    return pathlib.Path(ZCODE_DB).as_uri() + "?mode=ro"


def _short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def project_name(directory):
    if not directory:
        return None
    if directory.startswith(ZCODE_WORKSPACES):
        return "ZCode workspace"
    return os.path.basename(directory.rstrip("/")) or directory


def _one(c, sql, args):
    r = c.execute(sql, args).fetchone()
    return r if r else None


def _tool_detail(inp):
    return inp.get("description") or inp.get("command") or inp.get("file_path") \
        or inp.get("filePath") or inp.get("pattern") or inp.get("url") or ""


ACTIVE_WRITE_S = 8          # messages/parts written this recently => working
STALE_TURN_S = 600          # unanswered prompt with no writes for this long => crashed/cancelled


def _turn_in_progress(c, sid, turn, prompt_ms, now):
    """Return the last write time (s) if a turn is in flight, else None.

    ZCode only gives turn_usage its final status ('completed'/'error'), never 'running',
    so infer it: an unanswered prompt, an assistant message not yet completed, or fresh writes.
    """
    w = _one(c, "select max(t) t from (select max(time_updated) t from message where session_id=? "
                "union all select max(time_updated) from part where session_id=?)", (sid, sid))
    last_write = (w["t"] or 0) / 1000.0 if w else 0
    if not last_write:
        return None
    if now - last_write < ACTIVE_WRITE_S:
        return last_write
    if turn and turn["status"] == "running":
        return last_write
    if now - last_write > STALE_TURN_S:
        return None
    done = (turn and turn["completed_at"]) or 0
    if prompt_ms and prompt_ms > done:
        return last_write
    m = _one(c, "select data from message where session_id=? order by time_created desc limit 1", (sid,))
    if m:
        try:
            d = json.loads(m["data"])
            if d.get("role") == "assistant" and not (d.get("time") or {}).get("completed"):
                return last_write
        except ValueError:
            pass
    return None


def read_events(limit=40):
    """Recent ZCode activity as feed events, shaped like hook events (newest first).

    ZCode can't run hooks, so prompts, tool calls and finished turns come from its DB.
    """
    if not os.path.exists(ZCODE_DB):
        return []
    since = int((time.time() - RECENT_WINDOW) * 1000)
    out = []
    try:
        c = sqlite3.connect(_ro_uri(), uri=True, timeout=1)
        c.row_factory = sqlite3.Row
        dirs = {r["id"]: r["directory"] for r in c.execute("select id, directory from session")}

        def ev(ts_ms, event, sid, **kw):
            e = {"ts": ts_ms / 1000.0, "event": event, "session_id": sid, "cwd": dirs.get(sid),
                 "project": project_name(dirs.get(sid)), "agent": "ZCode", "source": "zcode-db"}
            e.update({k: v for k, v in kw.items() if v})
            out.append(e)

        for r in c.execute("select session_id, text, time_created from input_history where kind='prompt' "
                           "and time_created > ? order by time_created desc limit ?", (since, limit)):
            ev(r["time_created"], "UserPromptSubmit", r["session_id"], detail=_short(r["text"], 200))
        for r in c.execute("select session_id, data, time_created from part where data like '%\"type\":\"tool\"%' "
                           "and time_created > ? order by time_created desc limit ?", (since, limit)):
            try:
                p = json.loads(r["data"])
            except ValueError:
                continue
            st = p.get("state") or {}
            ev(r["time_created"], "PostToolUse", r["session_id"], tool=p.get("tool"),
               detail=_short(_tool_detail(st.get("input") or {}), 200), failed=st.get("status") == "error")
        for r in c.execute("select session_id, status, completed_at, tool_call_count from turn_usage "
                           "where completed_at > ? order by completed_at desc limit ?", (since, limit)):
            n = r["tool_call_count"] or 0
            ev(r["completed_at"], "Stop", r["session_id"], failed=r["status"] not in ("completed", "running"),
               detail="turn %s%s" % (r["status"], " · %d tool call(s)" % n if n else ""))
        c.close()
    except sqlite3.Error:
        return []
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out[:limit]


def read_sessions(limit=20):
    """Return recent ZCode sessions (newest first), [] if ZCode isn't installed or the DB is busy."""
    if not os.path.exists(ZCODE_DB):
        return []
    now = time.time()
    try:
        c = sqlite3.connect(_ro_uri(), uri=True, timeout=1)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "select id, directory, title, time_updated from session "
            "where time_deleted is null and time_archived is null and parent_id is null "
            "order by time_updated desc limit ?", (limit,)).fetchall()
        out = []
        for s in rows:
            sid = s["id"]
            turn = _one(c, "select status, started_at, completed_at, tool_call_count from turn_usage "
                           "where session_id=? order by started_at desc limit 1", (sid,))
            model = _one(c, "select model_id, provider_id from model_usage where session_id=? "
                            "order by started_at desc limit 1", (sid,))
            prompt = _one(c, "select text from input_history where session_id=? and kind='prompt' "
                             "order by time_created desc limit 1", (sid,))
            pending = _one(c, "select tool_name from tool_usage where session_id=? and status='running' "
                              "and approval_status not in ('none','approved','auto','granted') "
                              "order by started_at desc limit 1", (sid,))
            destructive = c.execute(
                "select count(*) from tool_usage where session_id=? and destructive=1 and started_at > ?",
                (sid, int((now - DESTRUCTIVE_WINDOW) * 1000))).fetchone()[0]
            last_tool = None
            part = _one(c, "select data from part where session_id=? and data like '%\"type\":\"tool\"%' "
                           "order by time_created desc limit 1", (sid,))
            if part:
                try:
                    p = json.loads(part["data"])
                    inp = (p.get("state") or {}).get("input") or {}
                    detail = inp.get("description") or inp.get("command") or inp.get("file_path") \
                        or inp.get("filePath") or inp.get("pattern") or inp.get("url") or ""
                    last_tool = _short("%s: %s" % (p.get("tool"), detail) if detail else p.get("tool"), 140)
                except ValueError:
                    pass
            last_activity = max(filter(None, [
                s["time_updated"], turn and turn["completed_at"], turn and turn["started_at"]])) / 1000.0
            in_progress = _turn_in_progress(c, sid, turn, prompt_ms=_one(
                c, "select max(time_created) t from input_history where session_id=? and kind='prompt'",
                (sid,))["t"], now=now)
            if in_progress:
                last_activity = max(last_activity, in_progress)
            out.append({
                "session_id": sid,
                "cwd": s["directory"],
                "project": project_name(s["directory"]),
                "title": _short(s["title"], 120),
                "last_activity": last_activity,
                "turn_running": bool(in_progress),
                "waiting_for": ("approve %s" % pending["tool_name"]) if pending else None,
                "model": model["model_id"] if model else None,
                "provider": model["provider_id"] if model else None,
                "last_prompt": _short(prompt["text"], 240) if prompt else None,
                "last_tool": last_tool,
                "destructive_recent": destructive,
                "recent": now - last_activity < RECENT_WINDOW,
            })
        c.close()
        return out
    except sqlite3.Error:
        return []
