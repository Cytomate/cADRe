"""Dashboard server guards: loopback Host check and the /api/hooks request body."""
import unittest
from unittest import mock

from cadre import __main__ as server


class HostCheck(unittest.TestCase):
    def test_loopback_only(self):
        names = server.allowed_hosts("127.0.0.1")
        for ok in ("127.0.0.1:8765", "localhost:8765", "LOCALHOST", "[::1]:8765"):
            self.assertTrue(server.host_ok(ok, names), ok)
        for bad in (None, "", "evil.com", "evil.com:8765", "127.0.0.1.evil.com", "[::2]:8765"):
            self.assertFalse(server.host_ok(bad, names), bad)


class InstallHooksBody(unittest.TestCase):
    def test_bad_bodies_are_rejected(self):
        for body in ([], "all", {}, {"paths": []}, {"paths": "x"}, {"paths": [1]}):
            with self.assertRaises(ValueError, msg=repr(body)):
                server.install_hooks(body)

    def test_paths_and_all(self):
        with mock.patch("cadre.installer.install_paths", return_value={"results": [], "notes": []}) as ip:
            server.install_hooks({"paths": ["/a"]})
            ip.assert_called_with(["/a"], dry_run=False)
            server.install_hooks({"all": True, "paths": ["/ignored"], "dry_run": True})
            ip.assert_called_with(None, dry_run=True)


if __name__ == "__main__":
    unittest.main()
