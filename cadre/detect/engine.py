"""The rule engine: what a rule is, the registry, and the three ways rules are run.

A rule is a small function registered with one of three decorators, by what it looks at:

    @event_rule(...)    one hook event          fn(ev)          e.g. a dangerous shell command
    @session_rule(...)  one session's events    fn(events)      e.g. the same command failing in a loop
    @record_rule(...)   one live agent record   fn(rec)         e.g. permissions bypassed, high CPU

The function returns what it found: None/False/"" for nothing, True, a short string saying
what matched (shown after the title), or a list of such strings. The engine turns that into
Finding dicts, catches any exception a rule raises (a broken rule must never break the
dashboard), and attaches the rule's id, severity, category and title.

Every rule also carries `hits` (inputs it must flag) and `misses` (inputs it must not);
tests/test_detect.py runs them for every registered rule, so a rule documents and tests itself.

Severities: "info" (a risky setup, shown on the agent only), "warn" and "high" (the tracker
goes after these: police droid, Risk flags total, red marks in the feed).
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List

SEVERITIES = ("info", "warn", "high")
KINDS = ("event", "session", "record")


@dataclass
class Rule:
    id: str                     # stable, kebab-case: "rm-rf", "sensitive-path"
    kind: str                   # event | session | record
    category: str               # commands | scope | secrets | runaway | process (= its rules/ file)
    severity: str
    title: str                  # what the user sees: "recursive force delete"
    fn: Callable
    hits: List = field(default_factory=list)
    misses: List = field(default_factory=list)
    doc: str = ""


RULES: Dict[str, Rule] = {}


def _register(kind, id, category, severity, title, hits=(), misses=()):
    if severity not in SEVERITIES:
        raise ValueError("%s: severity must be one of %s" % (id, SEVERITIES))

    def deco(fn):
        if id in RULES:
            raise ValueError("duplicate detection rule id: %s" % id)
        RULES[id] = Rule(id, kind, category, severity, title, fn, list(hits), list(misses),
                         (fn.__doc__ or "").strip())
        return fn
    return deco


def event_rule(id, category, severity, title, hits=(), misses=()):
    return _register("event", id, category, severity, title, hits, misses)


def session_rule(id, category, severity, title, hits=(), misses=()):
    return _register("session", id, category, severity, title, hits, misses)


def record_rule(id, category, severity, title, hits=(), misses=()):
    return _register("record", id, category, severity, title, hits, misses)


def _details(out):
    if not out:
        return []
    if out is True:
        return [""]
    if isinstance(out, str):
        return [out]
    return [str(x) for x in out if x]


def _run(kind, arg):
    found = []
    for r in RULES.values():
        if r.kind != kind:
            continue
        try:
            details = _details(r.fn(arg))
        except Exception:                  # a broken rule must never break the snapshot
            continue
        for d in details:
            found.append({"rule": r.id, "severity": r.severity, "category": r.category,
                          "title": r.title, "detail": d})
    return found


def check_event(ev):
    """Findings for one hook event (pure: the same event always gives the same findings)."""
    return _run("event", ev) if isinstance(ev, dict) else []


def check_session(events):
    """Findings over one session's events, oldest first (loops, bursts)."""
    return _run("session", list(events)) if events else []


def check_record(rec):
    """Findings for one live agent record (its process, folder and permission mode)."""
    return _run("record", rec)


def rank(severity):
    return SEVERITIES.index(severity) if severity in SEVERITIES else 0


def flag_text(f):
    """A finding as the one-line flag shown on agent cards, the menu bar and the tray."""
    return f["title"] + (" (%s)" % f["detail"] if f.get("detail") else "")
