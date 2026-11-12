"""False positives: findings the user has said are fine. They are taken off the agent (card,
risk banner, Risk flags, police droid, tray), off the Alerts tab and out of the explorer's
"Flagged by a rule", and they don't alert. Marking one, and taking the mark back, are both
written to the response log.

What a mark covers depends on the rule's kind (cadre/detect):
  * an event rule (a command, a path, a secret): that exact finding in that session, i.e. the
    same rule with the same detail. `rm -rf build` marked fine leaves `rm -rf ~` flagged.
  * a session or record rule (a loop, many child processes, permissions bypassed): the rule
    for that session or agent, whatever the detail (a CPU reading changes every time).

"who" is the session id, or the agent's record id when it has no session.

Marks are kept in ~/.cadre/dismissed.json (the response log is the history, this file the
state). Detection itself is untouched: a mark hides a finding, it never changes a rule, and
taking it back shows the finding again.
"""
import json
import os
import time

from .. import detect, events
from . import audit

NAME = "dismissed.json"
MAX = 2000              # marks kept; the oldest go first
CHECK_EVERY = 1.0       # seconds between looks at the file (another process may have changed it)

_state = {"at": 0.0, "stamp": None, "items": [], "keys": set()}


def path():
    return os.path.join(events.LOG_DIR, NAME)


def _exact(rule):
    r = detect.RULES.get(rule)
    return bool(r and r.kind == "event")


def _key(who, rule, detail):
    return (who, rule, (detail or "") if _exact(rule) else "")


def _load(force=False):
    now = time.time()
    if not force and now - _state["at"] < CHECK_EVERY:
        return _state
    _state["at"] = now
    file = path()
    try:
        st = os.stat(file)
        stamp = (file, st.st_size, st.st_mtime)
    except OSError:
        stamp = (file, None, None)
    if stamp == _state["stamp"]:
        return _state
    items = []
    if stamp[1] is not None:
        try:
            with open(file, encoding="utf-8") as f:
                raw = json.load(f)
            items = [d for d in raw if isinstance(d, dict) and d.get("who") and d.get("rule")]
        except (OSError, ValueError, TypeError):
            items = []
    _state.update(stamp=stamp, items=items, keys={_key(d["who"], d["rule"], d.get("detail")) for d in items})
    return _state


def _save(items):
    file = path()
    os.makedirs(os.path.dirname(file), mode=0o700, exist_ok=True)
    tmp = file + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # private, like the event log
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(items[-MAX:], f, ensure_ascii=False, indent=1)
    os.replace(tmp, file)
    _load(force=True)


def active(limit=200):
    """The marks in force, newest first."""
    return _load()["items"][::-1][:limit]


def is_dismissed(whos, finding):
    keys = _load()["keys"]
    return bool(keys) and any(_key(w, finding.get("rule"), finding.get("detail")) in keys for w in whos if w)


def _split(whos, findings):
    kept, gone = [], []
    for f in findings:
        (gone if is_dismissed(whos, f) else kept).append(f)
    return kept, gone


def apply_record(rec):
    """Move an agent record's dismissed findings from rec["findings"] to rec["dismissed"] and
    redo rec["flags"]. Called right after detect.review_record()."""
    if not (_load()["keys"] and rec.get("findings")):
        return rec
    kept, gone = _split((rec.get("session_id"), rec.get("id")), rec["findings"])
    if gone:
        rec["findings"], rec["dismissed"] = kept, gone
        rec["flags"] = [detect.flag_text(f) + (" ×%d" % f["count"] if f.get("count", 1) > 1 else "") for f in kept]
    return rec


def apply_event(ev):
    """The event with its dismissed findings moved to "dismissed". A copy when anything
    changes: events in the store are shared and keep every finding."""
    if not (_load()["keys"] and ev.get("findings")):
        return ev
    kept, gone = _split((ev.get("session_id"),), ev["findings"])
    if not gone:
        return ev
    out = dict(ev, dismissed=gone)
    if kept:
        out["findings"] = kept
    else:
        del out["findings"]
    return out


def mark(snap, who, rule, detail="", restore=False, agent=None, project=None, source="dashboard", now=None):
    """Mark a finding as a false positive (or take the mark back) and log it. Returns the
    log entry. ValueError = a request that makes no sense (nothing changed, nothing logged)."""
    now = now or time.time()
    r = detect.RULES.get(rule) if isinstance(rule, str) else None
    if not r:
        raise ValueError("no such rule: %s" % (rule,))
    if not (isinstance(who, str) and 0 < len(who) <= 200):
        raise ValueError("who expected: a session id or an agent id")
    if not isinstance(detail, str) or len(detail) > 600:
        raise ValueError("detail must be a short string")
    detail = detail if r.kind == "event" else ""
    rec = next((a for a in (snap or {}).get("agents") or []
                if who in (a.get("session_id"), a.get("id"))), None) or {}
    label = lambda v: str(v)[:80] if isinstance(v, str) and v else None
    text = r.title + (" (%s)" % detail if detail else "")
    items = list(_load(force=True)["items"])
    key = _key(who, rule, detail)
    there = [d for d in items if _key(d["who"], d["rule"], d.get("detail")) == key]
    entry = {"ts": now, "action": "restore" if restore else "dismiss", "source": source, "ok": False,
             "result": "", "agent_id": rec.get("id"),
             "agent": rec.get("display_name") or rec.get("agent") or label(agent),
             "agent_key": rec.get("agent_key"), "project": rec.get("project") or label(project),
             "session_id": rec.get("session_id"), "who": who, "rule": rule, "detail": detail,
             "severity": r.severity, "flags": [text]}
    try:
        if restore and there:
            _save([d for d in items if d not in there])
            entry.update(ok=True, result="flag restored: %s" % text)
        elif restore:
            entry["result"] = "it wasn't marked as a false positive"
        elif there:
            entry["result"] = "it is already marked as a false positive"
        else:
            _save(items + [{"who": who, "rule": rule, "detail": detail, "ts": now, "title": r.title,
                            "severity": r.severity, "agent": entry["agent"], "project": entry["project"]}])
            entry.update(ok=True, result="marked as a false positive: %s" % text)
    except OSError as e:
        entry["result"] = "couldn't save it: %s" % e
    try:
        audit.append(entry)
    except OSError:
        entry["result"] += " (not logged: the response log can't be written)"
    return entry
