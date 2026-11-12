"""The Linux status icon (cadre/indicator.py) and how `cadre menubar` starts it. The D-Bus
side needs a desktop session; the pure parts (icon pixels, labels, display variables, the
command line) are checked everywhere."""
import os
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

from cadre import indicator, menubar, statusmenu


def png(path, w, h, rows, kinds):
    """An RGBA PNG with the given filter type per row (rows hold the filtered bytes)."""
    raw = b"".join(bytes([k]) + r for k, r in zip(kinds, rows))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Pixmaps(unittest.TestCase):
    def test_shipped_icons_decode_to_argb(self):
        for name in indicator.ICONS.values():
            for n in indicator.SIZES:
                w, h, data = indicator.png_argb(os.path.join(indicator.STATIC, "%s-%d.png" % (name, n)))
                self.assertEqual((w, h, len(data)), (n, n, n * n * 4))
                self.assertEqual(data[:4], b"\0\0\0\0", "the tile's rounded corner is transparent")
        _, _, data = indicator.png_argb(os.path.join(indicator.STATIC, "indicator-24.png"))
        centre = (12 * 24 + 12) * 4
        self.assertEqual(data[centre:centre + 4], bytes((255, 0x2D, 0xD4, 0xBF)), "teal sensor, opaque, ARGB")

    def test_png_filters(self):
        px = [bytes((10, 20, 30, 255, 40, 50, 60, 128)), bytes((11, 22, 33, 255, 44, 55, 66, 129))]
        want = b"".join(bytes((r[i + 3], r[i], r[i + 1], r[i + 2])) for r in px for i in (0, 4))
        sub = lambda r: r[:4] + bytes((r[i] - r[i - 4]) & 255 for i in range(4, 8))      # noqa: E731
        up = bytes((b - a) & 255 for a, b in zip(px[0], px[1]))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.png")
            for kinds, rows in (((0, 0), px), ((1, 2), [sub(px[0]), up])):
                png(p, 2, 2, rows, kinds)
                self.assertEqual(indicator.png_argb(p)[2], want, kinds)
            with open(p, "wb") as f:
                f.write(b"GIF89a")
            self.assertRaises(ValueError, indicator.png_argb, p)


class Pure(unittest.TestCase):
    def test_underscores_survive_the_menu(self):
        self.assertEqual(indicator.menu_label("Claude Code · my_app   Bash"), "Claude Code · my__app   Bash")

    def test_display_env_fills_only_what_is_missing(self):
        manager = ["HOME=/home/u", "DISPLAY=:0", "WAYLAND_DISPLAY=wayland-0", "XDG_CURRENT_DESKTOP=ubuntu:GNOME",
                   "PATH=/bin", "XAUTHORITY="]
        self.assertEqual(indicator.display_env(manager, {"DBUS_SESSION_BUS_ADDRESS": "x"}),
                         {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"})
        self.assertEqual(indicator.display_env(manager, {"DISPLAY": ":1"}),
                         {"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"})
        self.assertEqual(indicator.display_env(None, {}), {})

    def test_menu_names_the_icon(self):
        rows = statusmenu.menu_items(None, None, False, "status icon")
        self.assertIn((statusmenu.CMD_QUIT_ICON, "Quit status icon only", True), rows)
        self.assertIn((statusmenu.CMD_START, "Start dashboard", True), rows)


class Starting(unittest.TestCase):
    def test_from_the_systemd_unit_it_gets_a_scope_of_its_own(self):
        with mock.patch.object(menubar.shutil, "which", return_value="/usr/bin/systemd-run"):
            argv = menubar.linux_command("/usr/bin/python3", "/x/cadre/indicator.py", 8765, {"INVOCATION_ID": "ab"})
            self.assertEqual(argv[:3], ["/usr/bin/systemd-run", "--user", "--scope"])
            self.assertEqual(argv[argv.index("--") + 1:], ["/usr/bin/python3", "/x/cadre/indicator.py", "--port", "8765"])
            self.assertEqual(menubar.linux_command("py", "s", 1, {}), ["py", "s", "--port", "1"])
        with mock.patch.object(menubar.shutil, "which", return_value=None):
            self.assertEqual(menubar.linux_command("py", "s", 1, {"INVOCATION_ID": "ab"}), ["py", "s", "--port", "1"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process names")
    def test_running_icon_is_recognised(self):
        me = os.getpid()
        self.assertTrue(menubar._is_icon({"pid": me + 1, "exe": "/usr/bin/python3.14",
                                          "args": "/usr/bin/python3 /home/u/.local/share/cadre/cadre/indicator.py --port 8765"}))
        self.assertFalse(menubar._is_icon({"pid": me + 1, "exe": "/usr/bin/vim", "args": "vim cadre/indicator.py"}))
        self.assertFalse(menubar._is_icon({"pid": me + 1, "exe": "/usr/bin/python3", "args": "python3 -m cadre"}))


if __name__ == "__main__":
    unittest.main()
