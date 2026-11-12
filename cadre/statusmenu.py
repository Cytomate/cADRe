"""What the status icons show: the Windows tray (cadre/tray.py) and the Linux status icon
(cadre/indicator.py). Plain functions of a dashboard snapshot, importable everywhere (the
tests run them on every OS). The macOS menu bar (cadre/menubar.js) has the same rules in
JavaScript; tests/test_menubar.py checks that the two agree.
"""
import json
import os
import time
import urllib.error
import urllib.request

from .events import LOG_DIR

PAUSED = os.path.join(LOG_DIR, "paused")
EVERY = 3                                         # seconds between polls
MAX_ROWS = 12
GLYPH = {"working": "●", "waiting": "◆", "idle": "○", "suspended": "‖", "sleeping": "·"}

CMD_OPEN, CMD_PAUSE, CMD_START, CMD_STOP, CMD_QUIT_ICON, CMD_QUIT = 1, 2, 3, 4, 5, 6
CMD_AGENT = 100                                   # 100 + row: an agent row (opens the dashboard)


def paused():
    return os.path.exists(PAUSED)


def set_paused(on, by="the cADRe status icon"):
    """Create or remove ~/.cadre/paused (emit.py checks it). Returns an error text or None."""
    try:
        if on:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(PAUSED, "w", encoding="utf-8") as f:
                f.write("paused by %s\n" % by)
        elif os.path.exists(PAUSED):
            os.remove(PAUSED)
    except OSError as e:
        return str(e)
    return None


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy


def fetch(op, base):
    """(snapshot, error): (None, None) when nothing answers."""
    try:
        with op.open(base + "/api/snapshot", timeout=5) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %d" % e.code
    except (OSError, ValueError):
        return None, None


def status(snap, error, is_paused):
    """(icon key, tooltip): key on = watching, waiting = an agent needs you, off = the
    dashboard is down or hook events are paused."""
    c = (snap or {}).get("counts") or {}
    if not snap:
        tip = "cADRe: dashboard not running" + (" (%s)" % error if error else "")
    else:
        parts = ["%d %s" % (c[k], k) for k in ("working", "waiting", "idle", "suspended") if c.get(k)]
        tip = "cADRe: " + (", ".join(parts) if parts else "no agents running")
    if is_paused:
        tip += "\nhook events paused"
    key = "off" if not snap or is_paused else "waiting" if c.get("waiting") else "on"
    return key, tip[:127]


def menu_items(snap, error, is_paused, icon="tray icon"):
    """[(id or None, text, enabled)]; id None = separator, id 0 = a line of text.
    Mirrors menubar.js render()."""
    c = (snap or {}).get("counts") or {}
    rows = [(0, status(snap, error, is_paused)[1].split("\n")[0], False)]
    if snap:
        live = [a for a in snap.get("agents") or [] if a.get("status") != "sleeping"]
        for i, a in enumerate(live[:MAX_ROWS]):
            doing = "needs you" if a.get("status") == "waiting" else \
                a.get("last_tool") or a.get("last_prompt") or a.get("status")
            text = "%s  %s%s   %s" % (GLYPH.get(a.get("status"), "•"), a.get("display_name") or a.get("agent"),
                                      " · " + a["project"] if a.get("project") else "", short(doing, 48))
            rows.append((CMD_AGENT + i, text, True))
            for f in (a.get("flags") or [])[:2]:
                rows.append((0, "      ! " + short(f, 60), False))
        if len(live) > MAX_ROWS:
            rows.append((CMD_OPEN, "    … %d more" % (len(live) - MAX_ROWS), True))
        if c.get("sleeping"):
            rows.append((0, "%d sleeping session%s" % (c["sleeping"], "" if c["sleeping"] == 1 else "s"), False))
    rows += [(None, "", False),
             (CMD_OPEN, "Open dashboard", bool(snap)),
             (CMD_PAUSE, "Resume recording hook events" if is_paused else "Pause recording hook events", True),
             (None, "", False),
             (CMD_STOP, "Stop dashboard", True) if snap else (CMD_START, "Start dashboard", True),
             (CMD_QUIT_ICON, "Quit %s only" % icon, True),
             (CMD_QUIT, "Quit cADRe", True)]
    return rows


def fresh_alerts(snap, mark):
    """(alerts of the snapshot newer than `mark`, newest first; the new mark). The dashboard
    lists its recent alerts in every snapshot (cadre/respond/alerts.py); each is shown once."""
    new = [a for a in (snap or {}).get("alerts") or [] if (a.get("ts") or 0) > mark]
    return new, max([mark] + [a["ts"] for a in new])


def alert_text(alerts):
    """(title, text) of one notification for these alerts."""
    who = {(a.get("agent"), a.get("project")) for a in alerts}
    if len(who) == 1:
        a = alerts[0]
        title = "cADRe: %s%s" % (a.get("agent") or "agent", " · " + a["project"] if a.get("project") else "")
        lines = [short(a.get("text"), 80) for a in alerts[:3]]
    else:
        title = "cADRe: %d risk flags" % len(alerts)
        lines = [short("%s: %s" % (a.get("project") or a.get("agent") or "agent", a.get("text")), 80)
                 for a in alerts[:3]]
    if len(alerts) > 3:
        lines.append("+%d more" % (len(alerts) - 3))
    return title, "\n".join(lines)


def short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def log(msg, name="tray.log"):
    """Errors go to ~/.cadre/<name> (a detached icon has no console)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, name), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass
