"""cadre/procs.py's cgroup freezer helpers (Linux), on a fake cgroup tree in a temp folder."""
import os
import tempfile
import unittest
from unittest import mock

from cadre import procs


class Freeze(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cg = "/user.slice/app.slice/vte-spawn-1a2b-3c.scope"
        d = os.path.join(self.root, self.cg.lstrip("/"))
        os.makedirs(d)
        self.dir = d
        with open(os.path.join(d, "cgroup.procs"), "w") as f:
            f.write("400\n500\n")
        self.events(0)
        for name, value in (("cadre.procs.CGROOT", self.root), ("cadre.procs._frozen", set()),
                            ("cadre.procs.FREEZE_WAIT", 0.2)):
            p = mock.patch(name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def events(self, frozen):
        with open(os.path.join(self.dir, "cgroup.events"), "w") as f:
            f.write("populated 1\nfrozen %d\n" % frozen)

    def test_which_scopes_are_a_terminal_tab_or_pane(self):
        for name in ("vte-spawn-11f829b4-5694-4d12-9109-3f58d16d8226.scope",
                     "ptyxis-spawn-5107cedc-0dda-403b-a1e9-a9a0facde656.scope",
                     "tmux-spawn-ae4ab968-2f22-49cb-886b-0d0046aca21e.scope"):
            self.assertTrue(procs.TERMINAL_SCOPE.match(name), name)
        for name in ("cadre.service", "app-gnome-firefox-1234.scope", "session-2.scope",
                     "cadre-icon-7376.scope", "vte-spawn-1.scope/../x"):
            self.assertFalse(procs.TERMINAL_SCOPE.match(name), name)

    def test_paths_stay_under_the_cgroup_root(self):
        self.assertRaises(OSError, procs._cg_file, "/../../etc", "passwd")
        self.assertEqual(procs.cgroup_pids("/../../etc"), [])

    def test_frozen_members_are_listed_as_stopped(self):
        with open(os.path.join(self.dir, "cgroup.freeze"), "w") as f:
            f.write("0")
        self.events(1)                         # the kernel's answer to the write below
        procs.freeze(self.cg)
        with open(os.path.join(self.dir, "cgroup.freeze")) as f:
            self.assertEqual(f.read(), "1")
        listing = {400: {"pid": 400, "stat": "Ss"}, 500: {"pid": 500, "stat": "Sl+"}, 9: {"pid": 9, "stat": "S"}}
        procs._mark_frozen(listing)
        self.assertEqual([listing[x]["stat"] for x in (400, 500, 9)], ["Ts", "Tl+", "S"])
        self.events(0)                         # thawed by someone else: forgotten
        listing[500]["stat"] = "S+"
        procs._mark_frozen(listing)
        self.assertEqual(listing[500]["stat"], "S+")
        self.assertEqual(procs._frozen, set())

    def test_a_freeze_the_kernel_never_confirms_is_an_error(self):
        open(os.path.join(self.dir, "cgroup.freeze"), "w").close()
        self.assertRaises(OSError, procs.freeze, self.cg)
        self.assertEqual(procs._frozen, set())


if __name__ == "__main__":
    unittest.main()
