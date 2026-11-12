"""Start or stop cADRe's status icon: the macOS menu bar icon (cadre/menubar.js), the
Windows tray icon (cadre/tray.py) or the Linux status icon (cadre/indicator.py). Same menu
on all three.

  python3 -m cadre menubar [--port 8765]     # start it (detached), unless it already runs
  python3 -m cadre menubar --stop
  py -m cadre tray [--stop]                  # Windows: the same command under its own name

macOS: a JavaScript for Automation script run by /usr/bin/osascript. Windows: ctypes
(Shell_NotifyIconW) run by pythonw.exe. Linux: a StatusNotifierItem over D-Bus, with Gio from
PyGObject (python3-gi, on every mainstream desktop; without it there is no icon, and the
dashboard is unaffected).
Both are only viewers: they poll the dashboard's /api/snapshot, and the dashboard keeps running
whether or not the icon does. The installed launcher (cadre_service.py, run at login by the
LaunchAgent or the HKCU Run value) starts the icon too; `cadre start` brings both back after
"Quit cADRe".
"""
import os
import shutil
import signal
import subprocess
import sys
import time

from .procs import list_processes

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "menubar.js")
OSASCRIPT = "/usr/bin/osascript"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
NAME = "tray icon" if IS_WIN else "menu bar icon" if IS_MAC else "status icon"
SYSTEM_PYTHON = "/usr/bin/python3"     # has the distribution's python3-gi when a venv's Python doesn't


def supported():
    return IS_WIN or IS_MAC or (IS_LINUX and bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")
                                                  or os.path.exists("/run/user/%d/bus" % os.getuid())))


def linux_python():
    """A Python that can import Gio (PyGObject): this one, else the system's; None if neither."""
    from . import indicator
    if indicator.has_gi():
        return sys.executable
    if os.path.isfile(SYSTEM_PYTHON) and os.path.realpath(SYSTEM_PYTHON) != os.path.realpath(sys.executable):
        try:
            ok = subprocess.run([SYSTEM_PYTHON, "-I", "-c", "import gi; gi.require_version('Gio', '2.0');"
                                 "from gi.repository import Gio"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            return SYSTEM_PYTHON
    return None


def linux_command(py, script, port, env=None):
    """argv that starts the icon. Started from the dashboard's systemd unit, it goes into a
    scope of its own: in the unit's cgroup, stopping the dashboard (its menu, `systemctl
    --user stop cadre`, setup's restart) would end the icon with it."""
    env = os.environ if env is None else env
    argv = [py, script, "--port", str(port)]
    run = shutil.which("systemd-run")
    if env.get("INVOCATION_ID") and run:
        return [run, "--user", "--scope", "--quiet", "--collect", "--unit=cadre-icon-%d" % os.getpid(), "--"] + argv
    return argv


def _is_icon(p):
    """Started from any copy of cADRe (checkout or install folder)?"""
    argv = [a.strip('"') for a in (p.get("args") or "").split()]   # a path with spaces splits, but
    if not argv or p["pid"] == os.getpid():                         # its last piece keeps the file name
        return False
    exe = os.path.basename((p.get("exe") or argv[0]).strip('"')).lower()
    if IS_WIN:
        return exe.startswith("python") and \
            any(a.replace("/", "\\").lower().endswith("cadre\\tray.py") for a in argv[1:])
    if IS_LINUX:
        return exe.startswith("python") and any(a.endswith("cadre/indicator.py") for a in argv[1:])
    return exe == "osascript" and any(os.path.basename(a) == "menubar.js" for a in argv[1:])


def running():
    """pids of running icons."""
    return [p["pid"] for p in list_processes().values() if _is_icon(p)]


def start(port, root=None):
    """root: the folder holding the cadre package to run the icon from (default: this one)."""
    pkg = os.path.join(root, "cadre") if root else HERE
    if not supported():
        print("No status icon here (it needs macOS, Windows or a Linux desktop session); "
              "open the dashboard at http://127.0.0.1:%d" % port)
        return 1
    pids = running()
    if pids:
        print("cADRe %s is already running (pid %s)." % (NAME, ", ".join(map(str, pids))))
        return 0
    if IS_WIN:
        from . import tray
        subprocess.Popen([tray.pythonw(), os.path.join(pkg, "tray.py"), "--port", str(port)],
                         cwd=os.path.dirname(pkg), **tray.detached())
        print("cADRe tray icon started (the cADRe ring in the notification area; Windows 11 may put it "
              "under the ^ arrow).")
        return 0
    if IS_LINUX:
        py = linux_python()
        if not py:
            print("The Linux status icon needs PyGObject: sudo apt install python3-gi (Fedora: python3-gobject).")
            return 1
        subprocess.Popen(linux_command(py, os.path.join(pkg, "indicator.py"), port), cwd=os.path.dirname(pkg),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)
        print("cADRe status icon started (the cADRe ring in the top bar; GNOME shows it with the AppIndicator "
              "extension, on by default in Ubuntu). Alerts come as desktop notifications.")
        return 0
    subprocess.Popen([OSASCRIPT, "-l", "JavaScript", os.path.join(pkg, "menubar.js"), str(port)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True)
    print("cADRe menu bar started (look for the cADRe ring at the top right).")
    return 0


def quit_running(pids=None):
    """Quit the running icons. Windows: ask its window to close first, so the icon leaves the
    tray at once (a killed process leaves a ghost icon until the mouse passes over it)."""
    pids = running() if pids is None else pids
    if IS_WIN and pids:
        from . import tray
        if tray.post(tray.WM_CLOSE):
            for _ in range(30):
                if not any(p in list_processes() for p in pids):
                    return pids
                time.sleep(0.1)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return pids


def stop():
    pids = quit_running()
    print("stopped the cADRe %s (pid %s)." % (NAME, ", ".join(map(str, pids))) if pids
          else "cADRe %s isn't running." % NAME)
    return 0
