"""cADRe entry point.

  python3 -m cadre            # dashboard on http://127.0.0.1:8765
  python3 -m cadre --once     # print a table and exit
  python3 -m cadre --json     # dump one snapshot as JSON
  python3 -m cadre --install-hooks [--dry-run]    # hook every agent that supports hooks
  python3 -m cadre --uninstall-hooks [--dry-run]
  python3 -m cadre setup [--dry-run]       # install for this user: hooks, autostart, dashboard
  python3 -m cadre uninstall [--dry-run] [--purge]
  python3 -m cadre start                     # start the installed dashboard (+ its status icon)
  python3 -m cadre menubar [--stop]          # status icon: macOS menu bar / Windows tray / Linux top bar (alias: tray)
  python3 -m cadre --rules                   # list the detection rules (cadre/detect)
  python3 -m cadre --responses               # list the response log (cadre/respond)
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__, events, zcode
from .collectors import snapshot
from .events import LOG_DIR, LOG_FILE, STORE

STATIC = os.path.join(os.path.dirname(__file__), "static")


class Cache:
    """Collect at most once per `ttl` seconds no matter how many tabs poll."""

    def __init__(self, ttl):
        self.ttl, self.data, self.at, self.lock = ttl, None, 0.0, threading.Lock()
        self.log_size = None

    def invalidate(self):
        with self.lock:
            self.at = 0.0

    def get(self):
        try:                            # a hook appended an event: show it now, not after ttl
            size = os.path.getsize(LOG_FILE)
        except OSError:
            size = None
        with self.lock:
            if self.data is None or time.time() - self.at > self.ttl or size != self.log_size:
                self.data, self.at, self.log_size = snapshot(), time.time(), size
            return self.data


def event_history(url):
    """GET /api/events?before=<ts>&limit=<n>&q=&agent=&kind=&project= : older feed events for
    the dashboard's scrolling feed and the event explorer, newest first. Hook events come from
    the log on disk (all of it, rotated part included); ZCode's from its DB (last 24 h)."""
    qs = parse_qs(urlsplit(url).query)
    arg = lambda k: (qs.get(k) or [None])[0]
    try:
        before = float(arg("before")) if arg("before") else None
        limit = max(1, min(500, int(arg("limit") or 100)))
    except ValueError:
        before, limit = None, 100
    from .respond import dismiss
    match = events.matcher(arg("q"), arg("agent"), arg("kind"), arg("project"))
    # findings marked as false positives are off the event before it is matched, so
    # kind=flagged (the Alerts tab, the Risk flags tile) doesn't list them
    seen = (lambda e: match(dismiss.apply_event(e))) if match else None
    logged, more = events.history(before, limit, seen)
    logged = [dismiss.apply_event(e) for e in logged]
    try:
        extra = [e for e in map(dismiss.apply_event, zcode.read_events(limit))
                 if (before is None or e["ts"] < before) and (match is None or match(e))]
    except Exception:                                   # never let ZCode's DB break the explorer
        extra = []
    merged = sorted(logged + extra, key=lambda e: e.get("ts") or 0, reverse=True)
    return {"events": merged[:limit], "more": more or len(merged) > limit}


def set_paused(on):
    """Create or remove ~/.cadre/paused; every hook (emit.py) skips its event while it exists."""
    path = os.path.join(LOG_DIR, "paused")
    if on:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("paused from the cADRe dashboard\n")
    elif os.path.exists(path):
        os.remove(path)


def install_hooks(body):
    """POST /api/hooks {"paths": [...]} installs into those hook files, {"all": true} into every
    file that needs it; "dry_run": true only reports. Paths are matched against what the
    installer discovers itself, so nothing outside the agents' own hook files can be written."""
    from . import installer
    if not isinstance(body, dict):
        raise ValueError("object expected")
    paths = body.get("paths")
    if body.get("all"):
        paths = None
    elif not (isinstance(paths, list) and paths and all(isinstance(p, str) for p in paths)):
        raise ValueError("paths or all expected")
    return installer.install_paths(paths, dry_run=bool(body.get("dry_run")))


def respond_action(body, snap):
    """POST /api/respond {"id": "<agent id from the snapshot>", "action": "pause" | "resume" |
    "kill", "confirm": true (kill only)}: the Pause / Resume / Kill buttons on an agent's card.
    The agent is looked up in the snapshot, so a request can't name a process of its own.

    {"action": "dismiss" | "restore", "who": "<session id or agent id>", "rule": "<rule id>",
    "detail": "..."} marks a finding as a false positive, or takes that back; "agent" and
    "project" are only labels for the log when the session is no longer in the snapshot."""
    from . import respond
    if not isinstance(body, dict):
        raise ValueError("object expected")
    if body.get("action") in ("dismiss", "restore"):
        return respond.dismiss.mark(snap, body.get("who"), body.get("rule"), body.get("detail") or "",
                                    restore=body["action"] == "restore",
                                    agent=body.get("agent"), project=body.get("project"))
    agent_id, action = body.get("id"), body.get("action")
    if not (isinstance(agent_id, str) and isinstance(action, str)):
        raise ValueError("id and action expected")
    return respond.act(snap, agent_id, action, confirm=body.get("confirm"))


STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".png": "image/png",
                ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json"}
LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}


def allowed_hosts(host):
    """Host header values we answer to. Binding to 127.0.0.1 keeps other machines out, but
    not "DNS rebinding": a web page whose own hostname is re-pointed at 127.0.0.1 could
    otherwise read /api/snapshot (prompts, paths) from the browser. Its requests carry that
    page's hostname in Host, so only loopback names (and an explicit --host) are accepted."""
    names = set(LOOPBACK)
    if host not in ("0.0.0.0", "::", ""):
        names.add(host.lower())
    return names


def host_ok(header, names):
    if not header:
        return False
    h = header.strip().lower()
    if h.startswith("["):                           # [::1]:8765
        h = h[: h.find("]") + 1] if "]" in h else h
    elif h.count(":") == 1:
        h = h.rsplit(":", 1)[0]
    return h in names


def origin_ok(header, names):
    """A browser names the page a POST comes from in Origin; it must be this dashboard. No
    Origin = not a browser (a hook, the tray icon, curl). A second lock behind the JSON
    content type, added with the endpoint that can pause or kill an agent."""
    if not header:
        return True
    u = urlsplit(header)
    return u.scheme in ("http", "https") and host_ok(u.netloc, names)


def serve(host, port, ttl):
    cache = Cache(ttl)
    names = allowed_hosts(host)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            if host_ok(self.headers.get("Host"), names):
                return True
            self._send(403, b"forbidden host", "text/plain")
            return False

        def do_POST(self):
            # events from older hooks that still POST (new ones append to ~/.cadre/events.jsonl)
            if not self._host_ok():
                return
            if self.path not in ("/api/events", "/api/pause", "/api/hooks", "/api/respond"):
                return self._send(404, b"not found", "text/plain")
            if not origin_ok(self.headers.get("Origin"), names):
                return self._send(403, b"forbidden origin", "text/plain")
            # Browsers can't send application/json cross-origin without a CORS preflight,
            # which we never answer, so web pages can't inject fake events (or pause hooks).
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(415, b"json only", "text/plain")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 64 * 1024:
                    return self._send(413, b"too large", "text/plain")
                body = json.loads(self.rfile.read(n))
                if self.path == "/api/hooks":       # Install / Install all pending (dashboard buttons)
                    out = install_hooks(body)
                    cache.invalidate()
                    return self._send(200, json.dumps(out).encode(), "application/json")
                if self.path == "/api/respond":     # Pause / Resume / Kill on an agent's card
                    try:
                        out = respond_action(body, cache.get())
                    except ValueError as e:
                        return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                    cache.invalidate()
                    return self._send(200, json.dumps(out).encode(), "application/json")
                if self.path == "/api/pause":      # the dashboard's Pause button: same file as the tray
                    set_paused(bool(body.get("paused")))
                    ok = True
                else:
                    ok = STORE.add(body)
                cache.invalidate()
                self._send(204 if ok else 400, b"", "text/plain")
            except (ValueError, AttributeError, OSError):
                self._send(400, b"bad request", "text/plain")

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/snapshot"):
                self._send(200, json.dumps(cache.get()).encode(), "application/json")
            elif path == "/api/events":
                self._send(200, json.dumps(event_history(self.path)).encode(), "application/json")
            elif path == "/api/hooks":              # Recalibrate: a fresh scan of the whole machine
                from . import installer
                self._send(200, json.dumps(installer.scan()).encode(), "application/json")
            elif path == "/api/responses":          # the response log, newest first
                from . import respond
                self._send(200, json.dumps({"responses": respond.audit.entries(200)[::-1]}).encode(),
                           "application/json")
            elif path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                name = os.path.basename(path)       # flat folder: no subpaths, no ..
                p = os.path.join(STATIC, name)
                ctype = STATIC_TYPES.get(os.path.splitext(name)[1])
                if ctype and os.path.isfile(p):
                    with open(p, "rb") as f:
                        self._send(200, f.read(), ctype)
                else:
                    self._send(404, b"not found", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

    srv = ThreadingHTTPServer((host, port), Handler)
    print("cADRe dashboard: http://%s:%d  (Ctrl+C to stop)" % (host, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def ago(ts):
    if not ts:
        return "-"
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return "%d%s ago" % (s // n, unit)
    return "%ds ago" % s


def print_table(snap):
    c = snap["counts"]
    print("cADRe @ %s   " % snap["host"] +
          "  ".join("%s=%d" % (k, v) for k, v in c.items()))
    print()
    fmt = "%-10s %-16s %-7s %-6s %-26s %-12s %s"
    print(fmt % ("STATUS", "AGENT", "PID", "CPU%", "PROJECT", "LAST SEEN", "DOING"))
    for a in snap["agents"]:
        doing = a.get("last_tool") or a.get("last_prompt") or ""
        print(fmt % (a["status"], a["agent"][:16], a.get("pid") or "-",
                     a.get("cpu", "-"), (a.get("project") or "-")[:26],
                     ago(a.get("last_activity")), doing[:70]))
        for fl in a.get("flags") or []:
            print("%-10s   ! %s" % ("", fl))
    print("\nINSTALLED")
    for i in snap["installed"]:
        print("  %s %-24s %s" % ("●" if i["active"] else "○", i["agent"], i["evidence"][0]))


def print_rules():
    from . import detect
    rows = sorted(detect.RULES.values(), key=lambda r: (r.category, -detect.rank(r.severity), r.id))
    cat = None
    for r in rows:
        if r.category != cat:
            cat = r.category
            print("\n%s  (cadre/detect/rules/%s.py)" % (cat, cat))
        print("  %-22s %-5s %-8s %s" % (r.id, r.severity, r.kind, r.title))
    print("\n%d rules. How to add one: cadre/detect/README.md" % len(rows))


def print_responses():
    from . import respond
    rows = respond.audit.entries(200)
    for e in rows:
        print("%s  %-7s %-4s %-28s %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("ts") or 0)), e.get("action"),
            "ok" if e.get("ok") else "FAIL",
            ("%s%s" % (e.get("agent") or "?", " · " + e["project"] if e.get("project") else ""))[:28],
            e.get("result") or ""))
    print("%s%d responses in %s" % ("\n" if rows else "", len(rows), respond.audit.path()))


def main():
    for s in (sys.stdout, sys.stderr):   # Windows consoles may not be UTF-8
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="cadre")
    ap.add_argument("command", nargs="?", choices=["setup", "uninstall", "start", "menubar", "tray"],
                    help="setup: install for this user (hooks, autostart, dashboard); uninstall: undo it; "
                         "start: start the installed dashboard (and its status icon) again; "
                         "menubar / tray: show the status icon (macOS menu bar, Windows tray, Linux top bar)")
    ap.add_argument("--version", action="version", version="cADRe " + __version__)
    ap.add_argument("--no-autostart", action="store_true", help="setup: don't start at login")
    ap.add_argument("--no-start", action="store_true", help="setup: don't start the dashboard now")
    ap.add_argument("--no-browser", action="store_true", help="setup: don't open the dashboard")
    ap.add_argument("--no-psutil", action="store_true",
                    help="setup (Windows): don't fetch psutil from PyPI into the install folder")
    ap.add_argument("--stop", action="store_true", help="menubar / tray: quit the status icon")
    ap.add_argument("--purge", action="store_true", help="uninstall: also delete ~/.cadre (event log)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ttl", type=float, default=2.0, help="seconds between collections")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rules", action="store_true", help="list the detection rules and where each one lives")
    ap.add_argument("--responses", action="store_true",
                    help="list the response log: every Pause, Resume and Kill done from the dashboard")
    ap.add_argument("--install-hooks", action="store_true",
                    help="add the watch-only hook to every agent found that supports hooks")
    ap.add_argument("--uninstall-hooks", action="store_true", help="remove cADRe hooks from all agents")
    ap.add_argument("--dry-run", action="store_true", help="with --install/--uninstall-hooks: only show changes")
    ap.add_argument("--agents", help="comma-separated agent keys to limit hook install/uninstall, "
                                     "e.g. claude-code,codex")
    a = ap.parse_args()
    if a.command in ("menubar", "tray"):
        from . import menubar
        raise SystemExit(menubar.stop() if a.stop else menubar.start(a.port))
    if a.command:
        from . import service
        if a.command == "start":
            raise SystemExit(service.start_all())
        if a.command == "setup":
            agents = {s.strip() for s in a.agents.split(",")} if a.agents else None
            raise SystemExit(service.setup(dry_run=a.dry_run, autostart=not a.no_autostart,
                                           start_now=not a.no_start, browser=not a.no_browser, agents=agents,
                                           psutil=not a.no_psutil))
        raise SystemExit(service.uninstall(dry_run=a.dry_run, purge=a.purge))
    if a.install_hooks or a.uninstall_hooks:
        from . import installer
        agents = {s.strip() for s in a.agents.split(",")} if a.agents else None
        raise SystemExit(installer.run(install=a.install_hooks, dry_run=a.dry_run, agents=agents))
    if a.rules:
        print_rules()
    elif a.responses:
        print_responses()
    elif a.json:
        print(json.dumps(snapshot(), indent=2))
    elif a.once:
        print_table(snapshot())
    else:
        serve(a.host, a.port, a.ttl)


if __name__ == "__main__":
    main()
