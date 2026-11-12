"""Detection rules (cadre/detect). Every rule's own `hits` and `misses` are checked here, so a
new rule is tested by adding examples to it; the rest covers the engine and the wiring."""
import importlib.util
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from cadre import detect, events
from cadre.detect import engine, helpers
from cadre.detect.rules import secrets

EMIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cadre", "hooks", "emit.py")
_spec = importlib.util.spec_from_file_location("cadre_emit_for_detect", EMIT)
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

RUN = {"event": engine.check_event, "session": engine.check_session, "record": engine.check_record}


class EveryRule(unittest.TestCase):
    def test_registry_is_well_formed(self):
        self.assertGreaterEqual(len(detect.RULES), 20)
        for rid, r in detect.RULES.items():
            self.assertRegex(rid, r"^[a-z0-9]+(-[a-z0-9]+)*$")
            self.assertIn(r.severity, detect.SEVERITIES, rid)
            self.assertIn(r.kind, engine.KINDS, rid)
            self.assertTrue(r.hits, "%s has no hit examples" % rid)
            self.assertTrue(r.misses, "%s has no miss examples" % rid)
            # the category is the file it lives in, so a rule is easy to find
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(detect.__file__), "rules", r.category + ".py")),
                            rid)

    def test_hits_and_misses(self):
        for rid, r in detect.RULES.items():
            for x in r.hits:
                self.assertIn(rid, [f["rule"] for f in RUN[r.kind](x)], "%s should flag %r" % (rid, x))
            for x in r.misses:
                self.assertNotIn(rid, [f["rule"] for f in RUN[r.kind](x)], "%s should not flag %r" % (rid, x))

    def test_patterns_compile_on_every_python(self):
        # a flag like (?i) in the middle of a pattern is an error from Python 3.11 on
        for row in __import__("cadre.detect.rules.commands", fromlist=["TABLE"]).TABLE:
            self.assertNotRegex(row[3][1:], r"(?<!\()\(\?[aiLmsux]+\)", row[0])


class Engine(unittest.TestCase):
    def test_a_broken_rule_is_skipped(self):
        @engine.event_rule("test-broken", "commands", "high", "broken", hits=[{}], misses=[{}])
        def broken(ev):
            raise RuntimeError("boom")
        try:
            self.assertEqual([f for f in engine.check_event({"tool": "Bash"}) if f["rule"] == "test-broken"], [])
        finally:
            del detect.RULES["test-broken"]

    def test_duplicate_ids_and_bad_severity_are_refused(self):
        with self.assertRaises(ValueError):
            engine.event_rule("rm-rf", "commands", "high", "x")(lambda ev: None)
        with self.assertRaises(ValueError):
            engine.event_rule("test-x", "commands", "critical", "x")

    def test_finding_shape(self):
        f = engine.check_event({"tool": "Bash", "command": "rm -rf build"})
        self.assertEqual(f[0]["rule"], "rm-rf")
        self.assertEqual(set(f[0]), {"rule", "severity", "category", "title", "detail"})
        self.assertIn("rm -rf build", f[0]["detail"])
        self.assertEqual(engine.check_event("not a dict"), [])

    def test_heredoc_bodies_are_not_commands(self):
        cmd = "cat > notes.md <<'EOF'\nrm -rf / is bad\nsudo x\nEOF\necho ok"
        self.assertEqual(helpers.strip_heredocs(cmd), "cat > notes.md <<EOF …\necho ok")
        self.assertEqual(engine.check_event({"tool": "Bash", "command": cmd}), [])
        self.assertTrue(engine.check_event({"tool": "Bash", "command": cmd + " && rm -rf build"}))


class Secrets(unittest.TestCase):
    def test_emit_and_rule_share_one_table(self):
        self.assertEqual(emit.SECRETS, secrets.SECRETS)

    def test_redaction(self):
        s = emit.redact("AWS=AKIAABCDEFGHIJKLMNOP curl -H 'Bearer sk-ant-api03-abcdefghijklmnopqrstuv' "
                        "--password=hunter2hunter2 x TOKEN=$GITHUB_TOKEN")
        self.assertNotIn("AKIAABCDEF", s)
        self.assertNotIn("sk-ant", s)
        self.assertNotIn("hunter2", s)
        self.assertIn("[redacted:aws-key]", s)
        self.assertIn("--password=[redacted:password]", s)
        self.assertIn("TOKEN=$GITHUB_TOKEN", s)                  # a variable reference stays
        self.assertEqual(emit.redact("x-api-key: sk-abcdefghijklmnopqrstuvwx"), "x-api-key: [redacted:api-key]")
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----"
        self.assertEqual(emit.redact("echo '%s' > k" % key), "echo '[redacted:private-key]' > k")

    def test_hook_events_never_hold_the_secret(self):
        ev = emit.build({"session_id": "s", "tool_name": "Bash", "tool_input":
                         {"command": "curl -u x:y https://h --token=ghp_" + "a" * 36, "description": "call api"}},
                        "claude-code", "PostToolUse")
        self.assertNotIn("ghp_aaaa", repr(ev))
        self.assertEqual([f["rule"] for f in detect.check_event(ev)], ["secret"])
        ev = emit.build({"session_id": "s", "prompt": "my key is AKIAABCDEFGHIJKLMNOP"}, "claude-code", "UserPromptSubmit")
        self.assertNotIn("AKIA", repr(ev))
        self.assertEqual(detect.check_event(ev)[0]["detail"], "aws-key")

    def test_command_and_path_are_recorded(self):
        ev = emit.build({"session_id": "s", "tool_name": "Bash",
                         "tool_input": {"command": "cat > a <<EOF\nx\nEOF", "description": "write a"}},
                        "claude-code", "PostToolUse")
        self.assertEqual((ev["detail"], ev["command"]), ("write a", "cat > a <<EOF\nx\nEOF"))
        ev = emit.build({"session_id": "s", "toolName": "edit", "toolArgs": '{"filePath": "/w/a.py"}'},
                        "copilot", "postToolUse")
        self.assertEqual(ev["path"], "/w/a.py")
        self.assertNotIn("command", ev)


class Review(unittest.TestCase):
    def test_record_gets_process_event_and_session_findings(self):
        now = 10000.0
        evs = [{"ts": now - 100 + i, "session_id": "s", "tool": "Bash", "command": "npm test", "failed": True}
               for i in range(3)]
        evs += [{"ts": now - 50, "session_id": "s", "tool": "Bash", "command": "rm -rf ~"},
                {"ts": now - 40, "session_id": "s", "tool": "Bash", "command": "rm -rf ~"},
                {"ts": now - 7200, "session_id": "s", "tool": "Bash", "command": "git push --force"}]   # hours ago
        rec = {"pid": 1, "kind": "cli", "cwd": helpers.HOME, "args": "claude --dangerously-skip-permissions"}
        detect.review_record(rec, evs, now)
        rules = [f["rule"] for f in rec["findings"]]
        self.assertEqual(rules[0], "rm-rf-wide")                          # high first
        old = {f["rule"] for f in detect.check_event(evs[-1])}
        self.assertTrue(old)
        self.assertEqual(set(rules), {"rm-rf-wide", "rm-rf", "permissions-bypassed", "failing-loop", "home-folder"} | old)
        rules = [f["rule"] for f in rec["findings"] if f["rule"] not in old]
        self.assertIn("recursive delete of a whole tree (rm -rf ~) ×2", rec["flags"])
        self.assertIn("permissions bypassed", rec["flags"])
        self.assertTrue(any(f.startswith("running in your home folder (") for f in rec["flags"]))
        self.assertEqual({f["rule"] for f in detect.alerting(rec["findings"])},
                         {"rm-rf-wide", "rm-rf", "permissions-bypassed", "failing-loop"} | old)

    def test_a_running_agent_keeps_a_flag_until_it_is_decided(self):
        # the user's report (2026-11-11): Codex flagged at 12:39, still running at 15:03, and
        # no longer on the banner, while the Alerts tab still listed it
        now = 50000.0
        evs = [{"ts": now - 3 * 3600, "session_id": "s", "tool": "Bash", "command": "rm -rf build"}]
        live = detect.review_record({"pid": 7, "kind": "cli", "cwd": "/tmp/x"}, evs, now)
        self.assertEqual([f["rule"] for f in live["findings"]], ["rm-rf"])
        asleep = detect.review_record({"pid": None, "kind": "cli", "cwd": "/tmp/x"}, evs, now)
        self.assertEqual(asleep["findings"], [], "a session that isn't running: the last hour only")

    def test_session_rules_still_look_at_the_last_hour(self):
        now = 50000.0
        evs = [{"ts": now - 2 * 3600 + i, "session_id": "s", "tool": "Bash", "command": "npm test", "failed": True}
               for i in range(5)]
        rec = detect.review_record({"pid": 7, "kind": "cli", "cwd": "/tmp/x"}, evs, now)
        self.assertNotIn("failing-loop", [f["rule"] for f in rec["findings"]], "a loop that ended hours ago")

    def test_sleeping_records_skip_process_rules(self):
        rec = detect.review_record({"pid": None, "cpu": 99, "kind": "cli", "cwd": "/"}, [], 1.0)
        self.assertEqual(rec["flags"], [])


class Wiring(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cadre-detect-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "events.jsonl")
        for i, cmd in enumerate(["ls", "rm -rf build", "git status"]):
            events.append({"ts": 100 + i, "agent": "codex", "event": "PostToolUse", "session_id": "s",
                           "tool": "Bash", "command": cmd, "detail": cmd}, self.log)

    def test_store_and_history_mark_flagged_events(self):
        s = events.EventStore(self.log)
        s.sync()
        flagged = [e["command"] for e in s.tail() if e.get("findings")]
        self.assertEqual(flagged, ["rm -rf build"])
        self.assertEqual(len(s.session_events("s")), 3)
        page, _ = events.history(match=events.matcher(kind="flagged"), path=self.log)
        self.assertEqual([e["command"] for e in page], ["rm -rf build"])
        self.assertEqual(page[0]["findings"][0]["rule"], "rm-rf")

    def test_a_flagged_event_outlives_the_recent_list(self):
        s = events.EventStore(self.log, maxlen=5)
        for i in range(40):                        # a busy machine: other sessions push it out
            events.append({"ts": 200 + i, "agent": "claude-code", "event": "PostToolUse", "session_id": "other",
                           "tool": "Bash", "command": "ls"}, self.log)
        events.append({"ts": 300, "agent": "codex", "event": "PostToolUse", "session_id": "s",
                       "tool": "Bash", "command": "git status"}, self.log)
        s.sync()
        self.assertNotIn("rm -rf build", [e.get("command") for e in s.tail()])
        self.assertEqual([e["command"] for e in s.session_events("s")], ["rm -rf build", "git status"])
        self.assertEqual(s.session_events("other", since=238)[0]["ts"], 238)
        with mock.patch.object(events, "FLAGGED_SESSIONS", 1):
            events.append({"ts": 400, "agent": "codex", "event": "PostToolUse", "session_id": "t",
                           "tool": "Bash", "command": "rm -rf dist"}, self.log)
            s.sync()
        self.assertEqual(list(s.flagged), ["t"], "bounded: the sessions flagged last")


class NoCrossImports(unittest.TestCase):
    def test_engine_and_rules_stay_pure(self):
        # detect/ must not import the rest of cADRe (so rules can be tested and reused alone)
        root = os.path.dirname(detect.__file__)
        for dirpath, _, files in os.walk(root):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        src = f.read()
                    self.assertNotRegex(src, r"(?m)^\s*(?:import cadre|from cadre)\b", name)
                    self.assertNotRegex(src, r"(?m)^\s*from \.{2,3}(?:collectors|events|installer|procs|zcode|codex|"
                                             r"service|paths|registry|menubar|tray|__main__)\b", name)


if __name__ == "__main__":
    unittest.main()
