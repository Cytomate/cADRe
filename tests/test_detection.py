"""Recognising agents from process names, and parsing process lists (registry, collectors, procs)."""
import json
import os
import unittest
from unittest import mock

from cadre import procs, registry
from cadre.collectors import match_spec


def key(exe, args=""):
    spec = match_spec({"exe": exe, "args": args or exe})
    return spec.key if spec else None


class MatchSpec(unittest.TestCase):
    def test_posix_names(self):
        self.assertEqual(key("claude"), "claude-code")
        self.assertEqual(key("/Users/x/.local/bin/claude"), "claude-code")
        self.assertEqual(key("/Applications/Claude.app/Contents/MacOS/Claude"), "claude-desktop")
        self.assertEqual(key("codex"), "codex")
        self.assertEqual(key("/opt/homebrew/bin/gemini"), "gemini")
        self.assertEqual(key("/Applications/Cursor.app/Contents/MacOS/Cursor"), "cursor")
        self.assertEqual(key("cursor-agent"), "cursor")
        self.assertEqual(key("/Applications/ZCode.app/Contents/MacOS/ZCode"), "zcode")
        self.assertEqual(key("ollama"), "ollama")

    def test_not_agents(self):
        for exe in ("/bin/zsh", "/usr/bin/python3", "claude-helper", "/usr/bin/codexx", "node", "vim"):
            self.assertIsNone(key(exe), exe)
        self.assertIsNone(key("/usr/bin/python3", "python3 -m http.server"))

    def test_windows_claude_code_vs_desktop(self):
        self.assertEqual(key(r"C:\Users\x\.local\bin\claude.exe"), "claude-code")
        self.assertEqual(key("CLAUDE.EXE"), "claude-code")
        self.assertEqual(key(r"C:\Users\x\AppData\Local\AnthropicClaude\app-1.0\claude.exe"), "claude-desktop")
        self.assertEqual(key(r"C:\Program Files\WindowsApps\Claude_1.2.3.0_x64__pzs8sxrjxfjjc\app\claude.exe"),
                         "claude-desktop")

    def test_windows_others(self):
        self.assertEqual(key(r"C:\Users\x\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe"), "codex")
        self.assertEqual(key(r"C:\Users\x\AppData\Local\Programs\cursor\Cursor.exe"), "cursor")
        self.assertEqual(key(r"C:\Users\x\AppData\Local\Programs\Ollama\ollama app.exe"), "ollama")

    def test_node_launched_clis(self):
        self.assertEqual(key("node", "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"),
                         "claude-code")
        self.assertEqual(key("node", "node /usr/local/bin/codex"), "codex")
        self.assertEqual(key(r"C:\Program Files\nodejs\node.exe",
                             r'"C:\Program Files\nodejs\node.exe" C:\npm\node_modules\@openai\codex\bin\codex.js'),
                         "codex")
        self.assertEqual(key("node", "node /x/gemini --yolo"), "gemini")
        # an editor that merely opens a file called codex isn't Codex
        self.assertIsNone(key("vim", "vim codex"))

    def test_codex_daemon_excluded(self):
        self.assertIsNone(key("/Users/x/.codex/packages/app-server-daemon/bin/codex",
                              "/Users/x/.codex/packages/app-server-daemon/bin/codex app-server"))


class Expand(unittest.TestCase):
    def test_home_and_vars(self):
        with mock.patch.dict(os.environ, {"CADRE_T_VAR": os.path.join(os.sep, "data")}):
            self.assertEqual(registry.expand("%CADRE_T_VAR%/x") if os.name == "nt"
                             else registry.expand("$CADRE_T_VAR/x"), os.path.normpath("/data/x"))
        self.assertEqual(registry.expand("~/a"), os.path.normpath(os.path.expanduser("~/a")))

    def test_unset_var_never_matches(self):
        os.environ.pop("CADRE_T_UNSET", None)
        self.assertEqual(registry.expand("%CADRE_T_UNSET%/x"), "\0unset")
        self.assertFalse(os.path.exists(registry.expand("%CADRE_T_UNSET%/x")))


class ParseWindows(unittest.TestCase):
    ROWS = [
        {"ProcessId": 4242, "ParentProcessId": 100, "Name": "claude.exe",
         "ExecutablePath": r"C:\Users\x\.local\bin\claude.exe", "CommandLine": "claude --resume",
         "WorkingSetSize": 104857600, "KernelModeTime": 10000000, "UserModeTime": 20000000, "Start": 1000},
        {"ProcessId": 4, "ParentProcessId": 0, "Name": "System", "ExecutablePath": None,
         "CommandLine": None, "WorkingSetSize": None, "KernelModeTime": None, "UserModeTime": None, "Start": None},
        {"ProcessId": None, "Name": "broken"},
    ]

    def test_list(self):
        p = procs.parse_windows(json.dumps(self.ROWS), now=1600)
        self.assertEqual(sorted(p), [4, 4242])
        c = p[4242]
        self.assertEqual((c["ppid"], c["rss_mb"], c["uptime_s"], c["args"]), (100, 100.0, 600, "claude --resume"))
        self.assertEqual(c["exe"], r"C:\Users\x\.local\bin\claude.exe")
        self.assertEqual((p[4]["exe"], p[4]["args"], p[4]["uptime_s"]), ("System", "System", 0))

    def test_single_object_and_junk(self):
        # ConvertTo-Json emits an object, not a list, for exactly one process
        self.assertEqual(list(procs.parse_windows(json.dumps(self.ROWS[0]), now=1600)), [4242])
        self.assertEqual(procs.parse_windows(""), {})
        self.assertEqual(procs.parse_windows("WARNING: not json"), {})

    def test_cpu_from_deltas(self):
        procs._prev_cpu.pop(77, None)
        row = dict(self.ROWS[0], ProcessId=77, KernelModeTime=0, UserModeTime=0)
        self.assertEqual(procs.parse_windows(json.dumps([row]), now=100)[77]["cpu"], 0.0)
        row["UserModeTime"] = 5000000                 # 0.5 s of CPU over 1 s
        self.assertEqual(procs.parse_windows(json.dumps([row]), now=101)[77]["cpu"], 50.0)


class Etime(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(procs._etime_to_secs("05"), 5)
        self.assertEqual(procs._etime_to_secs("01:05"), 65)
        self.assertEqual(procs._etime_to_secs("02:01:05"), 7265)
        self.assertEqual(procs._etime_to_secs("3-02:01:05"), 3 * 86400 + 7265)


if __name__ == "__main__":
    unittest.main()
