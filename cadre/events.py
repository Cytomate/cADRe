"""Hook events: an append-only log that the hooks write and the dashboard reads.

Every hook (cadre/hooks/emit.py) appends one JSON line to ~/.cadre/events.jsonl, whether or
not the dashboard is running, so nothing is lost while it is down. The dashboard keeps the
recent events in memory: on start it loads the tail of the log, and before every snapshot it
reads the lines added since (sync()). Old hooks that still POST to /api/events are handled by
add(), which appends to the same log, so every event takes the same path.

CADRE_HOME relocates ~/.cadre (hooks and dashboard must agree; used for tests).
"""
import collections
import json
import os
import threading
import time

from .detect import review_event

LOG_DIR = os.environ.get("CADRE_HOME") or os.path.join(os.path.expanduser("~"), ".cadre")
LOG_FILE = os.path.join(LOG_DIR, "events.jsonl")
LOG_MAX_BYTES = 20 * 1024 * 1024          # then events.jsonl -> events.jsonl.1 (same rule in emit.py)
LOAD_BYTES = 512 * 1024                   # history loaded at start
FIELDS = ("ts", "agent", "event", "session_id", "cwd", "transcript_path", "permission_mode",
          "tool", "detail", "failed", "notification_type", "command", "path")


def clean(raw):
    """Keep the known fields, cap string lengths; None if it isn't a usable event."""
    if not isinstance(raw, dict):
        return None
    ev = {k: raw.get(k) for k in FIELDS if raw.get(k) is not None}
    for k in ("detail", "tool", "event", "cwd", "agent", "command", "path"):
        if isinstance(ev.get(k), str):
            ev[k] = ev[k][:300]
    if not isinstance(ev.get("ts"), (int, float)):
        ev["ts"] = time.time()
    if not ev.get("event") or not ev.get("session_id"):
        return None
    return ev


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


def append(ev, path=None):
    """Append one event as a single write. The log rotates here too, under the same lock (same
    code as cadre/hooks/emit.py, which can't import this package)."""
    path = path or LOG_FILE
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
    lock = _lock(path) if os.name == "nt" or _full(path) else None
    try:
        # only with the lock in hand: the log can fill up between the two looks, and a writer
        # that waited too long for the lock has none (two unlocked rotations lose a whole log)
        if (lock is not None or os.name == "nt") and _full(path):
            try:
                os.replace(path, path + ".1")
            except OSError:
                pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    finally:
        _unlock(lock)


KINDS = {                                 # the explorer's "kind" filter
    "tool": lambda e: bool(e.get("tool")),
    "prompt": lambda e: e.get("event") == "UserPromptSubmit",
    "waiting": lambda e: e.get("event") in ("Notification", "PermissionRequest"),
    "session": lambda e: e.get("event") in ("SessionStart", "SessionEnd", "Stop", "SubagentStop"),
    "failed": lambda e: bool(e.get("failed")),
    "flagged": lambda e: bool(e.get("findings")),      # a detection rule fired (cadre/detect)
}


def matcher(q=None, agent=None, kind=None, project=None):
    """A filter for history(); None when nothing is filtered."""
    q = (q or "").strip().lower()
    project = (project or "").strip().lower()
    test = KINDS.get(kind or "")
    if not (q or agent or test or project):
        return None

    def match(e):
        if agent and e.get("agent") != agent:
            return False
        if test and not test(e):
            return False
        if project:
            cwd = (e.get("cwd") or "").replace("\\", "/").rstrip("/").lower()
            if (e.get("project") or "").lower() != project and cwd.rsplit("/", 1)[-1] != project:
                return False
        if q and q not in " ".join(str(e.get(k) or "") for k in ("event", "tool", "detail", "cwd", "agent")).lower():
            return False
        return True
    return match


def history(before=None, limit=100, match=None, path=None):
    """Logged events older than `before` (a timestamp), newest first: (events, more_left).
    Reads the log and the rotated .1 file backwards, so the explorer can page through
    everything the hooks recorded, not just what the dashboard holds in memory."""
    path = path or LOG_FILE
    out = []
    for p in (path, path + ".1"):
        try:
            with open(p, "rb") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                ev = clean(json.loads(line))
            except ValueError:
                continue
            if ev:
                review_event(ev)
            if not ev or (before is not None and ev["ts"] >= before) or (match and not match(ev)):
                continue
            if len(out) == limit:
                return out, True
            out.append(ev)
    return out, False


FLAGGED_KEEP = 50          # flagged events remembered per session ...
FLAGGED_SESSIONS = 200     # ... for this many sessions (the ones flagged last)


class EventStore:
    def __init__(self, path=None, maxlen=500):
        self.path = path or LOG_FILE
        self.lock = threading.Lock()
        self.recent = collections.deque(maxlen=maxlen)
        self.by_session = {}          # session_id -> latest event
        self.flagged = collections.OrderedDict()   # session_id -> its flagged events, kept when
                                                   # they have left `recent` (see session_events)
        self.tool_count = collections.Counter()
        self.total = 0
        self.offset = None            # bytes of the log already read; None = not loaded yet

    def _ingest(self, ev):
        ev.setdefault("received", ev["ts"])
        review_event(ev)
        self.recent.append(ev)
        self.by_session[ev["session_id"]] = ev
        if ev.get("findings"):
            self.flagged.setdefault(ev["session_id"], collections.deque(maxlen=FLAGGED_KEEP)).append(ev)
            self.flagged.move_to_end(ev["session_id"])
            while len(self.flagged) > FLAGGED_SESSIONS:
                self.flagged.popitem(last=False)
        if ev.get("tool"):
            self.tool_count[ev["session_id"]] += 1
        self.total += 1

    def sync(self):
        """Read events appended to the log since the last call (the tail of it on the first)."""
        with self.lock:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                return
            if self.offset is None:
                start, skip_partial = max(0, size - LOAD_BYTES), size > LOAD_BYTES
            else:
                start, skip_partial = (0, False) if size < self.offset else (self.offset, False)  # rotated
            if size == start:
                self.offset = size
                return
            try:
                with open(self.path, "rb") as f:
                    f.seek(start)
                    data = f.read(size - start)
            except OSError:
                return
            end = data.rfind(b"\n") + 1            # only complete lines; the rest next time
            consumed = data[:end]
            if skip_partial:
                consumed = consumed[consumed.find(b"\n") + 1:]
            for line in consumed.decode("utf-8", "replace").splitlines():
                try:
                    ev = clean(json.loads(line))
                except ValueError:
                    continue
                if ev:
                    self._ingest(ev)
            self.offset = start + end

    def add(self, raw):
        """An event POSTed by an older hook: log it like a new hook would, then read it in."""
        ev = clean(raw)
        if not ev:
            return False
        try:
            append(ev, self.path)
        except OSError:
            with self.lock:                       # can't write the log: keep it in memory at least
                ev["received"] = time.time()
                self._ingest(ev)
            return True
        self.sync()
        return True

    def fresh_session(self, agent, same_dir, since, claimed=()):
        """The newest session of `agent` that started in a folder (same_dir(cwd) is True) at or
        after `since` and has no transcript on disk yet: a session started a moment ago, before
        its first prompt (Claude Code writes the transcript only then). None if there is none."""
        with self.lock:
            evs = list(self.recent)
        for ev in reversed(evs):
            if ev["ts"] < since:
                break
            sid = ev["session_id"]
            if ev.get("agent") != agent or sid in claimed or not same_dir(ev.get("cwd")):
                continue
            t = ev.get("transcript_path")
            if t and os.path.exists(os.path.expanduser(t)):
                continue
            return sid
        return None

    def session_events(self, session_id, since=0):
        """This session's events still in memory (oldest first), for the detection rules: the
        recent ones, plus its flagged ones however many events came after them. A running
        agent keeps a risk flag until the user decides about it, and on a busy machine the
        newest 500 events are a few minutes."""
        if not session_id:
            return []
        with self.lock:
            out = [e for e in self.recent if e["session_id"] == session_id and e["ts"] >= since]
            have = {id(e) for e in out}
            out += [e for e in self.flagged.get(session_id, ()) if id(e) not in have and e["ts"] >= since]
        out.sort(key=lambda e: e["ts"])
        return out

    def latest(self, session_id):
        with self.lock:
            return self.by_session.get(session_id)

    def tail(self, n=40):
        with self.lock:
            return list(self.recent)[-n:][::-1]


STORE = EventStore()
