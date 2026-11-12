"""Claude Code transcript reading (collectors.read_claude_session): which folder a session
belongs to. Runs each read in a thread with a time limit: this code once looped forever."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from cadre import collectors


def read_with_limit(path, seconds=5):
    """read_claude_session(path), or the string "hung" if it doesn't return in time."""
    box = {}
    t = threading.Thread(target=lambda: box.update(info=collectors.read_claude_session(path)), daemon=True)
    t.start()
    t.join(seconds)
    return "hung" if t.is_alive() else box.get("info")


class ProjectOfASession(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cadre-col-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def transcript(self, project_cwd, cwds):
        """A transcript in the project folder Claude Code would use for project_cwd."""
        folder = collectors.claude_project_dir(project_cwd, home=os.path.join(self.dir, "claude"))
        os.makedirs(folder)
        path = os.path.join(folder, "0000-session.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for c in cwds:
                f.write(json.dumps({"type": "user", "cwd": c, "message": {"content": "hi"}}) + "\n")
        return path

    def test_no_recorded_cwd_matches_the_project_folder(self):
        # Seen 2026-11-10 on Windows: the climb to the parent stopped only at "/" or "", but
        # dirname("C:\\") is "C:\\" again, so this spun forever inside snapshot() and the
        # dashboard never answered.
        project = os.path.join(self.dir, "work", "app")
        elsewhere = os.path.join(self.dir, "other", "place")
        info = read_with_limit(self.transcript(project, [elsewhere]))
        self.assertNotEqual(info, "hung")
        self.assertEqual(info["cwd"], elsewhere)            # falls back to the first one recorded
        self.assertEqual(info["current_dir"], elsewhere)

    def test_roots_end_the_climb(self):
        for root in ("C:\\", "D:\\", "\\\\server\\share", "\\\\server\\share\\", "/", "C:", ""):
            with self.subTest(root=root):
                done = threading.Event()
                t = threading.Thread(target=lambda: (collectors._project_root(root, "no-such-name"), done.set()),
                                     daemon=True)
                t.start()
                self.assertTrue(done.wait(5), "climb from %r never ended" % root)

    def test_a_subfolder_belongs_to_the_project_above_it(self):
        project = os.path.join(self.dir, "work", "app")
        sub = os.path.join(project, "src", "deep")
        info = read_with_limit(self.transcript(project, [project, sub]))
        self.assertEqual(info["cwd"], project)
        self.assertEqual(info["current_dir"], sub)

    def test_the_project_is_found_from_a_subfolder_alone(self):
        project = os.path.join(self.dir, "work", "app")
        sub = os.path.join(project, "src")
        self.assertEqual(read_with_limit(self.transcript(project, [sub]))["cwd"], project)


class WhichSessionAProcessRuns(unittest.TestCase):
    """A session's flags and the Kill button land on the process the snapshot ties it to.
    Seen 2026-11-11 on Windows without psutil (no process folders): every claude.exe took the
    newest transcript nobody had, so the flagged session sat on the wrong process and Kill
    ended that one. Afterwards a Claude Code started in another folder inherited it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cadre-col-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, ".claude")
        os.makedirs(os.path.join(self.home, "projects"))
        os.makedirs(os.path.join(self.home, "sessions"))
        self.now = time.time()
        self.procs, self.cwds = {}, {}
        for name, value in (
                ("cadre.collectors.claude_homes", lambda: [self.home]),
                ("cadre.collectors.DEFAULT_CLAUDE_HOME", self.home),
                ("cadre.collectors.list_processes", lambda: {k: dict(v) for k, v in self.procs.items()}),
                ("cadre.collectors.fill_details", lambda procs_, pids: None),
                ("cadre.collectors.proc_cwds", lambda pids: dict(self.cwds)),
                ("cadre.collectors.proc_env", lambda pid, names: {}),
                ("cadre.collectors.codex.session_files", lambda homes=(): []),
                ("cadre.collectors.zcode.read_sessions", lambda: []),
                ("cadre.collectors.zcode.read_events", lambda n: []),
                ("cadre.collectors.hook_coverage", lambda *a: {"targets": [], "pending": 0}),
                ("cadre.collectors.installed_evidence", lambda spec: None)):
            p = mock.patch(name, value)
            p.start()
            self.addCleanup(p.stop)

    def claude(self, pid, uptime=600, args="claude"):
        self.procs[pid] = {"pid": pid, "ppid": 1, "stat": "S", "cpu": 0.0, "rss_mb": 1.0,
                           "uptime_s": uptime, "exe": r"C:\Users\x\.local\bin\claude.exe", "args": args}

    def transcript(self, sid, cwd, age):
        folder = collectors.claude_project_dir(cwd, home=self.home)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "cwd": cwd, "message": {"content": "hi"}}) + "\n")
        os.utime(path, (self.now - age, self.now - age))
        return path

    def session_file(self, pid, sid, cwd, age):
        with open(os.path.join(self.home, "sessions", "%d.json" % pid), "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "sessionId": sid, "cwd": cwd, "startedAt": int((self.now - age) * 1000)}, f)

    def agents(self):
        return {a["id"]: a for a in collectors.snapshot()["agents"]}

    def test_each_process_gets_the_session_it_says_it_runs(self):
        rogue, other = os.path.join(self.dir, "TestUserFolder"), os.path.join(self.dir, "work")
        self.claude(100), self.claude(200)                 # no folders: Windows without psutil
        self.transcript("aaaaaaaa-rogue", rogue, age=5)    # the newest transcript on the machine
        self.transcript("bbbbbbbb-other", other, age=60)
        self.session_file(100, "bbbbbbbb-other", other, age=590)
        self.session_file(200, "aaaaaaaa-rogue", rogue, age=590)
        a = self.agents()
        self.assertEqual((a["claude-code:100"]["session_id"], a["claude-code:100"]["project"]),
                         ("bbbbbbbb-other", "work"))
        self.assertEqual((a["claude-code:200"]["session_id"], a["claude-code:200"]["project"]),
                         ("aaaaaaaa-rogue", "TestUserFolder"))
        self.assertEqual(a["claude-code:200"]["cwd"], rogue)
        self.assertEqual(len(a), 2)

    def test_a_new_agent_does_not_inherit_an_ended_session(self):
        rogue, fresh = os.path.join(self.dir, "TestUserFolder"), os.path.join(self.dir, "fresh")
        self.transcript("aaaaaaaa-rogue", rogue, age=120)  # its process was killed
        self.claude(300, uptime=30)                        # started since, nothing typed yet
        self.session_file(300, "cccccccc-fresh", fresh, age=29)
        a = self.agents()
        live = a["claude-code:300"]
        self.assertEqual((live["session_id"], live["project"], live["cwd"]), ("cccccccc-fresh", "fresh", fresh))
        self.assertIsNone(a["claude-code:aaaaaaaa-rogue"]["pid"])
        self.assertEqual(a["claude-code:aaaaaaaa-rogue"]["status"], "sleeping")

    def test_a_session_file_left_by_an_earlier_process_with_that_pid_is_ignored(self):
        self.claude(100, uptime=60)
        self.session_file(100, "dddddddd-old", os.path.join(self.dir, "old"), age=9000)
        self.assertIsNone(self.agents()["claude-code:100"].get("session_id"))

    def test_without_session_files_or_folders_two_processes_get_no_guess(self):
        self.claude(100), self.claude(200)
        self.transcript("aaaaaaaa-rogue", os.path.join(self.dir, "TestUserFolder"), age=5)
        a = self.agents()
        self.assertIsNone(a["claude-code:100"].get("session_id"))
        self.assertIsNone(a["claude-code:200"].get("session_id"))
        self.assertIn("claude-code:aaaaaaaa-rogue", a)     # still listed, as a session without a process

    def test_one_process_without_a_folder_takes_only_what_it_wrote(self):
        self.claude(100, uptime=60)
        self.transcript("aaaaaaaa-rogue", os.path.join(self.dir, "TestUserFolder"), age=300)
        self.assertIsNone(self.agents()["claude-code:100"].get("session_id"))
        self.transcript("eeeeeeee-mine", os.path.join(self.dir, "mine"), age=10)
        self.assertEqual(self.agents()["claude-code:100"]["session_id"], "eeeeeeee-mine")

    def test_with_a_folder_an_older_transcript_there_is_not_this_process(self):
        folder = os.path.join(self.dir, "TestUserFolder")
        self.claude(100, uptime=60)
        self.cwds[100] = folder
        self.transcript("aaaaaaaa-rogue", folder, age=300)             # an earlier session, same folder
        self.assertIsNone(self.agents()["claude-code:100"].get("session_id"))
        self.claude(100, uptime=60, args="claude --continue")          # unless it was asked to carry on
        self.assertEqual(self.agents()["claude-code:100"]["session_id"], "aaaaaaaa-rogue")
        self.claude(100, uptime=60)
        self.transcript("ffffffff-new", folder, age=10)
        self.assertEqual(self.agents()["claude-code:100"]["session_id"], "ffffffff-new")

    def test_two_processes_in_one_folder_do_not_share_a_transcript(self):
        folder = os.path.join(self.dir, "work")
        self.claude(100), self.claude(200)
        self.cwds.update({100: folder, 200: folder})
        self.transcript("aaaaaaaa-one", folder, age=20)
        self.transcript("bbbbbbbb-two", folder, age=10)
        self.session_file(100, "aaaaaaaa-one", folder, age=590)        # 200 is an older Claude Code
        a = self.agents()
        self.assertEqual(a["claude-code:100"]["session_id"], "aaaaaaaa-one")
        self.assertEqual(a["claude-code:200"]["session_id"], "bbbbbbbb-two")


if __name__ == "__main__":
    unittest.main()
