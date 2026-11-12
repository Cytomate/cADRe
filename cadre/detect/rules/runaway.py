"""Runaway behaviour, judged over one session's recent events (oldest first).

  failing-loop   the same tool call failing again and again
  repeat-loop    the same command run many times in a short while
  tool-burst     far more tool calls per minute than a person could follow

Thresholds are module constants so they are easy to tune.
"""
from collections import Counter

from ..engine import session_rule
from ..helpers import command_of, path_of

LOOP_WINDOW = 600          # seconds
FAIL_REPEATS = 3
SAME_REPEATS = 10
BURST_WINDOW = 60
BURST_CALLS = 40


def _key(e):
    return (e.get("tool") or "", command_of(e) or path_of(e) or e.get("detail") or "")


def _tools(events):
    return [e for e in events if e.get("tool")]


def _recent(events, window):
    if not events:
        return []
    end = max(e.get("ts") or 0 for e in events)
    return [e for e in events if (e.get("ts") or 0) >= end - window]


def _say(key, n):
    tool, what = key
    s = "%d× %s" % (n, what or tool)
    return s if len(s) <= 80 else s[:79] + "…"


def _calls(n, ts0=1000.0, step=1.0, **kw):
    return [dict({"ts": ts0 + i * step, "tool": "Bash"}, **kw) for i in range(n)]


@session_rule("failing-loop", "runaway", "warn", "keeps retrying a failing call",
              hits=[_calls(3, command="npm test", failed=True)],
              misses=[_calls(2, command="npm test", failed=True),
                      _calls(3, command="npm test"),
                      _calls(3, step=400, command="npm test", failed=True)])
def failing_loop(events):
    """The same tool call (tool + command or file) failed FAIL_REPEATS times in LOOP_WINDOW."""
    c = Counter(_key(e) for e in _recent(_tools(events), LOOP_WINDOW) if e.get("failed"))
    return [_say(k, n) for k, n in c.most_common() if n >= FAIL_REPEATS]


@session_rule("repeat-loop", "runaway", "warn", "runs the same command over and over",
              hits=[_calls(10, command="git status")],
              misses=[_calls(9, command="git status"),
                      [dict(e, command="ls %d" % i) for i, e in enumerate(_calls(20))]])
def repeat_loop(events):
    """The same shell command SAME_REPEATS times in LOOP_WINDOW."""
    c = Counter(command_of(e) for e in _recent(_tools(events), LOOP_WINDOW) if command_of(e))
    return [_say(("", k), n) for k, n in c.most_common() if n >= SAME_REPEATS]


@session_rule("tool-burst", "runaway", "warn", "tool calls faster than you can follow",
              hits=[[dict(e, command="x%d" % i) for i, e in enumerate(_calls(40, step=1.0))]],
              misses=[[dict(e, command="x%d" % i) for i, e in enumerate(_calls(40, step=2.0))]])
def tool_burst(events):
    """BURST_CALLS tool calls within BURST_WINDOW seconds (sliding window)."""
    ts = sorted(e.get("ts") or 0 for e in _tools(events))
    best, j = 0, 0
    for i, t in enumerate(ts):
        while t - ts[j] > BURST_WINDOW:
            j += 1
        best = max(best, i - j + 1)
    return "%d tool calls in %d s" % (best, BURST_WINDOW) if best >= BURST_CALLS else None
