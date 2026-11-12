"""Where detection meets the rest of cADRe. The only module outside detect/ calls into:

    review_event(ev)                       events.EventStore and events.history (feed, explorer)
    review_record(rec, session_events)     collectors.snapshot (agent cards, office, tray, menu bar)
    alerting(findings)                     findings the tracker goes after (not "info")

A finding is a dict: rule, severity, category, title, detail (+ ts and count on records).
"""
import time

from .engine import check_event, check_record, check_session, flag_text, rank

WINDOW = 3600          # a session that isn't running keeps the flags from its events of the
                       # last hour, and session rules (loops, bursts) look at that hour


def review_event(ev):
    """Set ev["findings"] (only when there are any). Returns ev."""
    f = check_event(ev)
    if f:
        ev["findings"] = f
    else:
        ev.pop("findings", None)
    return ev


def review_record(rec, session_events=(), now=None):
    """Set rec["findings"] and rec["flags"] (the one-line texts) for a live or sleeping agent:
    its process rules, the findings of its session's events (repeats merged, with a count)
    and the session rules.

    A running agent keeps the finding of an event for as long as it runs: the flag is a
    question to the user (pause, kill, false positive?), and it must not answer itself after
    an hour while the agent carries on. A session without a process keeps it for WINDOW."""
    now = now or time.time()
    every = sorted(session_events, key=lambda e: e.get("ts") or 0)
    events = [e for e in every if (e.get("ts") or 0) >= now - WINDOW]
    found = check_record(rec) if rec.get("pid") else []
    merged = {}
    for e in (every if rec.get("pid") else events):
        for f in e.get("findings") or check_event(e):
            k = (f["rule"], f["detail"])
            if k in merged:
                merged[k]["count"] += 1
                merged[k]["ts"] = e.get("ts")
            else:
                merged[k] = dict(f, ts=e.get("ts"), count=1)
    found += list(merged.values())
    found += check_session(events)
    found.sort(key=lambda f: (-rank(f["severity"]), -(f.get("ts") or now)))
    rec["findings"] = found
    rec["flags"] = [flag_text(f) + (" ×%d" % f["count"] if f.get("count", 1) > 1 else "") for f in found]
    return rec


def alerting(findings):
    """The findings worth the tracker's attention: everything but "info"."""
    return [f for f in findings or [] if f.get("severity") != "info"]
