"""Linux status icon for cADRe: a StatusNotifierItem (the "AppIndicator" protocol) with a
D-Bus menu, plus desktop notifications. Gio over the session bus, nothing else: needs
PyGObject (python3-gi, part of every Ubuntu / Fedora desktop), no AppIndicator library.

  python3 -m cadre menubar [--port 8765]          # start it (detached), unless it already runs
  python3 -m cadre menubar --stop
  python3 cadre/indicator.py --port 8765 --debug   # foreground; prints the menu after every poll

The Linux counterpart of the Windows tray (cadre/tray.py), with the same menu and alerts
(cadre/statusmenu.py). It is only a viewer: it polls the dashboard's /api/snapshot every 3 s
and never touches agents. Where it shows up: GNOME with the AppIndicator extension (Ubuntu
has it on by default), KDE Plasma, Xfce, Cinnamon, MATE, waybar and the like. Plain GNOME
without the extension has no place for icons; the notifications still come.

Started by the dashboard's launcher, which systemd runs at login, possibly before the desktop
is up: the icon waits for a StatusNotifierWatcher to appear on the bus (and registers again if
the shell restarts), and takes DISPLAY / WAYLAND_DISPLAY from the systemd user manager when it
opens the browser (a service started at login doesn't have them).
"""
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import zlib

if __name__ == "__main__" and not __package__:      # run as a script: import the package next to it
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cadre import statusmenu as sm  # noqa: E402
from cadre.statusmenu import (CMD_AGENT, CMD_OPEN, CMD_PAUSE, CMD_QUIT, CMD_QUIT_ICON,  # noqa: E402
                              CMD_START, CMD_STOP, EVERY)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
ICONS = {"on": "indicator", "waiting": "indicator-waiting", "off": "indicator-off"}
SIZES = (24, 48)
WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
NOTIFY = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications")
GIVE_UP = 300          # s: quit if neither an icon host nor a notification server shows up (headless)
DISPLAY_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
               "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")

XML = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
 </interface>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
   <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
   <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
   <arg type="u" direction="in"/>
  </method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup">
   <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
 </interface>
</node>"""


# ------------------------------------------------------------------ pure helpers (tested anywhere)
def png_argb(path):
    """(width, height, ARGB32 bytes in network order) of an 8-bit RGBA PNG, the kind
    scripts/make_icons.py writes; StatusNotifierItem pixmaps are ARGB32."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: %s" % path)
    pos, idat = 8, b""
    w = h = None
    while pos < len(data):
        n, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if (depth, ctype, interlace) != (8, 6, 0):
                raise ValueError("only 8-bit RGBA, not interlaced: %s" % path)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    raw, stride, out, prev = zlib.decompress(idat), w * 4, bytearray(), bytearray(w * 4)
    for y in range(h):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b, c = prev[i], prev[i - 4] if i >= 4 else 0
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        for i in range(0, stride, 4):
            out += bytes((line[i + 3], line[i], line[i + 1], line[i + 2]))   # RGBA -> ARGB
        prev = line
    return w, h, bytes(out)


def menu_label(text):
    """dbusmenu labels take "_" as a mnemonic marker: double it so names keep theirs."""
    return text.replace("_", "__")


def display_env(manager_env, env):
    """The display variables the systemd user manager knows and `env` lacks (a service
    started at login runs before the desktop session has handed them over)."""
    have = {k for k in DISPLAY_ENV if env.get(k)}
    out = {}
    for item in manager_env or ():
        k, _, v = item.partition("=")
        if k in DISPLAY_ENV and k not in have and v:
            out[k] = v
    return out


# ------------------------------------------------------------------ the icon
class Indicator:
    def __init__(self, port, debug=False):
        from gi.repository import Gio, GLib       # noqa: F401  (imported here: tests import the module without gi)
        self.Gio, self.GLib = Gio, GLib
        self.port, self.debug = port, debug
        self.base = "http://127.0.0.1:%d" % port
        self.snap, self.error = None, None
        self.opener = sm.opener()
        self.alert_mark = time.time()              # alerts up to here were shown (or predate the icon)
        self.wake = threading.Event()
        self.pixmaps = {k: [png_argb(os.path.join(STATIC, "%s-%d.png" % (f, n))) for n in SIZES]
                        for k, f in ICONS.items()}
        self.key, self.tip = "off", "cADRe"
        self.rows, self.revision = [], 1           # menu: [(cmd or None, text, enabled)]
        self.notes = set()                         # ids of our notifications (their clicks open the dashboard)
        self.hosts = set()                         # who has seen us: "watcher", "notify"
        self.busname = "org.kde.StatusNotifierItem-%d-1" % os.getpid()

    # ---------------------------------------------------------- D-Bus
    def V(self, sig, value):
        return self.GLib.Variant(sig, value)

    def get_property(self, conn, sender, path, iface, name):
        V = self.V
        if iface == "com.canonical.dbusmenu":
            return {"Version": V("u", 3), "TextDirection": V("s", "ltr"), "Status": V("s", "normal"),
                    "IconThemePath": V("as", [])}.get(name)
        pix = self.pixmaps[self.key]
        title, _, more = self.tip.partition("\n")
        return {"Category": V("s", "ApplicationStatus"), "Id": V("s", "cadre"), "Title": V("s", "cADRe"),
                "Status": V("s", "Active"), "WindowId": V("i", 0), "IconName": V("s", ""),
                "IconPixmap": V("a(iiay)", pix), "OverlayIconName": V("s", ""),
                "AttentionIconName": V("s", ""),
                "ToolTip": V("(sa(iiay)ss)", ("", pix, title, more)),
                "ItemIsMenu": V("b", True), "Menu": V("o", MENU_PATH)}.get(name)

    def item_props(self, i):
        cmd, text, on = self.rows[i]
        if cmd is None:
            return {"type": self.V("s", "separator")}
        return {"label": self.V("s", menu_label(text)), "enabled": self.V("b", bool(on and cmd))}

    def layout(self):
        kids = [self.V("(ia{sv}av)", (i + 1, self.item_props(i), [])) for i in range(len(self.rows))]
        return (0, {"children-display": self.V("s", "submenu")}, kids)

    def method_call(self, conn, sender, path, iface, method, params, inv):
        try:
            args = params.unpack()
            if method == "GetLayout":
                return inv.return_value(self.V("(u(ia{sv}av))", (self.revision, self.layout())))
            if method == "GetGroupProperties":
                ids = [i for i in args[0] if 0 < i <= len(self.rows)] or range(1, len(self.rows) + 1)
                return inv.return_value(self.V("(a(ia{sv}))", ([(i, self.item_props(i - 1)) for i in ids],)))
            if method == "GetProperty":
                i, name = args
                v = self.item_props(i - 1).get(name) if 0 < i <= len(self.rows) else None
                return inv.return_value(self.V("(v)", (v or self.V("s", ""),)))
            if method == "Event":
                if args[1] == "clicked":
                    self.clicked(args[0])
                return inv.return_value(None)
            if method == "EventGroup":
                for i, kind, _, _ in args[0]:
                    if kind == "clicked":
                        self.clicked(i)
                return inv.return_value(self.V("(ai)", ([],)))
            if method == "AboutToShow":
                return inv.return_value(self.V("(b)", (False,)))
            if method == "AboutToShowGroup":
                return inv.return_value(self.V("(aiai)", ([], [])))
            if method in ("Activate", "SecondaryActivate"):   # hosts that click the icon, not a menu
                self.command(CMD_OPEN)
            return inv.return_value(None)
        except Exception as e:
            sm.log("%s %s: %s: %s" % (iface, method, type(e).__name__, e), "indicator.log")
            inv.return_dbus_error("org.cadre.Error", str(e))

    def clicked(self, item):
        if 0 < item <= len(self.rows):
            cmd = self.rows[item - 1][0]
            if cmd:
                self.GLib.idle_add(lambda: self.command(cmd) and False)

    def emit(self, path, iface, name, sig=None, value=None):
        try:
            self.conn.emit_signal(None, path, iface, name, self.V(sig, value) if sig else None)
        except Exception as e:
            sm.log("signal %s: %s" % (name, e), "indicator.log")

    def register(self, *_):
        """Ask the watcher to show us; again whenever it (re)appears, e.g. after a shell restart."""
        self.hosts.add("watcher")
        self.conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                       self.V("(s)", (self.busname,)), None, self.Gio.DBusCallFlags.NONE, -1, None,
                       self._registered)

    def _registered(self, conn, res):
        try:
            conn.call_finish(res)
            if self.debug:
                print("[icon] registered with %s" % WATCHER, flush=True)
        except Exception as e:
            sm.log("RegisterStatusNotifierItem: %s" % e, "indicator.log")

    # ---------------------------------------------------------- notifications
    def notify(self, title, text):
        hints = {"urgency": self.V("y", 1), "desktop-entry": self.V("s", "cadre")}
        icon = os.path.join(STATIC, "icon-192.png")
        self.conn.call(NOTIFY[0], NOTIFY[1], NOTIFY[2], "Notify",
                       self.V("(susssasa{sv}i)", ("cADRe", 0, icon, title, self.GLib.markup_escape_text(text),
                                                  ["default", "Open dashboard"], hints, -1)),
                       self.GLib.VariantType("(u)"), self.Gio.DBusCallFlags.NONE, -1, None, self._notified)

    def _notified(self, conn, res):
        try:
            self.notes.add(conn.call_finish(res).unpack()[0])
            self.hosts.add("notify")
        except Exception as e:
            sm.log("Notify: %s" % e, "indicator.log")

    def on_action(self, conn, sender, path, iface, signal_name, params):
        nid, action = params.unpack()
        if nid in self.notes and action in ("default", "Open dashboard"):
            self.command(CMD_OPEN)

    # ---------------------------------------------------------- polling and showing
    def poller(self):
        while True:
            self.snap, self.error = sm.fetch(self.opener, self.base)
            self.GLib.idle_add(self.refresh)
            self.wake.wait(EVERY)
            self.wake.clear()

    def refresh(self):
        paused = sm.paused()
        key, tip = sm.status(self.snap, self.error, paused)
        if key != self.key:
            self.key = key
            self.emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")
        if tip != self.tip:
            self.tip = tip
            self.emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
        rows = sm.menu_items(self.snap, self.error, paused, "status icon")
        if rows != self.rows:
            self.rows, self.revision = rows, self.revision + 1
            self.emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", "(ui)", (self.revision, 0))
        new, self.alert_mark = sm.fresh_alerts(self.snap, self.alert_mark)
        if new:                                    # a detection rule just flagged an agent
            title, text = sm.alert_text(new)
            self.notify(title, text)
            if self.debug:
                print("[alert] %s | %s" % (title, text.replace("\n", " | ")), flush=True)
        if self.debug:
            print("[icon %s] %s\n%s" % (key, tip.replace("\n", " | "), "\n".join(
                "----" if c is None else ("  " if on else "  (off) ") + t for c, t, on in rows)), flush=True)
        return False

    # ---------------------------------------------------------- actions
    def command(self, cid):
        if cid == CMD_OPEN or cid >= CMD_AGENT:
            self.open_url(self.base + "/")
        elif cid == CMD_PAUSE:
            err = sm.set_paused(not sm.paused())
            if err:
                sm.log("pause: %s" % err, "indicator.log")
            self.refresh()
        elif cid == CMD_START:
            self.error = "starting…"
            threading.Thread(target=self.start_dashboard, daemon=True).start()
            self.refresh()
        elif cid == CMD_STOP:
            self.stop_dashboard()
        elif cid == CMD_QUIT_ICON:
            self.loop.quit()
        elif cid == CMD_QUIT:                      # the whole of cADRe (hooks still append to the event log)
            self.stop_dashboard()
            self.loop.quit()

    def open_url(self, url):
        env = dict(os.environ)
        try:
            got = self.conn.call_sync("org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                                      "org.freedesktop.DBus.Properties", "Get",
                                      self.V("(ss)", ("org.freedesktop.systemd1.Manager", "Environment")),
                                      None, self.Gio.DBusCallFlags.NONE, 2000, None)
            env.update(display_env(got.unpack()[0], env))
        except Exception:
            pass                                   # no systemd: what we were started with
        try:
            p = subprocess.Popen(["xdg-open", url], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
            threading.Thread(target=p.wait, daemon=True).start()
        except OSError as e:
            sm.log("open %s: %s" % (url, e), "indicator.log")

    def start_dashboard(self):
        """The installed copy if there is one (as `cadre start` does), else this folder's."""
        try:
            os.environ["CADRE_PORT"] = str(self.port)
            from cadre import paths, service
            if paths.installed():
                service.start(False)
            else:
                subprocess.Popen([sys.executable, "-m", "cadre", "--port", str(self.port)],
                                 cwd=os.path.dirname(HERE), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except Exception as e:
            sm.log("start: %s" % e, "indicator.log")
        self.wake.set()

    def stop_dashboard(self):
        """Stop the dashboard that answered the last poll (systemd counts SIGTERM as a clean
        exit, so it doesn't restart it; it comes back at the next login or with `cadre start`)."""
        pid = ((self.snap or {}).get("tracker") or {}).get("pid")
        if pid and pid != os.getpid():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError as e:
                sm.log("stop: %s" % e, "indicator.log")
        self.snap, self.error = None, "stopped"
        self.refresh()
        self.wake.set()

    # ---------------------------------------------------------- main loop
    def give_up(self):
        if not self.hosts:
            sm.log("no status icon host and no notification server after %d s: quitting" % GIVE_UP,
                   "indicator.log")
            self.loop.quit()
        return False

    def run(self):
        Gio, GLib = self.Gio, self.GLib
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        register = getattr(self.conn, "register_object_with_closures2", None) or self.conn.register_object
        for path, iface in ((ITEM_PATH, node.interfaces[0]), (MENU_PATH, node.interfaces[1])):
            register(path, iface, self.method_call, self.get_property, None)
        self.refresh()
        self.loop = GLib.MainLoop()
        Gio.bus_own_name_on_connection(self.conn, self.busname, Gio.BusNameOwnerFlags.NONE, None, None)
        Gio.bus_watch_name_on_connection(self.conn, WATCHER, Gio.BusNameWatcherFlags.NONE, self.register, None)
        Gio.bus_watch_name_on_connection(self.conn, NOTIFY[0], Gio.BusNameWatcherFlags.NONE,
                                         lambda *_: self.hosts.add("notify"), None)
        self.conn.signal_subscribe(NOTIFY[0], NOTIFY[2], "ActionInvoked", NOTIFY[1], None,
                                   Gio.DBusSignalFlags.NONE, self.on_action)
        try:                                       # GLib 2.88 moved it; older ones only have the GLib name
            import gi
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            on_signal = GLibUnix.signal_add
        except (ImportError, ValueError, AttributeError):
            on_signal = GLib.unix_signal_add
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            on_signal(GLib.PRIORITY_DEFAULT, sig, lambda: self.loop.quit() or False)
        GLib.timeout_add_seconds(GIVE_UP, self.give_up)
        threading.Thread(target=self.poller, daemon=True).start()
        self.loop.run()
        return 0


def has_gi():
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="cadre.indicator")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--debug", action="store_true", help="print the menu after every poll")
    a = ap.parse_args(argv)
    if not has_gi():
        print("The Linux status icon needs PyGObject: sudo apt install python3-gi (Fedora: python3-gobject)")
        return 1
    try:
        return Indicator(a.port, a.debug).run()
    except Exception as e:
        sm.log("status icon stopped: %s: %s" % (type(e).__name__, e), "indicator.log")
        raise


if __name__ == "__main__":
    sys.exit(main())
