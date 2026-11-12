"""The event log: cleaning, the dashboard's incremental reader, and the explorer's history paging."""
import json
import os
import shutil
import tempfile
import unittest

from cadre import events


class LogCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cadre-events-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "events.jsonl")

    def ev(self, ts, **kw):
        e = {"ts": ts, "agent": "codex", "event": "PostToolUse", "session_id": "s", "tool": "Bash",
             "detail": "cmd %d" % ts, "cwd": "/w/proj"}
        e.update(kw)
        return e

    def write_raw(self, text, path=None):
        with open(path or self.log, "ab") as f:
            f.write(text.encode("utf-8"))


class Clean(unittest.TestCase):
    def test_keeps_known_fields_and_caps(self):
        ev = events.clean({"ts": 1, "event": "Stop", "session_id": "s", "detail": "x" * 1000, "evil": 1})
        self.assertNotIn("evil", ev)
        self.assertEqual(len(ev["detail"]), 300)

    def test_rejects_unusable(self):
        self.assertIsNone(events.clean([1]))
        self.assertIsNone(events.clean({"event": "Stop"}))
        self.assertIsNone(events.clean({"session_id": "s"}))
        self.assertIsInstance(events.clean({"event": "Stop", "session_id": "s", "ts": "bad"})["ts"], float)


class Store(LogCase):
    def test_sync_reads_only_complete_new_lines(self):
        s = events.EventStore(self.log)
        s.sync()                                   # no file yet
        self.assertEqual(s.total, 0)
        events.append(self.ev(1), self.log)
        self.write_raw('not json\n{"partial": ')
        s.sync()
        self.assertEqual(s.total, 1)
        self.write_raw('1}\n')                     # completes a line that isn't an event
        events.append(self.ev(2), self.log)
        s.sync()
        self.assertEqual([e["ts"] for e in s.tail()], [2, 1])
        # a hook caught mid-write: the half line must be read once it is complete, not lost
        line = json.dumps(self.ev(3)) + "\n"
        self.write_raw(line[:20])
        s.sync()
        self.assertEqual(s.total, 2)
        self.write_raw(line[20:])
        s.sync()
        self.assertEqual(s.tail(1)[0]["ts"], 3)

    def test_rotation_is_followed(self):
        s = events.EventStore(self.log)
        for i in range(5):
            events.append(self.ev(i), self.log)
        s.sync()
        os.replace(self.log, self.log + ".1")
        events.append(self.ev(10), self.log)
        s.sync()
        self.assertEqual(s.tail(1)[0]["ts"], 10)
        self.assertEqual(s.total, 6)

    def test_add_logs_and_ingests(self):
        s = events.EventStore(self.log)
        self.assertTrue(s.add(self.ev(5)))
        self.assertFalse(s.add({"nope": 1}))
        self.assertEqual(s.latest("s")["ts"], 5)
        with open(self.log, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_fresh_session_before_first_prompt(self):
        have = os.path.join(self.dir, "old.jsonl")
        open(have, "w").close()
        s = events.EventStore(self.log)
        events.append(self.ev(50, agent="claude-code", event="SessionStart", session_id="old",
                              transcript_path=have, cwd="/w/new"), self.log)
        events.append(self.ev(90, agent="claude-code", event="SessionStart", session_id="early", cwd="/w/new"), self.log)
        events.append(self.ev(100, agent="claude-code", event="SessionStart", session_id="new",
                              transcript_path=os.path.join(self.dir, "not-yet.jsonl"), cwd="/w/new"), self.log)
        events.append(self.ev(101, agent="codex", event="SessionStart", session_id="cx", cwd="/w/new"), self.log)
        s.sync()
        here = lambda d: d == "/w/new"
        self.assertEqual(s.fresh_session("claude-code", here, since=95), "new")
        self.assertIsNone(s.fresh_session("claude-code", here, since=95, claimed={"new"}))
        self.assertIsNone(s.fresh_session("claude-code", lambda d: d == "/w/other", since=95))
        self.assertEqual(s.fresh_session("claude-code", here, since=0, claimed={"new"}), "early")
        self.assertIsNone(s.fresh_session("claude-code", here, since=0, claimed={"new", "early"}))  # has a transcript

    def test_first_load_skips_a_cut_line(self):
        old = events.LOAD_BYTES
        events.LOAD_BYTES = 200
        self.addCleanup(setattr, events, "LOAD_BYTES", old)
        for i in range(20):
            events.append(self.ev(i), self.log)
        s = events.EventStore(self.log)
        s.sync()
        self.assertGreater(s.total, 0)
        self.assertLess(s.total, 20)
        self.assertEqual(s.tail(1)[0]["ts"], 19)


class History(LogCase):
    def test_pages_backwards_through_rotated_log(self):
        for i in range(0, 5):
            events.append(self.ev(i), self.log + ".1")
        for i in range(5, 10):
            events.append(self.ev(i), self.log)
        page, more = events.history(limit=4, path=self.log)
        self.assertEqual([e["ts"] for e in page], [9, 8, 7, 6])
        self.assertTrue(more)
        page, more = events.history(before=6, limit=4, path=self.log)
        self.assertEqual([e["ts"] for e in page], [5, 4, 3, 2])
        page, more = events.history(before=2, limit=4, path=self.log)
        self.assertEqual(([e["ts"] for e in page], more), ([1, 0], False))

    def test_matcher(self):
        evs = [self.ev(1), self.ev(2, event="UserPromptSubmit", tool=None, detail="Fix the TRAY icon"),
               self.ev(3, agent="claude-code", failed=True, cwd="C:\\src\\Other\\"),
               self.ev(4, event="Notification", tool=None)]
        for e in evs:
            events.append({k: v for k, v in e.items() if v is not None}, self.log)

        def ts(**kw):
            return [e["ts"] for e in events.history(match=events.matcher(**kw), path=self.log)[0]]
        self.assertIsNone(events.matcher())
        self.assertEqual(ts(q="tray"), [2])
        self.assertEqual(ts(agent="claude-code"), [3])
        self.assertEqual(ts(kind="prompt"), [2])
        self.assertEqual(ts(kind="failed"), [3])
        self.assertEqual(ts(kind="waiting"), [4])
        self.assertEqual(ts(kind="tool"), [3, 1])
        self.assertEqual(ts(project="other"), [3])
        self.assertEqual(ts(project="proj", kind="tool"), [1])


class Append(LogCase):
    def test_utf8_one_line_each(self):
        events.append(self.ev(1, detail="ünï ✓\nsecond line"), self.log)
        with open(self.log, "rb") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0].decode("utf-8"))["detail"], "ünï ✓\nsecond line")


if __name__ == "__main__":
    unittest.main()
