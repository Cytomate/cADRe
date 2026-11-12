"""emit.py: payload mapping per agent, and the promise never to disturb an agent
(no output, exit 0, short summaries only), checked by running it the way hooks do."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

EMIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cadre", "hooks", "emit.py")
_spec = importlib.util.spec_from_file_location("cadre_emit_under_test", EMIT)
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)


def build(payload, agent, event=None, env=None):
    with mock.patch.dict(os.environ, env or {}):
        for k in ("DEVIN_PROJECT_DIR", "GEMINI_SESSION_ID", "GEMINI_CWD"):
            if not env or k not in env:
                os.environ.pop(k, None)
        return emit.build(payload, agent, event)


class Mapping(unittest.TestCase):
    def test_claude_code_tool(self):
        ev = build({"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/p",
                    "tool_name": "Bash", "tool_input": {"command": "git status", "description": "Show status"},
                    "tool_response": {"stdout": "SECRET OUTPUT"}, "permission_mode": "default"},
                   "claude-code", "PostToolUse")
        self.assertEqual((ev["agent"], ev["event"], ev["tool"], ev["detail"]),
                         ("claude-code", "PostToolUse", "Bash", "Show status"))
        self.assertEqual(ev["session_id"], "s1")
        self.assertNotIn("SECRET", json.dumps(ev))

    def test_claude_code_failure_and_prompt_cut(self):
        ev = build({"session_id": "s", "tool_name": "Edit", "tool_input": {"file_path": "/a.py"}},
                   "claude-code", "PostToolUseFailure")
        self.assertTrue(ev["failed"])
        self.assertEqual(ev["detail"], "/a.py")
        ev = build({"session_id": "s", "prompt": "x" * 5000}, "claude-code", "UserPromptSubmit")
        self.assertEqual(len(ev["detail"]), 200)
        self.assertTrue(ev["detail"].endswith("…"))

    def test_devin_relabel(self):
        ev = build({"session_id": "s"}, "claude-code", "Stop", env={"DEVIN_PROJECT_DIR": "/proj"})
        self.assertEqual((ev["agent"], ev["cwd"]), ("devin", "/proj"))

    def test_copilot_camelcase_and_notifications(self):
        ev = build({"sessionId": "c1", "cwd": "/p", "toolName": "bash",
                    "toolArgs": json.dumps({"command": "ls -la"})}, "copilot", "postToolUse")
        self.assertEqual((ev["event"], ev["tool"], ev["detail"], ev["session_id"]),
                         ("PostToolUse", "bash", "ls -la", "c1"))
        self.assertEqual(build({"prompt": "hi"}, "copilot", "userPromptSubmitted")["event"], "UserPromptSubmit")
        self.assertEqual(build({"notification_type": "agent_idle"}, "copilot", "notification")["event"], "Stop")
        self.assertEqual(build({"notification_type": "shell_completed"}, "copilot", "notification")["event"], "Info")
        self.assertEqual(build({"notification_type": "permission_prompt"}, "copilot", "notification")["event"],
                         "Notification")
        err = build({"error": "boom"}, "copilot", "errorOccurred")
        self.assertEqual((err["event"], err["detail"], err["failed"]), ("ErrorOccurred", "boom", True))

    def test_gemini(self):
        ev = build({"tool_name": "run_shell_command", "tool_input": {"command": "make"}}, "gemini", "AfterTool",
                   env={"GEMINI_SESSION_ID": "g1", "GEMINI_CWD": "/g"})
        self.assertEqual((ev["event"], ev["session_id"], ev["cwd"], ev["detail"]),
                         ("PostToolUse", "g1", "/g", "make"))
        self.assertEqual(build({}, "gemini", "BeforeAgent")["event"], "UserPromptSubmit")
        self.assertEqual(build({}, "gemini", "AfterAgent")["event"], "Stop")

    def test_cursor(self):
        ev = build({"conversation_id": "k1", "workspace_roots": ["/w"], "prompt": "fix it"},
                   "cursor", "beforeSubmitPrompt")
        self.assertEqual((ev["event"], ev["session_id"], ev["cwd"], ev["detail"]),
                         ("UserPromptSubmit", "k1", "/w", "fix it"))

    def test_windsurf(self):
        ev = build({"trajectory_id": "w1", "agent_action_name": "post_run_command",
                    "tool_info": {"command_line": "npm test", "cwd": "/ws"}}, "windsurf", "post_run_command")
        self.assertEqual((ev["event"], ev["tool"], ev["detail"], ev["cwd"]),
                         ("PostToolUse", "run_command", "npm test", "/ws"))
        ev = build({"tool_info": {"user_prompt": "hello"}}, "windsurf", "pre_user_prompt")
        self.assertEqual((ev["event"], ev["detail"]), ("UserPromptSubmit", "hello"))
        self.assertEqual(build({}, "windsurf", "post_cascade_response")["event"], "Stop")

    def test_codex_session_start(self):
        ev = build({"session_id": "x", "source": "startup"}, "codex", "SessionStart")
        self.assertEqual(ev["detail"], "startup")

    def test_fallback_session_id_and_no_nones(self):
        ev = build({}, "cursor", "stop")
        self.assertEqual(ev["session_id"], "cursor:unknown")
        self.assertNotIn(None, ev.values())

    def test_summaries_are_short(self):
        self.assertEqual(emit.summarise_tool({"content": "file body", "file_path": "/x"}), "/x")
        self.assertEqual(emit.summarise_tool({"content": "file body only"}), "")
        self.assertLessEqual(len(emit.summarise_tool({"command": "y" * 999})), 160)
        self.assertEqual(emit.summarise_tool("not json"), "not json")
        self.assertEqual(emit.short("a \n\t b", 10), "a b")


class RunAsHook(unittest.TestCase):
    """Runs emit.py like an agent does: `python -I -S emit.py --agent .. --event ..` with stdin."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cadre-emit-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.log = os.path.join(self.home, "events.jsonl")

    def run_hook(self, stdin, *args, env=None):
        e = {k: v for k, v in os.environ.items() if k not in ("CADRE_URL", "CADRE_HOOKS", "DEVIN_PROJECT_DIR")}
        e["CADRE_HOME"] = self.home
        e.update(env or {})
        p = subprocess.run([sys.executable, "-I", "-S", EMIT] + list(args),
                           input=stdin if isinstance(stdin, bytes) else stdin.encode("utf-8"),
                           capture_output=True, env=e, timeout=20)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        self.assertEqual(p.stderr, b"")

    def events(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_event_is_logged(self):
        self.run_hook(json.dumps({"session_id": "s", "prompt": "héllo ✓"}),
                      "--agent", "claude-code", "--event", "UserPromptSubmit")
        evs = self.events()
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["event"], evs[0]["detail"]), ("UserPromptSubmit", "héllo ✓"))

    def test_payload_with_bom_is_logged(self):
        # PowerShell can pipe UTF-8 with a byte-order mark; json.loads rejects a BOM
        self.run_hook(b"\xef\xbb\xbf" + json.dumps({"session_id": "s", "toolName": "bash"}).encode(),
                      "--agent", "copilot", "--event", "postToolUse")
        self.assertEqual([e["tool"] for e in self.events()], ["bash"])

    def test_bad_input_is_silent(self):
        for stdin in (b"", b"not json", b"[1,2]", b"\xff\xfe\x00garbage", b"{" * 10000):
            self.run_hook(stdin, "--agent", "codex", "--event", "Stop")
        self.run_hook("{}")                       # no arguments at all
        self.run_hook("{}", "--agent")            # dangling flag

    def test_off_switches(self):
        self.run_hook("{}", "--agent", "codex", "--event", "Stop", env={"CADRE_HOOKS": "0"})
        open(os.path.join(self.home, "paused"), "w").close()
        self.run_hook("{}", "--agent", "codex", "--event", "Stop")
        self.assertEqual(self.events(), [])

    def test_unreachable_url_is_silent(self):
        self.run_hook('{"session_id": "s"}', "--agent", "codex", "--event", "Stop",
                      env={"CADRE_URL": "http://127.0.0.1:9/api/events"})
        self.assertEqual(len(self.events()), 1)

    def test_unwritable_log_is_silent(self):
        blocker = os.path.join(self.home, "file")
        open(blocker, "w").close()
        self.run_hook("{}", "--agent", "codex", "--event", "Stop", env={"CADRE_HOME": os.path.join(blocker, "x")})


if __name__ == "__main__":
    unittest.main()
