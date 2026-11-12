"""`cadre setup` / `cadre uninstall`: install cADRe for this user in one step, or take it out.

setup:
  1. copies the cadre package to the install folder (cadre/paths.py), with a small launcher,
  2. installs the watch-only hooks for every agent found, pointing at that copy,
  3. starts the dashboard at login (Windows: HKCU Run key; macOS: a LaunchAgent; Linux: a
     systemd user service),
  4. starts it now and opens http://127.0.0.1:8765.
Re-running it updates the installed copy (e.g. after `git pull`) and restarts the dashboard.

uninstall: removes the hooks, the autostart entry and the install folder; --purge also deletes
~/.cadre (the event log). Both take --dry-run. Nothing here needs admin rights.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

from . import installer, paths, procs
from .events import LOG_DIR
from .procs import list_processes
from .registry import HOME

PORT = int(os.environ.get("CADRE_PORT") or 8765)     # passed on to the dashboard as --port
URL = "http://127.0.0.1:%d/" % PORT
LABEL = "com.cadre.dashboard"
RUN_KEY = os.environ.get("CADRE_RUN_KEY", r"Software\Microsoft\Windows\CurrentVersion\Run")  # env: tests
RUN_VALUE = "cADRe"
START_WAIT = 15                                       # seconds start() waits for the dashboard to answer
PLIST = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")
UNIT = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                    "systemd", "user", "cadre.service")

LAUNCHER_CODE = '''"""Starts the cADRe dashboard from this folder (written by `cadre setup`; runs at login)."""
import errno
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# pythonw / launchd / systemd: log to a file. pythonw.exe always logs: started with stdout on
# NUL (setup, `cadre start`), isatty() is True, since Windows counts NUL as a character device.
if sys.stdout is None or not sys.stdout.isatty() or os.path.basename(sys.executable).lower() == "pythonw.exe":
    log_dir = os.environ.get("CADRE_HOME") or os.path.join(os.path.expanduser("~"), ".cadre")
    os.makedirs(log_dir, exist_ok=True)
    sys.stdout = sys.stderr = open(os.path.join(log_dir, "dashboard.log"), "a", encoding="utf-8", buffering=1)

from cadre.__main__ import main  # noqa: E402

# The status icon (macOS menu bar, Windows tray, Linux status icon) comes up with the dashboard, so the one
# autostart entry starts both; --no-menubar (or CADRE_MENUBAR=0) leaves it out. It is
# skipped if one already runs.
if "--no-menubar" in sys.argv:
    sys.argv.remove("--no-menubar")
elif sys.platform in ("darwin", "win32", "linux") and os.environ.get("CADRE_MENUBAR") != "0":
    try:
        from cadre import menubar
        port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else "8765"
        menubar.start(int(port))
    except Exception as e:                            # never keep the dashboard from starting
        print("cADRe: status icon not started: %s" % e)

try:
    main()
except OSError as e:
    # another dashboard already serves the port: exit cleanly so autostart doesn't loop
    if e.errno == errno.EADDRINUSE or getattr(e, "winerror", None) == 10048:
        print("cADRe: port already in use, another dashboard is running")
        sys.exit(0)
    raise
'''


def say(dry, msg):
    print(("  would " if dry else "  ") + msg)


def short(p):
    return p.replace(HOME, "~")


# ------------------------------------------------------------------ interpreters
def service_python():
    """Interpreter for the dashboard: a release's bundled Python if there is one, else
    Windows: pythonw.exe (no console window); elsewhere the hooks' Python."""
    bundled = paths.bundled_python(gui=True)
    if bundled:
        return bundled
    exe = sys.executable or "python"
    if paths.IS_WIN:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        return w if os.path.isfile(w) else exe
    return installer.PYTHON


# ------------------------------------------------------------------ the running dashboard
def port_busy():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def dashboard_info():
    """(pid, command line) of whatever cADRe answers on the port, or None."""
    try:
        req = urllib.request.Request(URL + "api/snapshot", headers={"Host": "127.0.0.1:%d" % PORT})
        with urllib.request.urlopen(req, timeout=5) as r:
            pid = (json.loads(r.read()).get("tracker") or {}).get("pid")
    except (OSError, ValueError):
        return None
    p = list_processes().get(pid) if pid else None
    return pid, (p or {}).get("args", "?")


def installed_pids():
    """Dashboards started from the install folder (matched by the launcher path)."""
    want = os.path.normcase(paths.LAUNCHER)
    return [p["pid"] for p in list_processes().values()
            if want in os.path.normcase(p.get("args") or "") and p["pid"] != os.getpid()]


def stop_icon(dry):
    """Quit the status icon, so setup restarts it from the new copy (the launcher does)."""
    from . import menubar
    if not menubar.supported():
        return
    pids = menubar.running()
    for pid in pids:
        say(dry, "quit the %s (pid %d)" % (menubar.NAME, pid))
    if pids and not dry:
        menubar.quit_running(pids)


def stop_installed(dry):
    if paths.IS_MAC and os.path.exists(PLIST):
        say(dry, "stop the LaunchAgent (%s)" % LABEL)
        if not dry:
            _run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)])
    elif not paths.IS_WIN and not paths.IS_MAC and os.path.exists(UNIT) and shutil.which("systemctl"):
        say(dry, "stop the systemd user service (cadre)")
        if not dry:
            _run(["systemctl", "--user", "stop", "cadre"])
    for pid in installed_pids():
        say(dry, "stop the installed dashboard (pid %d)" % pid)
        if not dry:
            try:
                os.kill(pid, signal.SIGTERM)          # TerminateProcess on Windows
            except OSError:
                pass
    if not dry:
        for _ in range(50):
            if not installed_pids():
                break
            time.sleep(0.1)


def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start(dry):
    if port_busy():
        info = dashboard_info()
        who = "a cADRe dashboard (pid %s: %s)" % info if info else "another program"
        print("  port %d is already used by %s; not starting a second one." % (PORT, who))
        print("    Stop it and run setup again, or log out and in: the installed one starts at login.")
        return False
    py = service_python()
    say(dry, "start the dashboard: %s %s" % (short(py), short(paths.LAUNCHER)))
    if dry:
        return True
    if paths.IS_MAC and os.path.exists(PLIST):
        _run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), PLIST]) or _run(["launchctl", "load", "-w", PLIST])
    elif not paths.IS_WIN and not paths.IS_MAC and os.path.exists(UNIT) and shutil.which("systemctl") \
            and _run(["systemctl", "--user", "restart", "cadre"]):
        pass
    else:
        # also Linux when systemd can't start it (no user manager: SSH without a session bus,
        # WSL or a container without systemd), which used to leave nothing running
        flags = 0x00000008 | 0x00000200 if paths.IS_WIN else 0   # DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([py, paths.LAUNCHER, "--port", str(PORT)], cwd=paths.APP, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=flags, start_new_session=not paths.IS_WIN)
    # By the clock, not by count: one failed try takes 2 s on Windows when nothing listens yet
    # (a refused connection is retried) and 5 s when the port is open but silent, so counting
    # 100 tries made setup, and the release installer waiting on it, sit for 3.5 to 8 minutes.
    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline:
        if port_busy() and dashboard_info():
            return True
        time.sleep(0.1)
    print("  the dashboard didn't answer within %d s; it may still be starting. See %s"
          % (START_WAIT, short(os.path.join(LOG_DIR, "dashboard.log"))))
    return False


def start_all():
    """`cadre start`: bring the installed dashboard and its status icon back up, e.g. after
    "Quit cADRe" in the icon's menu or `launchctl bootout`. Changes no files."""
    if not paths.installed():
        print("cADRe isn't installed for this user yet: run `python3 -m cadre setup`,")
        print("or `python3 -m cadre` to run the dashboard from this folder.")
        return 1
    from . import menubar
    if menubar.supported():              # before the dashboard, whose launcher would start it too
        menubar.start(PORT, paths.APP)
    info = dashboard_info()
    if info:
        print("cADRe dashboard is already running (pid %s): %s" % (info[0], URL))
        return 0
    if start(False):
        print("cADRe dashboard started: %s" % URL)
        return 0
    return 1


# ------------------------------------------------------------------ files
def copy_app(dry):
    if paths.running_from_install():
        print("  (running from the installed copy: files left as they are)")
        return
    say(dry, "copy cADRe to %s" % short(paths.APP))
    if dry:
        return
    os.makedirs(paths.APP, exist_ok=True)
    dest = os.path.join(paths.APP, "cadre")
    new, old = dest + ".new", dest + ".old"
    for d in (new, old):
        shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(paths.PKG, new, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.cadre-bak"))
    if os.path.exists(dest):
        os.replace(dest, old)          # swap quickly so hooks find a complete copy
    os.replace(new, dest)
    shutil.rmtree(old, ignore_errors=True)
    with open(paths.LAUNCHER, "w", encoding="utf-8", newline="\n") as f:
        f.write(LAUNCHER_CODE)


PIP_WAIT = 180      # seconds pip gets to fetch psutil


def ensure_psutil(dry, wanted=True):
    """Windows: fetch psutil into the install folder (paths.SITE) when this Python has none.
    Without it cADRe can't read a running agent's folder or environment there. Never fatal:
    offline or without pip, cADRe runs on the PowerShell backend and the dashboard says so."""
    if not paths.IS_WIN:
        return
    if paths.bundled_python():
        print("  psutil: comes with cADRe's own Python")
        return
    if procs.psutil:
        print("  psutil: found (%s)" % getattr(procs.psutil, "__version__", "?"))
        return
    if not wanted:
        print("  psutil: not installed, skipped (--no-psutil); folders of running agents won't be read")
        return
    say(dry, "fetch psutil from PyPI into %s" % short(paths.SITE))
    if dry:
        return
    cmd = [installer.PYTHON, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
           "--no-warn-script-location", "--only-binary=:all:", "--upgrade", "--target", paths.SITE, "psutil"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_WAIT,
                           encoding="utf-8", errors="replace", creationflags=0x08000000)
        err = "" if r.returncode == 0 else (r.stderr or r.stdout or "pip failed").strip().splitlines()[-1]
    except (OSError, subprocess.TimeoutExpired) as e:
        err = str(e) or type(e).__name__
    if not err and procs._load_psutil():
        print("  psutil: installed")
        return
    print("  psutil: NOT installed (%s)" % (err or "the fetched copy doesn't load in this Python"))
    print("  cADRe still runs, without the folders of running agents. Later: py -m pip install psutil")


# ------------------------------------------------------------------ autostart
def autostart_desc():
    if paths.IS_WIN:
        return "HKCU\\%s\\%s" % (RUN_KEY, RUN_VALUE)
    if paths.IS_MAC:
        return short(PLIST)
    return short(UNIT)


def add_autostart(dry):
    py = service_python()
    say(dry, "start it at login: %s" % autostart_desc())
    if dry:
        return
    if paths.IS_WIN:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, '"%s" "%s" --port %d' % (py, paths.LAUNCHER, PORT))
    elif paths.IS_MAC:
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, "w", encoding="utf-8") as f:
            f.write(_plist(py))
    else:
        os.makedirs(os.path.dirname(UNIT), exist_ok=True)
        with open(UNIT, "w", encoding="utf-8") as f:
            f.write("[Unit]\nDescription=cADRe dashboard\n\n[Service]\nWorkingDirectory=%s\n"
                    "ExecStart=\"%s\" \"%s\" --port %d\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n"
                    % (paths.APP, py, paths.LAUNCHER, PORT))
        if shutil.which("systemctl"):
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "cadre"])
        else:
            print("    systemctl not found: start %s at login yourself" % short(paths.LAUNCHER))


def _plist(py):
    from xml.sax.saxutils import escape
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '  <key>Label</key><string>%s</string>\n'
            '  <key>ProgramArguments</key><array><string>%s</string><string>%s</string>'
            '<string>--port</string><string>%d</string></array>\n'
            '  <key>WorkingDirectory</key><string>%s</string>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>\n'
            '</dict></plist>\n') % (LABEL, escape(py), escape(paths.LAUNCHER), PORT, escape(paths.APP))


def remove_autostart(dry):
    if paths.IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
                winreg.QueryValueEx(k, RUN_VALUE)
                say(dry, "remove the login entry %s" % autostart_desc())
                if not dry:
                    winreg.DeleteValue(k, RUN_VALUE)
        except OSError:
            pass
        return
    path = PLIST if paths.IS_MAC else UNIT
    if os.path.exists(path):
        say(dry, "remove %s" % short(path))
        if not dry:
            if not paths.IS_MAC and shutil.which("systemctl"):
                _run(["systemctl", "--user", "disable", "cadre"])
            os.remove(path)
            if not paths.IS_MAC and shutil.which("systemctl"):
                _run(["systemctl", "--user", "daemon-reload"])


# ------------------------------------------------------------------ commands
def setup(dry_run=False, autostart=True, start_now=True, browser=True, agents=None, psutil=True):
    print("cADRe setup%s\n" % ("  [dry run]" if dry_run else ""))
    print("1. Install")
    stop_installed(dry_run)
    if start_now:                        # it comes back with the dashboard, running the new copy
        stop_icon(dry_run)
    copy_app(dry_run)
    ensure_psutil(dry_run, psutil)
    print("\n2. Hooks")
    installer.use_emit(paths.APP_EMIT)
    installer.run(install=True, dry_run=dry_run, agents=agents)
    print("\n3. Autostart")
    if autostart:
        add_autostart(dry_run)
    else:
        print("  skipped (--no-autostart)")
    print("\n4. Dashboard")
    ok = start(dry_run) if start_now else print("  not started (--no-start)")
    if ok and browser and not dry_run:
        webbrowser.open(URL)
    print("\nDone%s. Dashboard: %s" % (" (dry run, nothing changed)" if dry_run else "", URL))
    print("Edge/Chrome: use 'Install app' in the address bar to give it its own window.")
    print(installer.python_note())
    return 0


def uninstall(dry_run=False, purge=False, files=True):
    """files=False: leave the install root alone (the Windows release's uninstaller deletes
    it itself, after this has removed the hooks and the autostart entry)."""
    print("cADRe uninstall%s\n" % ("  [dry run]" if dry_run else ""))
    print("1. Hooks")
    installer.run(install=False, dry_run=dry_run)
    print("\n2. Autostart and dashboard")
    remove_autostart(dry_run)
    stop_installed(dry_run)
    stop_icon(dry_run)
    print("\n3. Files")
    if not files:
        print("  left to the uninstaller: %s" % short(paths.ROOT))
    elif os.path.exists(paths.ROOT):
        if paths.running_from_install():
            print("  run uninstall from a git checkout to remove %s (it is running from there)"
                  % short(paths.ROOT))
        else:
            say(dry_run, "delete %s" % short(paths.ROOT))
            if not dry_run:
                shutil.rmtree(paths.ROOT, ignore_errors=True)
    else:
        print("  nothing installed at %s" % short(paths.ROOT))
    if purge:
        say(dry_run, "delete %s (event log)" % short(LOG_DIR))
        if not dry_run:
            shutil.rmtree(LOG_DIR, ignore_errors=True)
    else:
        print("  kept %s (event log); --purge deletes it" % short(LOG_DIR))
    print("\nDone%s." % (" (dry run, nothing changed)" if dry_run else ""))
    return 0

