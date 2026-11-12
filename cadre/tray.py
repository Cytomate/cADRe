"""Windows tray icon for cADRe (notification area). ctypes only: no pywin32, no compiler.

  py -m cadre tray [--port 8765]           # start it (detached, pythonw), unless it already runs
  py -m cadre tray --stop
  py cadre\\tray.py --port 8765 --debug     # foreground; prints the menu after every poll

The Windows counterpart of the macOS menu bar icon (cadre/menubar.js), with the same menu.
It is only a viewer: it polls the dashboard's /api/snapshot every 3 s and never touches
agents. "Pause recording hook events" creates ~/.cadre/paused (CADRE_HOME respected), which
emit.py checks before recording anything. When a detection rule newly flags an agent (the
snapshot's "alerts"), it shows a Windows notification; clicking it opens the dashboard.

Icon: the cADRe ring, blue = watching, amber = an agent is waiting for you, grey = the
dashboard is down or hook events are paused. The tooltip has the counts; left or right click opens the menu.

Other processes can drive it by window class: `--stop` posts WM_CLOSE to the "cADReTray"
window, and posting WM_COMMAND with one of the CMD_* ids runs that menu item (used to test it
without clicking).
"""
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes as W

if __name__ == "__main__" and not __package__:      # run as a script: import the package next to it
    # insert, don't replace sys.path[0]: in the embeddable Python (Windows release) a ._pth
    # file makes sys.path[0] the standard library's zip, not this script's folder
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cadre import statusmenu as sm  # noqa: E402
from cadre.statusmenu import (CMD_AGENT, CMD_OPEN, CMD_PAUSE, CMD_QUIT, CMD_QUIT_ICON,  # noqa: E402,F401
                              CMD_START, CMD_STOP, EVERY, PAUSED, alert_text, fresh_alerts, short)

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = {k: os.path.join(HERE, "static", f) for k, f in
         (("on", "tray.ico"), ("waiting", "tray-waiting.ico"), ("off", "tray-off.ico"))}
CLASS = "cADReTray"

# ------------------------------------------------------------------ Win32
WM_DESTROY, WM_CLOSE, WM_NULL, WM_COMMAND = 0x0002, 0x0010, 0x0000, 0x0111
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_APP = 0x8000
WM_TRAY, WM_REFRESH = WM_APP + 1, WM_APP + 2      # icon callback; "a poll finished"
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
NIIF_WARNING = 2
NIN_BALLOONUSERCLICK = 0x0405                     # the notification itself was clicked
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON = 49
ERROR_ALREADY_EXISTS = 183
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HANDLE), ("lpszMenuName", W.LPCWSTR),
                ("lpszClassName", W.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128),
                ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256),
                ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", W.HICON)]


def _api():
    u = ctypes.WinDLL("user32", use_last_error=True)
    s = ctypes.WinDLL("shell32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    sigs = {
        (u, "RegisterClassW"): (W.ATOM, ctypes.POINTER(WNDCLASSW)),
        (u, "CreateWindowExW"): (W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID),
        (u, "DefWindowProcW"): (LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM),
        (u, "DestroyWindow"): (W.BOOL, W.HWND),
        (u, "GetMessageW"): (W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT),
        (u, "TranslateMessage"): (W.BOOL, ctypes.POINTER(W.MSG)),
        (u, "DispatchMessageW"): (LRESULT, ctypes.POINTER(W.MSG)),
        (u, "PostMessageW"): (W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM),
        (u, "PostQuitMessage"): (None, ctypes.c_int),
        (u, "RegisterWindowMessageW"): (W.UINT, W.LPCWSTR),
        (u, "FindWindowW"): (W.HWND, W.LPCWSTR, W.LPCWSTR),
        (u, "CreatePopupMenu"): (W.HMENU,),
        (u, "AppendMenuW"): (W.BOOL, W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR),
        (u, "SetMenuDefaultItem"): (W.BOOL, W.HMENU, W.UINT, W.UINT),
        (u, "TrackPopupMenu"): (ctypes.c_int, W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                W.HWND, ctypes.c_void_p),
        (u, "DestroyMenu"): (W.BOOL, W.HMENU),
        (u, "GetCursorPos"): (W.BOOL, ctypes.POINTER(W.POINT)),
        (u, "SetForegroundWindow"): (W.BOOL, W.HWND),
        (u, "LoadImageW"): (W.HANDLE, W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT),
        (u, "DestroyIcon"): (W.BOOL, W.HICON),
        (u, "GetSystemMetrics"): (ctypes.c_int, ctypes.c_int),
        (u, "SetProcessDPIAware"): (W.BOOL,),
        (s, "Shell_NotifyIconW"): (W.BOOL, W.DWORD, ctypes.POINTER(NOTIFYICONDATAW)),
        (k, "GetModuleHandleW"): (W.HMODULE, W.LPCWSTR),
        (k, "CreateMutexW"): (W.HANDLE, W.LPVOID, W.BOOL, W.LPCWSTR),
    }
    api = {}
    for (dll, name), (res, *args) in sigs.items():
        f = getattr(dll, name)
        f.restype, f.argtypes = res, args
        api[name] = f
    return type("Win32", (), api)


# ------------------------------------------------------------------ the icon
class Tray:
    def __init__(self, port, debug=False):
        self.port, self.debug = port, debug
        self.base = "http://127.0.0.1:%d" % port
        self.w = _api()
        self.snap, self.error = None, None
        self.shown = None                          # (icon key, tooltip) last given to the shell
        self.alert_mark = time.time()              # alerts up to here were shown (or predate the icon)
        self.wake = threading.Event()
        self.opener = sm.opener()
        self.icons = {}
        self.hwnd = None
        self._wndproc = WNDPROC(self._proc)       # keep a reference: Windows calls it

    # ---------------------------------------------------------- state
    def paused(self):
        return sm.paused()

    def set_paused(self, on):
        err = sm.set_paused(on, "the cADRe tray icon")
        if err:
            log("pause: %s" % err)

    def fetch(self):
        return sm.fetch(self.opener, self.base)

    def poller(self):
        while True:
            self.snap, self.error = self.fetch()
            self.w.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)
            self.wake.wait(EVERY)
            self.wake.clear()

    # ---------------------------------------------------------- what is shown
    def status(self):
        """(icon key, tooltip)"""
        return sm.status(self.snap, self.error, self.paused())

    def menu_items(self):
        """[(id or None, text, enabled)]; id None = separator. Mirrors menubar.js render()."""
        return sm.menu_items(self.snap, self.error, self.paused(), "tray icon")

    def refresh(self):
        shown = self.status()
        if self.shown is None:                     # not added yet (e.g. Explorer wasn't up at login)
            self.notify(NIM_ADD, *shown)
        elif shown != self.shown:
            self.notify(NIM_MODIFY, *shown)
        new, self.alert_mark = fresh_alerts(self.snap, self.alert_mark)
        if new:                                    # a detection rule just flagged an agent
            title, text = alert_text(new)
            self.balloon(title, text)
            if self.debug:
                print("[alert] %s | %s" % (title, text.replace("\n", " | ")), flush=True)
        if self.debug:
            rows = ["----" if cid is None else ("  " if on else "  (off) ") + text
                    for cid, text, on in self.menu_items()]
            print("[icon %s%s] %s\n%s" % (shown[0], "" if self.shown else ", not in the tray",
                                         shown[1].replace("\n", " | "), "\n".join(rows)), flush=True)

    def notify(self, action, key="off", tip="cADRe"):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        if action != NIM_DELETE:
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self.icons.get(key)
            nid.szTip = tip
        ok = self.w.Shell_NotifyIconW(action, ctypes.byref(nid))
        if action != NIM_DELETE:
            self.shown = (key, tip) if ok else None
        return ok

    def balloon(self, title, text):
        """A Windows notification from the icon (a toast on Windows 10 and 11)."""
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        nid.uFlags = NIF_INFO
        nid.szInfoTitle, nid.szInfo = title[:63], text[:255]
        nid.dwInfoFlags = NIIF_WARNING
        return self.w.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def show_menu(self):
        h = self.w.CreatePopupMenu()
        for cid, text, on in self.menu_items():
            if cid is None:
                self.w.AppendMenuW(h, MF_SEPARATOR, 0, None)
            else:
                self.w.AppendMenuW(h, MF_STRING | (0 if on else MF_GRAYED), cid,
                                   text.replace("&", "&&").replace("\t", " "))
        if self.snap:
            self.w.SetMenuDefaultItem(h, CMD_OPEN, 0)
        pt = W.POINT()
        self.w.GetCursorPos(ctypes.byref(pt))
        self.w.SetForegroundWindow(self.hwnd)      # else the menu doesn't close on an outside click
        cid = self.w.TrackPopupMenu(h, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        self.w.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        self.w.DestroyMenu(h)
        if cid:
            self.command(cid)

    # ---------------------------------------------------------- actions
    def command(self, cid):
        if cid == CMD_OPEN or cid >= CMD_AGENT:
            webbrowser.open(self.base + "/")
        elif cid == CMD_PAUSE:
            self.set_paused(not self.paused())
            self.refresh()
        elif cid == CMD_START:
            self.error = "starting…"
            threading.Thread(target=self.start_dashboard, daemon=True).start()
            self.refresh()
        elif cid == CMD_STOP:
            self.stop_dashboard()
        elif cid == CMD_QUIT_ICON:
            self.w.DestroyWindow(self.hwnd)
        elif cid == CMD_QUIT:                      # the whole of cADRe (hooks still append to the event log)
            self.stop_dashboard()
            self.w.DestroyWindow(self.hwnd)

    def start_dashboard(self):
        """The installed copy if there is one (as `cadre start` does), else this folder's."""
        try:
            os.environ["CADRE_PORT"] = str(self.port)
            from cadre import paths, service
            if paths.installed():
                service.start(False)
            else:
                subprocess.Popen([pythonw(), "-m", "cadre", "--port", str(self.port)],
                                 cwd=os.path.dirname(HERE), **detached())
        except Exception as e:
            log("start: %s" % e)
        self.wake.set()

    def stop_dashboard(self):
        """Stop the dashboard that answered the last poll (it comes back at the next login,
        with "Start dashboard" or with `cadre start`)."""
        pid = ((self.snap or {}).get("tracker") or {}).get("pid")
        if pid and pid != os.getpid():
            try:
                os.kill(int(pid), signal.SIGTERM)   # TerminateProcess
            except OSError as e:
                log("stop: %s" % e)
        self.snap, self.error = None, "stopped"
        self.refresh()
        self.wake.set()

    # ---------------------------------------------------------- window
    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY:
                if lp & 0xFFFF in (WM_LBUTTONUP, WM_RBUTTONUP):
                    self.show_menu()
                elif lp & 0xFFFF == NIN_BALLOONUSERCLICK:
                    webbrowser.open(self.base + "/")
                return 0
            if msg == WM_REFRESH:
                self.refresh()
                return 0
            if msg == WM_COMMAND:
                self.command(wp & 0xFFFF)
                return 0
            if msg == self.taskbar_created:        # Explorer restarted: the icon is gone, add it again
                self.shown = None
                self.notify(NIM_ADD, *self.status())
                return 0
            if msg == WM_DESTROY:
                self.notify(NIM_DELETE)
                self.w.PostQuitMessage(0)
                return 0
        except Exception as e:                     # an exception can't cross into Windows
            log("%s: %s" % (type(e).__name__, e))
        return self.w.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self):
        w = self.w
        self._mutex = w.CreateMutexW(None, False, "Local\\cADRe.tray")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            print("cADRe tray icon is already running.")
            return 0
        try:
            w.SetProcessDPIAware()                 # so the small-icon size below is the real one
        except (AttributeError, OSError):
            pass
        size = w.GetSystemMetrics(SM_CXSMICON) or 16
        for key, path in ICONS.items():
            self.icons[key] = w.LoadImageW(None, path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        hinst = w.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinst, lpszClassName=CLASS)
        if not w.RegisterClassW(ctypes.byref(wc)):
            raise OSError("RegisterClassW failed (%d)" % ctypes.get_last_error())
        self.taskbar_created = w.RegisterWindowMessageW("TaskbarCreated")
        # a hidden top-level window, not a message-only one: those miss "TaskbarCreated"
        self.hwnd = w.CreateWindowExW(0, CLASS, "cADRe", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed (%d)" % ctypes.get_last_error())
        if not self.notify(NIM_ADD, *self.status()):
            log("Shell_NotifyIconW(NIM_ADD) failed; retrying after each poll")
        signal.signal(signal.SIGINT, lambda *_: w.PostMessageW(self.hwnd, WM_CLOSE, 0, 0))   # --debug: Ctrl+C
        threading.Thread(target=self.poller, daemon=True).start()
        msg = W.MSG()
        while w.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            w.TranslateMessage(ctypes.byref(msg))
            w.DispatchMessageW(ctypes.byref(msg))
        for h in self.icons.values():
            if h:
                w.DestroyIcon(h)
        return 0


# ------------------------------------------------------------------ helpers
def log(msg):
    """Errors go to ~/.cadre/tray.log (under pythonw there is no console)."""
    sm.log(msg, "tray.log")


def pythonw():
    """A release's bundled pythonw.exe if there is one, else the one next to this Python."""
    from cadre import paths
    if paths.bundled_python(gui=True):
        return paths.bundled_python(gui=True)
    exe = sys.executable or "pythonw"
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return w if os.path.isfile(w) else exe


def detached():
    return dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS, NEW_PROCESS_GROUP


def window():
    """The running tray icon's window, or None."""
    return _api().FindWindowW(CLASS, None) or None


def post(msg, wparam=0):
    """Post to the running tray icon: WM_CLOSE quits it, (WM_COMMAND, CMD_*) runs a menu item."""
    hwnd = window()
    if hwnd:
        _api().PostMessageW(hwnd, msg, wparam, 0)
    return bool(hwnd)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="cadre.tray")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--debug", action="store_true", help="print the menu after every poll")
    a = ap.parse_args(argv)
    if a.debug and sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # piped stdout is cp1252
    try:
        return Tray(a.port, a.debug).run()
    except Exception as e:
        log("tray stopped: %s: %s" % (type(e).__name__, e))
        raise


if __name__ == "__main__":
    sys.exit(main())
