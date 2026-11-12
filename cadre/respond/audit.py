"""The response log: one line in ~/.cadre/responses.jsonl for every Pause, Resume and Kill,
whether it worked or not. Only the dashboard writes it, so it needs no lock.

An entry: ts, action, source, ok, result (one sentence), agent_id, agent, agent_key, project,
session_id, pid, pids (every process acted on), started (the process's start time) and flags
(what the agent was flagged for at that moment).
"""
import json
import os

from .. import events

NAME = "responses.jsonl"
READ_BYTES = 256 * 1024

_cache = (None, [])      # ((path, size, mtime), entries): the snapshot asks every few seconds


def path():
    return os.path.join(events.LOG_DIR, NAME)


def append(entry, file=None):
    file = file or path()
    os.makedirs(os.path.dirname(file), mode=0o700, exist_ok=True)
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)   # as events.append
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def entries(limit=200, file=None):
    """The last `limit` entries, oldest first. Lines that don't parse are skipped."""
    global _cache
    file = file or path()
    try:
        st = os.stat(file)
    except OSError:
        return []
    key = (file, st.st_size, st.st_mtime)
    if _cache[0] != key:
        out = []
        try:
            with open(file, "rb") as f:
                f.seek(max(0, st.st_size - READ_BYTES))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("action"):
                out.append(e)
        _cache = (key, out)
    return _cache[1][-limit:]
