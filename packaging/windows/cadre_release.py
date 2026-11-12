"""cADRe Windows release: the bridge between the installer (Inno Setup) and `cadre setup`.

The installer unpacks two folders into %LOCALAPPDATA%\\cADRe:

  python\\    the embeddable python.org build (+ psutil): cADRe's own interpreter
  release\\   this file and the cadre package of this release

and then runs this file with python\\python.exe:

  cadre_release.py install [--no-start] [--no-autostart]
        `cadre setup`: copy release\\cadre to app\\, hooks, autostart, start the dashboard
  cadre_release.py stop        before an upgrade replaces python\\: stop the dashboard and icon
  cadre_release.py uninstall   remove hooks and autostart, stop everything; the uninstaller
                               deletes the files (the event log in ~/.cadre stays)
  cadre_release.py open        the Start menu entry: start the dashboard (+ tray icon) if it
                               isn't running, then open it in the browser

Hooks, the dashboard and the tray then run python\\ (cadre.paths.bundled_python()), never
whatever Python is installed on the machine. Everything printed also goes to
release\\setup.log, since the installer runs this without a visible console.
"""
import faulthandler
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# the embeddable Python's ._pth file leaves the script's folder off sys.path
sys.path.insert(0, HERE)
LOG = os.path.join(HERE, "setup.log")
LIMIT = 120          # seconds one command may take before the watchdog ends it (a normal run: 5)


class Tee:
    """Write to the console (if any) and to setup.log."""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, text):
        for s in self.streams:
            try:
                s.write(text)
            except (OSError, ValueError):
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except (OSError, ValueError):
                pass


def run(cmd, extra):
    from cadre import __version__, service
    print("== cADRe %s release: %s %s (%s)" % (__version__, cmd, " ".join(extra),
                                                time.strftime("%Y-%m-%d %H:%M:%S")))
    if cmd == "install":
        return service.setup(autostart="--no-autostart" not in extra,
                             start_now="--no-start" not in extra, browser=False)
    if cmd == "stop":
        service.stop_installed(False)
        service.stop_icon(False)
        return 0
    if cmd == "uninstall":
        return service.uninstall(files=False)
    if cmd == "open":
        import webbrowser
        rc = service.start_all()
        if rc == 0:
            webbrowser.open(service.URL)
        return rc
    print("unknown command %r: use install, stop, uninstall or open" % cmd)
    return 2


def main(argv):
    cmd, extra = (argv[0], argv[1:]) if argv else ("install", [])
    try:
        # line-buffered: if a step hangs, the log must already show the steps before it
        log = open(LOG, "a", encoding="utf-8", buffering=1)
    except OSError:
        log = None
    sys.stdout = Tee(sys.stdout, log)
    sys.stderr = Tee(sys.stderr, log)
    watchdog(log)
    try:
        return run(cmd, extra)
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        faulthandler.cancel_dump_traceback_later()
        sys.stdout.flush()
        if log:
            log.close()


def watchdog(log):
    """The installer waits for this process without a time limit and shows no output, so a
    step that hangs would leave its window stuck for good. After LIMIT seconds, write where
    every thread is to setup.log and exit with code 1 (the installer then points at the log)."""
    if log:
        log.write("(gives up after %d s)\n" % LIMIT)
        faulthandler.dump_traceback_later(LIMIT, exit=True, file=log)
    else:
        t = threading.Timer(LIMIT, os._exit, (1,))
        t.daemon = True
        t.start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
