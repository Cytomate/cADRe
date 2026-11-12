"""Which findings are news. Pure: no imports from the rest of cADRe, no I/O.

Detection works findings out again on every snapshot (cadre/detect never stores them), so
"this agent has a finding" is true for as long as the agent runs (an hour for a session without
a process). An alert is the moment one first
shows up: that is what the tray balloon, the browser notification and the dashboard toast are
for. AlertLog.update() is given every snapshot's records and returns the recent alerts,
newest first; clients remember the newest `ts` they have shown and notify only for later ones.

An alert is a dict: id, ts, agent_id, agent, project, rule, severity, title, detail, text.

Three rules keep it quiet:
  * "info" findings never alert (a risky setup, not an event).
  * The first snapshot after the dashboard starts only takes note: what was already flagged
    before it came up is not news.
  * One alert per agent and rule within QUIET seconds, so ten `rm -rf` in a row, or a CPU
    reading that flickers around its limit, is one notification.
"""
import collections

QUIET = 300          # seconds before the same rule on the same agent alerts again
KEEP = 50            # alerts kept for the snapshot


def _who(rec):
    """The session if there is one: a live agent (id = pid) that exits becomes a sleeping
    session with another id, and its findings must not alert a second time."""
    return rec.get("session_id") or rec.get("id")


def _text(f):
    return f.get("title", "") + (" (%s)" % f["detail"] if f.get("detail") else "")


class AlertLog:
    def __init__(self, quiet=QUIET, keep=KEEP):
        self.quiet = quiet
        self.recent = collections.deque(maxlen=keep)
        self.present = set()         # (who, rule, detail) in the last snapshot
        self.last = {}               # (who, rule) -> when it last alerted
        self.primed = False
        self.n = 0

    def update(self, records, now):
        """Take one snapshot's agent records; return the recent alerts, newest first."""
        present, fresh = set(), []
        for rec in records or []:
            who = _who(rec)
            for f in rec.get("findings") or []:
                if f.get("severity") == "info":
                    continue
                key = (who, f.get("rule"), f.get("detail") or "")
                present.add(key)
                if key in self.present or not self.primed:
                    continue
                if now - self.last.get(key[:2], -self.quiet) < self.quiet:
                    continue
                self.last[key[:2]] = now
                self.n += 1
                fresh.append({"id": "a%d-%d" % (int(now), self.n), "ts": now,
                              "agent_id": rec.get("id"), "agent": rec.get("display_name") or rec.get("agent"),
                              "project": rec.get("project"), "rule": f.get("rule"),
                              "severity": f.get("severity"), "title": f.get("title"),
                              "detail": f.get("detail") or "", "text": _text(f)})
        fresh.sort(key=lambda a: a["severity"] == "high")      # high last = newest = shown first
        self.recent.extend(fresh)
        self.present, self.primed = present, True
        self.last = {k: t for k, t in self.last.items() if now - t < self.quiet}
        return list(self.recent)[::-1]


ALERTS = AlertLog()
