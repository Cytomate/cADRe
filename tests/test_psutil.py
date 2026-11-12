"""psutil on Windows: `setup` fetches it into the install folder, procs loads it from there,
and the snapshot says so when it is missing. pip is never run and nothing is downloaded here.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cadre import paths, procs, service


class LoadFromTheInstallFolder(unittest.TestCase):
    NAME = "cadre_fake_psutil"            # a stand-in, so the test doesn't depend on a real psutil

    def setUp(self):
        self.site = tempfile.mkdtemp(prefix="cadre-site-")
        self.addCleanup(shutil.rmtree, self.site, True)
        self.addCleanup(lambda: self.site in sys.path and sys.path.remove(self.site))
        self.addCleanup(sys.modules.pop, self.NAME, None)

    def package(self, code):
        os.makedirs(os.path.join(self.site, self.NAME))
        with open(os.path.join(self.site, self.NAME, "__init__.py"), "w", encoding="utf-8") as f:
            f.write(code)

    def test_missing_everywhere(self):
        self.assertIsNone(procs._load_psutil(self.site, self.NAME))
        self.assertNotIn(self.site, sys.path)

    def test_found_in_the_install_folder(self):
        self.package("__version__ = 'fake'\n")
        mod = procs._load_psutil(self.site, self.NAME)
        self.assertEqual(mod.__version__, "fake")
        self.assertEqual(sys.path[-1], self.site)          # appended: it shadows nothing

    def test_a_copy_that_does_not_load_is_left_out(self):
        self.package("raise OSError('DLL load failed')\n")  # built for another Python or CPU
        self.assertIsNone(procs._load_psutil(self.site, self.NAME))
        self.assertNotIn(self.site, sys.path)
        self.assertNotIn(self.NAME, sys.modules)


class Limited(unittest.TestCase):
    def test_only_windows_without_psutil_is_limited(self):
        for win, ps, limited in ((True, None, True), (True, object(), False), (False, None, False)):
            with mock.patch.object(procs, "IS_WIN", win), mock.patch.object(procs, "psutil", ps):
                self.assertEqual(bool(procs.limited()), limited, (win, ps))


class SetupFetchesIt(unittest.TestCase):
    def run_it(self, win=True, bundled=None, have=None, dry=False, wanted=True, pip=None, loads=True):
        out = io.StringIO()
        with mock.patch.object(paths, "IS_WIN", win), \
                mock.patch.object(paths, "SITE", os.path.join("X:\\", "cADRe", "site")), \
                mock.patch.object(paths, "bundled_python", return_value=bundled), \
                mock.patch.object(procs, "psutil", have), \
                mock.patch.object(procs, "_load_psutil", return_value=object() if loads else None), \
                mock.patch.object(subprocess, "run", side_effect=pip) as run, \
                contextlib.redirect_stdout(out):
            service.ensure_psutil(dry, wanted)
        return run, out.getvalue()

    @staticmethod
    def done(code=0, err=""):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, "", err)

    def test_pip_puts_it_in_the_install_folder_only(self):
        run, out = self.run_it(pip=self.done())
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:4], ["-m", "pip", "install"])
        self.assertEqual(cmd[cmd.index("--target") + 1], os.path.join("X:\\", "cADRe", "site"))
        self.assertIn("--only-binary=:all:", cmd)          # never compile on the user's machine
        self.assertNotIn("--user", cmd)
        self.assertEqual(cmd[-1], "psutil")
        self.assertIn("psutil: installed", out)

    def test_nothing_to_do(self):
        for kw in ({"win": False}, {"bundled": "python.exe"}, {"have": mock.Mock(__version__="7")},
                   {"dry": True}, {"wanted": False}):
            with self.subTest(**kw):
                run, _ = self.run_it(pip=self.done(), **kw)
                run.assert_not_called()

    def test_a_dry_run_says_what_it_would_do(self):
        self.assertIn("would fetch psutil from PyPI", self.run_it(dry=True)[1])

    def test_failure_is_reported_and_never_raised(self):
        offline = self.done(1, "ERROR: Could not find a version\nERROR: No matching distribution found for psutil")
        for pip, loads, text in ((offline, True, "No matching distribution"),
                                 (OSError("no pip"), True, "no pip"),
                                 (subprocess.TimeoutExpired("pip", 1), True, "NOT installed"),
                                 (self.done(), False, "doesn't load")):
            with self.subTest(text=text):
                _, out = self.run_it(pip=pip, loads=loads)
                self.assertIn("psutil: NOT installed", out)
                self.assertIn(text, out)
                self.assertIn("cADRe still runs", out)


if __name__ == "__main__":
    unittest.main()
