"""Codex session reader, against a synthetic rollout file in the 0.167 format."""
import json
import os
import shutil
import tempfile
import time
import unittest

from cadre import codex

SID = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


def line(t, payload):
    return json.dumps({"timestamp": "2026-11-08T10:00:00.000Z", "type": t, "payload": payload})


class Rollout(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cadre-codex-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file = os.path.join(self.dir, "rollout-2026-11-08T10-00-00-%s.jsonl" % SID)
        self.lines = [
            line("session_meta", {"id": SID, "cwd": "/w/proj", "timestamp": "2026-11-08T10:00:00.000Z",
                                  "cli_version": "0.167.0", "source": "cli", "git": {"branch": "main"}}),
            line("turn_context", {"cwd": "/w/proj", "model": "gpt-5-codex",
                                  "sandbox_policy": {"type": "workspace-write"}, "approval_policy": "on-request"}),
            line("response_item", {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}),
            line("response_item", {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": "run the tests"}]}),
            line("event_msg", {"type": "task_started"}),
            line("response_item", {"type": "custom_tool_call", "name": "exec",
                                   "input": 'await tools.exec_command({cmd:"git status --short"})'}),
        ]
        self.write()

    def write(self):
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")
        codex._summary_cache.pop(self.file, None)
        codex._meta_cache.pop(self.file, None)

    def test_summary(self):
        info = codex.read_session(self.file)
        self.assertEqual(info["session_id"], SID)
        self.assertEqual((info["cwd"], info["git_branch"], info["model"]), ("/w/proj", "main", "gpt-5-codex"))
        self.assertEqual((info["permission_mode"], info["approval_policy"]), ("workspace-write", "on-request"))
        self.assertEqual(info["last_prompt"], "run the tests")
        self.assertEqual(info["last_tool"], "exec_command: git status --short")
        self.assertEqual(info["tool_calls"], 1)
        self.assertTrue(info["turn_open"])
        self.assertTrue(codex.turn_running(info, time.time()))
        self.assertFalse(codex.turn_running(info, time.time() + codex.TURN_STALE_SECS + 5))

    def test_turn_complete(self):
        self.lines.append(line("event_msg", {"type": "task_complete"}))
        self.write()
        self.assertFalse(codex.read_session(self.file)["turn_open"])

    def test_bad_lines_are_skipped(self):
        self.lines.insert(2, "{not json")
        self.lines.append('{"type": "response_item", "payload": "oops"}')
        self.write()
        self.assertEqual(codex.read_session(self.file)["last_prompt"], "run the tests")

    def test_find_live_session(self):
        start = os.path.getmtime(self.file) - 1
        self.assertEqual(codex.find_live_session([self.file], "/w/proj", start), self.file)
        self.assertIsNone(codex.find_live_session([self.file], "/w/other", start))
        self.assertEqual(codex.find_live_session([self.file], None, start), self.file)
        self.assertEqual(codex.find_live_session([self.file], "/w/other", None, resume_id=SID), self.file)
        self.assertIsNone(codex.find_live_session([self.file], "/w/proj", start, claimed={self.file}))


class Helpers(unittest.TestCase):
    def test_describe_tool(self):
        self.assertEqual(codex.describe_tool("shell", {"command": ["bash", "-lc", "ls -la"]}), "shell: ls -la")
        self.assertEqual(codex.describe_tool("shell", json.dumps({"command": ["powershell", "-Command", "dir"]})),
                         "shell: dir")
        self.assertEqual(codex.describe_tool("apply_patch", "*** Begin Patch\n*** Update File: src/a.py\n"),
                         "apply_patch: src/a.py")
        self.assertEqual(codex.describe_tool("web_search", {"query": "python"}), "web_search: python")
        self.assertEqual(codex.describe_tool("noop", {}), "noop")

    def test_session_dir_and_resume(self):
        self.assertEqual(codex.session_dir("codex -C sub", "/w"), os.path.normpath("/w/sub"))
        # a real absolute path for this OS ("/abs dir" has no drive, so it isn't absolute on Windows)
        absdir = os.path.join(os.path.abspath(os.sep), "abs dir")
        self.assertEqual(codex.session_dir('codex --cd "%s"' % absdir, "/w"), absdir)
        self.assertEqual(codex.session_dir("codex", "/w"), "/w")
        self.assertEqual(codex.resume_id("codex resume --last %s" % SID), SID)
        self.assertIsNone(codex.resume_id("codex resume"))


if __name__ == "__main__":
    unittest.main()
