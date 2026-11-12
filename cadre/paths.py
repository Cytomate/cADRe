"""Where `cadre setup` installs cADRe for this user (see cadre/service.py).

  Windows  %LOCALAPPDATA%\\cADRe\\app
  macOS    ~/Library/Application Support/cADRe/app
  Linux    $XDG_DATA_HOME/cadre/app (default ~/.local/share/cadre/app)

The installed copy is what hooks and autostart run, so moving or re-cloning the git checkout
no longer breaks them. Data (the event log) stays in ~/.cadre.

A release (Windows installer) also puts its own Python next to the app, in <root>/python:
the embeddable python.org build. When it is there, hooks, the dashboard and the tray icon
run it instead of whatever Python ran `setup` (see bundled_python()).
"""
import os
import sys

from .registry import HOME

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def app_root():
    if IS_WIN:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local"), "cADRe")
    if IS_MAC:
        return os.path.join(HOME, "Library", "Application Support", "cADRe")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "cadre")


ROOT = app_root()
APP = os.path.join(ROOT, "app")
LAUNCHER = os.path.join(APP, "cadre_service.py")
APP_EMIT = os.path.join(APP, "cadre", "hooks", "emit.py")
PKG = os.path.dirname(os.path.abspath(__file__))
PKG_EMIT = os.path.join(PKG, "hooks", "emit.py")
PYTHON_DIR = os.path.join(ROOT, "python")       # a release's own interpreter, if any
SITE = os.path.join(ROOT, "site")               # psutil, fetched by `setup` on Windows (cadre/procs.py)


def bundled_python(gui=False):
    """The interpreter a release ships in the install root, or None.

    Windows: python.exe (hooks: console, waits and takes the payload on stdin like any
    console program) or, with gui=True, pythonw.exe (dashboard and tray: no console window).
    Elsewhere: bin/python3 (no release bundles one yet; kept so the layout is the same).
    """
    if IS_WIN:
        exe = os.path.join(PYTHON_DIR, "pythonw.exe" if gui else "python.exe")
    else:
        exe = os.path.join(PYTHON_DIR, "bin", "python3")
    return exe if os.path.isfile(exe) else None


def installed():
    return os.path.isfile(APP_EMIT) and os.path.isfile(LAUNCHER)


def running_from_install():
    return os.path.normcase(PKG) == os.path.normcase(os.path.join(APP, "cadre"))
