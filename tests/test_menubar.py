"""The macOS menu bar icon (cadre/menubar.js): which alerts it notifies for, and with what text.

The icon itself needs a logged-in Mac, but the alert rules are plain JavaScript between two
marker comments. They are cut out and run with osascript (JavaScript for Automation), next to
the same cases as the Windows tray and the Linux icon (cadre/statusmenu.py): they must agree.
"""
import json
import os
import re
import subprocess
import sys
import unittest

from cadre import menubar

with open(menubar.SCRIPT, encoding="utf-8") as _f:
    SOURCE = _f.read()
PURE = re.search(r"// alerts: begin.*?// alerts: end", SOURCE, re.S)


def tray_rules():
    """fresh_alerts / alert_text / short of cadre/statusmenu.py (the Windows tray and the Linux icon)."""
    from cadre import statusmenu
    return {k: getattr(statusmenu, k) for k in ("fresh_alerts", "alert_text", "short")}

A = {"ts": 100.0, "agent": "Claude Code", "project": "shop", "text": "recursive force delete (rm -rf build)"}
B = {"ts": 101.5, "agent": "Aider", "project": "api", "text": "reads a private key (~/.ssh/id_rsa)"}
CASES = [[A], [A, dict(A, ts=102, text="x" * 200)], [A, B], [A, B, A, B, A],
         [{"ts": 5, "text": "no agent  and\nodd   spacing"}], [dict(A, text='say "hi" \\ `id` $(id)')]]


def js(expr):
    out = subprocess.run([menubar.OSASCRIPT, "-l", "JavaScript", "-e",
                          PURE.group(0) + "\nJSON.stringify(%s)" % expr],
                         capture_output=True, text=True, timeout=30, encoding="utf-8")
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class Source(unittest.TestCase):
    def test_the_alert_rules_can_be_cut_out(self):
        self.assertTrue(PURE)
        for name in ("function short(", "function freshAlerts(", "function alertText("):
            self.assertIn(name, PURE.group(0))

    def test_alert_text_never_becomes_applescript(self):
        body = SOURCE[SOURCE.index("function notify("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("on run argv", body)
        self.assertNotRegex(body, r"display notification [\"']?\s*\+", "text spliced into the script")
        self.assertRegex(body, r"'end run', title, text\]")


@unittest.skipUnless(sys.platform == "darwin" and os.path.exists(menubar.OSASCRIPT), "needs osascript (macOS)")
class Alerts(unittest.TestCase):
    def test_each_alert_is_shown_once(self):
        snap = {"alerts": [B, A]}
        new, mark = js("freshAlerts(%s, 0)" % json.dumps(snap))
        self.assertEqual((new, mark), ([B, A], 101.5))
        self.assertEqual(js("freshAlerts(%s, 101.5)" % json.dumps(snap)), [[], 101.5])
        self.assertEqual(js("freshAlerts(%s, 100)" % json.dumps(snap)), [[B], 101.5])
        for nothing in ("null", "{}", '{"alerts": null}', '{"alerts": [{"text": "no time"}]}'):
            self.assertEqual(js("freshAlerts(%s, 7)" % nothing), [[], 7], nothing)

    def test_same_answers_as_the_windows_tray(self):
        tray = tray_rules()
        for alerts in CASES:
            self.assertEqual(js("freshAlerts(%s, 50)" % json.dumps({"alerts": alerts})),
                             list(tray["fresh_alerts"]({"alerts": alerts}, 50)), alerts)
            self.assertEqual(js("alertText(%s)" % json.dumps(alerts)), list(tray["alert_text"](alerts)), alerts)


if __name__ == "__main__":
    unittest.main()
