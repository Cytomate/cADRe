"""Which Python hooks, the dashboard and the tray run: a release's bundled one first, one
answer whoever installs (python.exe even from pythonw), and never the macOS /usr/bin/python3
stub. Also the release bootstrap (packaging/windows/cadre_release.py)."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from cadre import installer, paths, service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return path


class TempCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cadre-py-")
        self.addCleanup(shutil.rmtree, self.dir, True)


class BundledPython(TempCase):
    def test_windows_console_and_gui(self):
        py = os.path.join(self.dir, "python")
        with mock.patch.object(paths, "PYTHON_DIR", py), mock.patch.object(paths, "IS_WIN", True):
            self.assertIsNone(paths.bundled_python())
            exe, exew = touch(os.path.join(py, "python.exe")), touch(os.path.join(py, "pythonw.exe"))
            self.assertEqual(paths.bundled_python(), exe)
            self.assertEqual(paths.bundled_python(gui=True), exew)

    def test_posix_layout(self):
        py = os.path.join(self.dir, "python")
        with mock.patch.object(paths, "PYTHON_DIR", py), mock.patch.object(paths, "IS_WIN", False):
            self.assertIsNone(paths.bundled_python())
            exe = touch(os.path.join(py, "bin", "python3"))
            self.assertEqual(paths.bundled_python(), exe)
            self.assertEqual(paths.bundled_python(gui=True), exe)


class HookPython(TempCase):
    def test_bundled_wins_everywhere(self):
        for win, mac in ((True, False), (False, True), (False, False)):
            with mock.patch.object(paths, "bundled_python", return_value="/opt/cadre/python/bin/python3"), \
                    mock.patch.object(installer, "IS_WIN", win), mock.patch.object(installer, "IS_MAC", mac):
                self.assertEqual(installer.hook_python(), "/opt/cadre/python/bin/python3")

    def test_windows_pythonw_becomes_python(self):
        exe, exew = touch(os.path.join(self.dir, "python.exe")), touch(os.path.join(self.dir, "pythonw.exe"))
        with mock.patch.object(paths, "bundled_python", return_value=None), \
                mock.patch.object(installer, "IS_WIN", True):
            with mock.patch.object(installer.sys, "executable", exew):      # the dashboard
                self.assertEqual(installer.hook_python(), exe)
            with mock.patch.object(installer.sys, "executable", exe):       # py -m cadre setup
                self.assertEqual(installer.hook_python(), exe)

    def test_windows_pythonw_alone_is_kept(self):
        exew = touch(os.path.join(self.dir, "pythonw.exe"))
        with mock.patch.object(paths, "bundled_python", return_value=None), \
                mock.patch.object(installer, "IS_WIN", True), mock.patch.object(installer.sys, "executable", exew):
            self.assertEqual(installer.hook_python(), exew)

    def test_mac_system_python_only_when_real(self):
        with mock.patch.object(paths, "bundled_python", return_value=None), \
                mock.patch.object(installer, "IS_WIN", False), mock.patch.object(installer, "IS_MAC", True), \
                mock.patch.object(installer.sys, "executable", "/opt/homebrew/bin/python3.13"):
            with mock.patch.object(installer, "mac_system_python_ok", return_value=True):
                self.assertEqual(installer.hook_python(), "/usr/bin/python3")
            with mock.patch.object(installer, "mac_system_python_ok", return_value=False):
                self.assertEqual(installer.hook_python(), "/opt/homebrew/bin/python3.13")


class MacCommandLineTools(TempCase):
    def run_with(self, isfile, result=None, error=None):
        run = mock.Mock(side_effect=error) if error else mock.Mock(return_value=result)
        with mock.patch.object(installer.os.path, "isfile", side_effect=isfile), \
                mock.patch.object(installer.subprocess, "run", run):
            return installer.mac_system_python_ok(), run

    def test_command_line_tools_present(self):
        ok, run = self.run_with(lambda p: p == installer.CLT_PYTHON)
        self.assertTrue(ok)
        run.assert_not_called()                     # the common case needs no subprocess

    def test_xcode_app_selected(self):
        dev = "/Applications/Xcode.app/Contents/Developer"
        res = subprocess.CompletedProcess([], 0, stdout=dev + "\n", stderr="")
        ok, _ = self.run_with(lambda p: p == dev + "/usr/bin/python3", res)
        self.assertTrue(ok)

    def test_nothing_installed(self):
        res = subprocess.CompletedProcess([], 2, stdout="", stderr="xcode-select: error: unable to get active developer directory")
        self.assertFalse(self.run_with(lambda p: False, res)[0])
        self.assertFalse(self.run_with(lambda p: False, error=OSError("no xcode-select"))[0])

    def test_note_explains_the_fallback(self):
        with mock.patch.object(paths, "bundled_python", return_value=None), \
                mock.patch.object(installer, "IS_MAC", True), \
                mock.patch.object(installer, "PYTHON", "/opt/homebrew/bin/python3.13"):
            note = installer.python_note()
        self.assertIn("xcode-select --install", note)
        self.assertIn("/opt/homebrew/bin/python3.13", note)


class ServicePython(TempCase):
    def test_dashboard_uses_bundled_pythonw(self):
        with mock.patch.object(paths, "bundled_python", side_effect=lambda gui=False: "B-gui" if gui else "B"):
            self.assertEqual(service.service_python(), "B-gui")


class UninstallKeepsFiles(TempCase):
    def run_uninstall(self, files):
        root = os.path.join(self.dir, "cADRe")
        touch(os.path.join(root, "python", "python.exe"))
        with mock.patch.object(paths, "ROOT", root), \
                mock.patch.object(paths, "running_from_install", return_value=False), \
                mock.patch.object(installer, "run", return_value=0), \
                mock.patch.object(service, "remove_autostart"), mock.patch.object(service, "stop_installed"), \
                mock.patch.object(service, "stop_icon"), mock.patch("builtins.print"):
            service.uninstall(files=files)
        return os.path.exists(root)

    def test_files_false_leaves_the_root_to_the_uninstaller(self):
        self.assertTrue(self.run_uninstall(files=False))

    def test_default_deletes_the_root(self):
        self.assertFalse(self.run_uninstall(files=True))


class ReleaseBootstrap(TempCase):
    def load(self):
        spec = importlib.util.spec_from_file_location(
            "cadre_release", os.path.join(ROOT, "packaging", "windows", "cadre_release.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.LOG = os.path.join(self.dir, "setup.log")
        return mod

    def test_commands_call_service(self):
        mod = self.load()
        with mock.patch.object(service, "setup", return_value=0) as setup, \
                mock.patch.object(service, "uninstall", return_value=0) as uninstall, \
                mock.patch.object(service, "stop_installed") as stop, mock.patch.object(service, "stop_icon"), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(mod.main(["install", "--no-start"]), 0)
            setup.assert_called_once_with(autostart=True, start_now=False, browser=False)
            self.assertEqual(mod.main(["uninstall"]), 0)
            uninstall.assert_called_once_with(files=False)
            self.assertEqual(mod.main(["stop"]), 0)
            stop.assert_called_once_with(False)
            self.assertEqual(mod.main(["bogus"]), 2)
        with open(mod.LOG, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("release: install --no-start", log)
        self.assertIn("unknown command 'bogus'", log)

    def test_a_crash_is_logged_not_raised(self):
        mod = self.load()
        with mock.patch.object(service, "setup", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(mod.main(["install"]), 1)
        with open(mod.LOG, encoding="utf-8") as f:
            self.assertIn("RuntimeError: boom", f.read())

    def test_a_hanging_step_is_ended_and_the_log_says_where(self):
        # The installer waits for this process without a limit. Seen 2026-11-10: its window
        # stayed on "Installing hooks and starting cADRe..." and setup.log was still empty.
        code = ("import importlib.util, sys, time\n"
                "sys.path.insert(0, %r)\n"
                "from cadre import service\n"
                "spec = importlib.util.spec_from_file_location('cadre_release', %r)\n"
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                "mod.LOG, mod.LIMIT = %r, 1\n"
                "def stuck(**kw):\n"
                "    print('4. Dashboard')\n"
                "    time.sleep(60)\n"
                "service.setup = stuck\n"
                "sys.exit(mod.main(['install']))\n"
                ) % (ROOT, os.path.join(ROOT, "packaging", "windows", "cadre_release.py"),
                     os.path.join(self.dir, "setup.log"))
        started = time.monotonic()
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(r.returncode, 1)
        with open(os.path.join(self.dir, "setup.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("4. Dashboard", log)          # what ran before the hang was already on disk
        self.assertIn("Timeout", log)               # faulthandler's header
        self.assertIn("in stuck", log)              # and the stack names the step that hung


class StartWait(unittest.TestCase):
    def start(self, port_busy, info):
        with mock.patch.object(service, "START_WAIT", 0.5), \
                mock.patch.object(service, "port_busy", side_effect=port_busy), \
                mock.patch.object(service, "dashboard_info", side_effect=info) as dashboard_info, \
                mock.patch.object(service, "service_python", return_value="py"), \
                mock.patch.object(service.os.path, "exists", return_value=False), \
                mock.patch.object(service.subprocess, "Popen"), mock.patch("builtins.print") as out:
            started = time.monotonic()
            ok = service.start(False)
        return ok, time.monotonic() - started, dashboard_info, " ".join(str(c) for c in out.call_args_list)

    def test_gives_up_by_the_clock_when_nothing_listens(self):
        # 100 tries used to take 3.5 minutes on Windows, where a refused connection costs 2 s
        ok, took, dashboard_info, out = self.start(lambda: False, lambda: None)
        self.assertFalse(ok)
        self.assertLess(took, 3)
        dashboard_info.assert_not_called()          # no 2 s poll while the port is closed
        self.assertIn("didn't answer within", out)

    def test_returns_as_soon_as_the_dashboard_answers(self):
        busy = iter([False, False, True])           # the check before starting, then two polls
        ok, took, _, out = self.start(lambda: next(busy), lambda: (1, "x"))
        self.assertTrue(ok)
        self.assertLess(took, 0.5)
        self.assertNotIn("didn't answer", out)


class LinuxStart(unittest.TestCase):
    """Linux with a unit file: systemd starts the dashboard; when it can't (no user manager:
    SSH without a session bus, WSL, a container), start() runs it itself. It used to start
    nothing and wait out START_WAIT (seen 2026-11-12, Ubuntu)."""
    def start(self, systemctl_ok):
        with mock.patch.object(service, "port_busy", side_effect=[False, True]), \
                mock.patch.object(service, "dashboard_info", return_value=(1, "x")), \
                mock.patch.object(service, "service_python", return_value="py"), \
                mock.patch.object(service.paths, "IS_WIN", False), \
                mock.patch.object(service.paths, "IS_MAC", False), \
                mock.patch.object(service.os.path, "exists", side_effect=lambda p: p == service.UNIT), \
                mock.patch.object(service.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(service, "_run", return_value=systemctl_ok) as run, \
                mock.patch.object(service.subprocess, "Popen") as popen, mock.patch("builtins.print"):
            ok = service.start(False)
        return ok, run, popen

    def test_systemd_starts_it(self):
        ok, run, popen = self.start(True)
        self.assertTrue(ok)
        run.assert_called_once_with(["systemctl", "--user", "restart", "cadre"])
        popen.assert_not_called()

    def test_started_directly_when_systemd_cant(self):
        ok, run, popen = self.start(False)
        self.assertTrue(ok)
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0][:2], ["py", paths.LAUNCHER])


if __name__ == "__main__":
    unittest.main()
