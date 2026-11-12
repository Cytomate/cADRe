"""Response (cadre/respond): alerts, Pause / Resume / Kill, false positives, the response log.

Mock tests: the process list and the calls that would suspend or end a process are replaced,
so no agent (and no other process) is touched. The one exception is RealProcess, which
suspends, resumes and ends a child process this test starts itself.
"""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from cadre import __main__ as server
from cadre import procs, respond
from cadre.respond import actions, audit, dismiss
from cadre.respond.alerts import AlertLog

ME = os.getpid()
NOW = 1_800_000_000.0


def finding(rule="rm-rf", severity="warn", detail="rm -rf build", title="recursive force delete"):
    return {"rule": rule, "severity": severity, "category": "commands", "title": title, "detail": detail}


def agent(id="claude-code:500", pid=500, findings=(), **kw):
    rec = {"id": id, "agent": "Claude Code", "agent_key": "claude-code", "pid": pid, "project": "shop",
           "session_id": "s-1", "status": "working", "uptime_s": 600, "findings": list(findings),
           "flags": [f["title"] for f in findings]}
    rec.update(kw)
    return rec


# ---------------------------------------------------------------- alerts
class Alerts(unittest.TestCase):
    def test_first_snapshot_only_takes_note(self):
        log = AlertLog()
        self.assertEqual(log.update([agent(findings=[finding()])], NOW), [])
        self.assertEqual(log.update([agent(findings=[finding()])], NOW + 3), [])

    def test_new_finding_alerts_once(self):
        log = AlertLog()
        log.update([agent()], NOW)
        out = log.update([agent(findings=[finding()])], NOW + 3)
        self.assertEqual(len(out), 1)
        a = out[0]
        self.assertEqual((a["agent_id"], a["agent"], a["project"], a["rule"], a["severity"], a["ts"]),
                         ("claude-code:500", "Claude Code", "shop", "rm-rf", "warn", NOW + 3))
        self.assertEqual(a["text"], "recursive force delete (rm -rf build)")
        # still flagged on the next snapshots: the same alert, not a new one
        again = log.update([agent(findings=[finding()])], NOW + 6)
        self.assertEqual([x["id"] for x in again], [a["id"]])

    def test_info_never_alerts(self):
        log = AlertLog()
        log.update([agent()], NOW)
        self.assertEqual(log.update([agent(findings=[finding("home-folder", "info")])], NOW + 3), [])

    def test_same_rule_is_quiet_for_a_while(self):
        log = AlertLog(quiet=300)
        log.update([agent()], NOW)
        log.update([agent(findings=[finding(detail="rm -rf a")])], NOW + 3)
        out = log.update([agent(findings=[finding(detail="rm -rf a"), finding(detail="rm -rf b")])], NOW + 6)
        self.assertEqual(len(out), 1, "a second rm -rf seconds later is the same news")
        out = log.update([agent(findings=[finding(detail="rm -rf c")])], NOW + 400)
        self.assertEqual([a["detail"] for a in out], ["rm -rf c", "rm -rf a"])

    def test_a_finding_that_flickers_alerts_once(self):
        log = AlertLog(quiet=300)
        cpu = finding("high-cpu", "warn", "")
        log.update([agent()], NOW)
        for i, fs in enumerate(([cpu], [], [cpu], [], [cpu])):
            out = log.update([agent(findings=fs)], NOW + 3 * (i + 1))
        self.assertEqual(len(out), 1)

    def test_other_rule_and_other_agent_alert(self):
        log = AlertLog()
        log.update([agent()], NOW)
        out = log.update([agent(findings=[finding(), finding("curl-pipe-sh", "high", "curl x | sh")]),
                          agent("codex:7", 7, [finding()], session_id="s-2", agent="Codex")], NOW + 3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["severity"], "high", "the worst one first")

    def test_exited_agent_does_not_alert_again_as_a_sleeping_session(self):
        log = AlertLog(quiet=0)
        log.update([agent()], NOW)
        log.update([agent(findings=[finding()])], NOW + 3)
        # the process is gone: the same session is now listed under another id, without a pid
        out = log.update([agent("claude-code:s-1", None, [finding()], status="sleeping")], NOW + 6)
        self.assertEqual(len(out), 1)

    def test_list_is_bounded_and_newest_first(self):
        log = AlertLog(quiet=0, keep=5)
        log.update([], NOW)
        for i in range(12):
            out = log.update([agent(findings=[finding(detail="n%d" % i)])], NOW + i + 1)
        self.assertEqual([a["detail"] for a in out], ["n11", "n10", "n9", "n8", "n7"])

    def test_junk_records_are_survived(self):
        log = AlertLog()
        log.update([{}], NOW)
        self.assertEqual(len(log.update([{"id": "x", "findings": [{"severity": "high"}]}, {}], NOW + 1)), 1)


# ---------------------------------------------------------------- the response log
class Audit(unittest.TestCase):
    def setUp(self):
        self.file = os.path.join(os.environ["CADRE_HOME"], "audit-test-%d.jsonl" % id(self))
        self.addCleanup(lambda: os.path.exists(self.file) and os.remove(self.file))

    def test_append_and_read(self):
        self.assertEqual(audit.entries(file=self.file), [])
        for i in range(5):
            audit.append({"ts": NOW + i, "action": "pause", "ok": True, "agent": "Clau\u00e9"}, self.file)
        got = audit.entries(3, self.file)
        self.assertEqual([e["ts"] for e in got], [NOW + 2, NOW + 3, NOW + 4])
        self.assertEqual(got[0]["agent"], "Clau\u00e9")

    def test_junk_lines_are_skipped(self):
        audit.append({"ts": NOW, "action": "kill", "ok": False}, self.file)
        with open(self.file, "a", encoding="utf-8") as f:
            f.write('half a li\n[1, 2]\n{"no": "action"}\n')
        audit.append({"ts": NOW + 1, "action": "resume", "ok": True}, self.file)
        self.assertEqual([e["action"] for e in audit.entries(file=self.file)], ["kill", "resume"])


# ---------------------------------------------------------------- false positives
class Dismiss(unittest.TestCase):
    def setUp(self):
        base = os.path.join(os.environ["CADRE_HOME"], "fp-test-%d" % id(self))
        self.file, self.log = base + ".json", base + ".jsonl"
        for name, value in (("cadre.respond.dismiss.path", lambda: self.file),
                            ("cadre.respond.audit.path", lambda: self.log)):
            p = mock.patch(name, value)
            p.start()
            self.addCleanup(p.stop)
        dismiss._load(force=True)
        self.addCleanup(lambda: [os.path.exists(f) and os.remove(f) for f in (self.file, self.log)])
        self.addCleanup(lambda: dismiss._state.update(at=0.0, stamp=None, items=[], keys=set()))
        self.rm = finding(detail="rm -rf build")
        self.cpu = finding("high-cpu", "warn", "92%", "high CPU")
        self.snap = {"agents": [agent(findings=[self.rm, self.cpu])]}

    def mark(self, who="s-1", rule="rm-rf", detail="rm -rf build", **kw):
        return dismiss.mark(self.snap, who, rule, detail, now=NOW, **kw)

    def logged(self):
        return audit.entries(file=self.log)

    def test_bad_requests(self):
        for who, rule, detail in (("s-1", "no-such-rule", ""), ("s-1", None, ""), ("s-1", ["rm-rf"], ""),
                                  ("", "rm-rf", ""), (None, "rm-rf", ""), (5, "rm-rf", ""),
                                  ("x" * 201, "rm-rf", ""), ("s-1", "rm-rf", 7), ("s-1", "rm-rf", "x" * 601)):
            with self.assertRaises(ValueError, msg=(who, rule, detail)):
                self.mark(who, rule, detail)
        self.assertEqual(dismiss.active(), [])
        self.assertEqual(self.logged(), [])

    def test_mark_and_take_back(self):
        e = self.mark()
        self.assertEqual((e["action"], e["ok"], e["result"]),
                         ("dismiss", True, "marked as a false positive: recursive force delete (rm -rf build)"))
        self.assertEqual((e["agent"], e["project"], e["who"], e["rule"], e["detail"], e["severity"]),
                         ("Claude Code", "shop", "s-1", "rm-rf", "rm -rf build", "warn"))
        self.assertTrue(dismiss.is_dismissed(("s-1",), self.rm))
        self.assertEqual([(d["who"], d["rule"], d["detail"]) for d in dismiss.active()], [("s-1", "rm-rf", "rm -rf build")])
        again = self.mark()
        self.assertEqual((again["ok"], again["result"]), (False, "it is already marked as a false positive"))
        self.assertEqual(len(dismiss.active()), 1)
        back = self.mark(restore=True)
        self.assertEqual((back["action"], back["ok"], back["result"]),
                         ("restore", True, "flag restored: recursive force delete (rm -rf build)"))
        self.assertFalse(dismiss.is_dismissed(("s-1",), self.rm))
        self.assertFalse(self.mark(restore=True)["ok"])
        self.assertEqual([(x["action"], x["ok"]) for x in self.logged()],
                         [("dismiss", True), ("dismiss", False), ("restore", True), ("restore", False)])

    def test_an_event_rule_is_marked_for_that_exact_finding_only(self):
        self.mark()
        self.assertFalse(dismiss.is_dismissed(("s-1",), finding(detail="rm -rf ~")), "another command")
        self.assertFalse(dismiss.is_dismissed(("s-2",), self.rm), "another session")
        self.assertFalse(dismiss.is_dismissed(("s-1",), finding("rm-rf-wide", "high", "rm -rf build")), "another rule")

    def test_a_record_rule_is_marked_whatever_its_detail(self):
        e = self.mark(rule="high-cpu", detail="92%")
        self.assertEqual((e["detail"], e["result"]), ("", "marked as a false positive: high CPU"))
        self.assertTrue(dismiss.is_dismissed(("s-1",), finding("high-cpu", "warn", "97%")))
        self.assertFalse(dismiss.is_dismissed(("s-2",), self.cpu))
        self.assertTrue(self.mark(rule="high-cpu", detail="40%", restore=True)["ok"])

    def test_record_loses_the_flag_and_keeps_the_rest(self):
        rec = agent(findings=[dict(self.rm, count=3, ts=NOW), self.cpu])
        rec["flags"] = ["stale"]
        self.assertIs(dismiss.apply_record(rec), rec)
        self.assertEqual(rec["flags"], ["stale"], "nothing marked: untouched")
        self.mark()
        dismiss.apply_record(rec)
        self.assertEqual([f["rule"] for f in rec["findings"]], ["high-cpu"])
        self.assertEqual([f["rule"] for f in rec["dismissed"]], ["rm-rf"])
        self.assertEqual(rec["flags"], ["high CPU (92%)"])

    def test_record_without_a_session_is_matched_by_its_id(self):
        rec = agent("ollama:77", 77, [self.cpu], session_id=None)
        dismiss.mark({"agents": [rec]}, "ollama:77", "high-cpu", now=NOW)
        dismiss.apply_record(rec)
        self.assertEqual((rec["findings"], rec["flags"]), ([], []))

    def test_event_is_copied_not_changed(self):
        wide = finding("rm-rf-wide", "high", "rm -rf build")
        ev = {"ts": NOW, "session_id": "s-1", "tool": "Bash", "findings": [wide, self.rm]}
        self.assertIs(dismiss.apply_event(ev), ev)
        self.mark()
        out = dismiss.apply_event(ev)
        self.assertEqual(([f["rule"] for f in out["findings"]], [f["rule"] for f in out["dismissed"]]),
                         (["rm-rf-wide"], ["rm-rf"]))
        self.assertEqual(len(ev["findings"]), 2, "the stored event keeps every finding")
        self.assertNotIn("dismissed", ev)
        self.mark(rule="rm-rf-wide")
        out = dismiss.apply_event(ev)
        self.assertNotIn("findings", out)
        self.assertEqual(len(out["dismissed"]), 2)
        plain = {"ts": NOW, "session_id": "s-1"}
        self.assertIs(dismiss.apply_event(plain), plain)

    def test_marks_survive_a_restart_and_a_broken_file(self):
        self.mark()
        dismiss._state.update(at=0.0, stamp=None, items=[], keys=set())      # a freshly started dashboard
        self.assertTrue(dismiss.is_dismissed(("s-1",), self.rm))
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("{not json")
        dismiss._load(force=True)
        self.assertEqual(dismiss.active(), [])
        self.assertTrue(self.mark()["ok"], "a broken file is replaced, not fatal")

    def test_label_from_the_request_when_the_session_is_gone(self):
        e = dismiss.mark({"agents": []}, "old-session", "rm-rf", "rm -rf x", agent="Codex", project="shop" * 50, now=NOW)
        self.assertEqual((e["agent"], len(e["project"]), e["agent_id"]), ("Codex", 80, None))

    def test_oldest_marks_go_first(self):
        with mock.patch.object(dismiss, "MAX", 3):
            for i in range(5):
                self.mark(detail="rm -rf d%d" % i)
        self.assertEqual([d["detail"] for d in dismiss.active()], ["rm -rf d4", "rm -rf d3", "rm -rf d2"])

    def test_dismissed_findings_do_not_alert_or_flag_in_the_snapshot_glue(self):
        from cadre import collectors
        self.mark(rule="many-children", detail="")
        rec = agent(findings=[], child_count=500, args="claude", cwd="/work/shop", kind="cli")
        with mock.patch("cadre.detect.review_record",
                        side_effect=lambda r, ev, now: r.update(findings=[finding("many-children", "warn", "500", "many child processes")],
                                                                flags=["many child processes (500)"])):
            collectors.review(rec, (), NOW)
        self.assertEqual((rec["findings"], rec["flags"]), ([], []))
        log = AlertLog()
        log.update([], NOW)
        self.assertEqual(log.update([rec], NOW + 3), [])

    def test_explorer_drops_them_before_matching(self):
        self.mark()
        evs = [{"ts": NOW - 1, "session_id": "s-1", "agent": "claude-code", "tool": "Bash", "findings": [self.rm]},
               {"ts": NOW - 2, "session_id": "s-1", "agent": "claude-code", "tool": "Bash",
                "findings": [finding(detail="rm -rf ~")]},
               {"ts": NOW - 3, "session_id": "s-2", "agent": "claude-code", "tool": "Bash", "findings": [self.rm]}]

        def history(before, limit, match):
            return [e for e in evs if match is None or match(e)], False
        with mock.patch("cadre.events.history", history), mock.patch("cadre.zcode.read_events", return_value=[]):
            flagged_ = server.event_history("/api/events?kind=flagged")["events"]
            everything = server.event_history("/api/events")["events"]
        self.assertEqual([e["ts"] for e in flagged_], [NOW - 2, NOW - 3])
        self.assertEqual(len(everything), 3)
        self.assertEqual([f["rule"] for f in everything[0]["dismissed"]], ["rm-rf"])
        self.assertNotIn("findings", everything[0])

    def test_endpoint(self):
        out = server.respond_action({"action": "dismiss", "who": "s-1", "rule": "rm-rf", "detail": "rm -rf build"}, self.snap)
        self.assertTrue(out["ok"])
        out = server.respond_action({"action": "restore", "who": "s-1", "rule": "rm-rf", "detail": "rm -rf build"}, self.snap)
        self.assertEqual((out["action"], out["ok"]), ("restore", True))
        with self.assertRaises(ValueError):
            server.respond_action({"action": "dismiss", "who": "s-1"}, self.snap)

    def test_log_entries_do_not_disturb_the_paused_agents(self):
        self.mark()
        with mock.patch("cadre.respond.actions._paused", None):
            self.assertFalse(respond.paused_by_us(500))


# ---------------------------------------------------------------- pause / resume / kill
def proc(pid, ppid, exe="claude", uptime=600, stat="S"):
    return {"pid": pid, "ppid": ppid, "stat": stat, "cpu": 0.0, "rss_mb": 1.0, "uptime_s": uptime,
            "exe": exe, "args": exe}


class Machine:
    """A fake process table plus a record of everything done to it."""

    def __init__(self, *procs_):
        self.procs = {p["pid"]: p for p in procs_}
        self.calls = []              # ("suspend" | "resume" | "terminate" | "kill", pid)
        self.deny = set()            # pids that raise
        self.survive = set()         # pids that outlive a plain terminate
        self.front = set()           # pids that are their terminal's foreground job
        self.shells = {}             # agent pid -> its ancestors on that terminal, nearest first
        self.wakes = set()           # pids continued the moment they stop (a tmux pane's shell)

    def parents(self, pid):
        return [(x, self.procs[x]["uptime_s"]) for x in self.shells.get(pid, ()) if pid in self.front]

    def do(self, what):
        def fn(pid, force=False):
            if pid in self.deny:
                raise OSError("Access is denied")
            name = "kill" if force else what
            self.calls.append((name, pid))
            if name in ("suspend", "resume") and pid in self.procs:
                self.procs[pid]["stat"] = "T" if name == "suspend" and pid not in self.wakes else "S"
                if name == "suspend" and not any("T" in self.procs[x]["stat"] for x in self.shells.get(pid, ())):
                    self.front.discard(pid)        # the shell takes the terminal back
            if name == "kill" or (name == "terminate" and pid not in self.survive):
                self.procs.pop(pid, None)
        return fn


class Act(unittest.TestCase):
    def setUp(self):
        self.log = os.path.join(os.environ["CADRE_HOME"], "act-test-%d.jsonl" % id(self))
        # agent 500 with a shell 501 that runs 502; an unrelated 900; cADRe itself (this test)
        self.m = Machine(proc(500, 1), proc(501, 500, "bash", 30), proc(502, 501, "rm", 5),
                         proc(900, 1, "vim"), proc(ME, 1, "python", 10))
        self.snap = {"generated_at": NOW, "agents": [agent(), agent("claude-code:s-9", None, status="sleeping")]}
        for name, value in (
                ("cadre.respond.audit.path", lambda: self.log),
                ("cadre.procs.list_processes", lambda: {k: dict(v) for k, v in self.m.procs.items()}),
                ("cadre.procs.fill_details", lambda procs_, pids: None),
                ("cadre.procs.suspend", self.m.do("suspend")),
                ("cadre.procs.resume", self.m.do("resume")),
                ("cadre.procs.terminate", self.m.do("terminate")),
                ("cadre.procs.alive", lambda pid: pid in self.m.procs),
                ("cadre.procs.foreground", lambda pid: pid in self.m.front),
                ("cadre.procs.terminal_parents", self.m.parents),
                ("cadre.respond.actions.GRACE", 0.3),
                ("cadre.respond.actions.HOLD_CHECK", 0),
                ("cadre.respond.actions._front", set()),
                ("cadre.respond.actions._held", {}),
                ("cadre.respond.actions._paused", {})):
            p = mock.patch(name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self.log) and os.remove(self.log))

    def act(self, action, agent_id="claude-code:500", **kw):
        return respond.act(self.snap, agent_id, action, now=NOW, **kw)

    def logged(self):
        return audit.entries(file=self.log)

    # -- requests that make no sense: nothing done, nothing logged
    def test_bad_requests(self):
        for agent_id, action, kw in (("claude-code:500", "delete", {}), ("claude-code:500", "", {}),
                                     ("nobody:1", "pause", {}), ("claude-code:s-9", "pause", {}),
                                     ("500", "pause", {}), ("claude-code:500", "kill", {}),
                                     ("claude-code:500", "kill", {"confirm": "yes"})):
            with self.assertRaises(ValueError, msg=(agent_id, action)):
                self.act(action, agent_id, **kw)
        self.assertEqual(self.m.calls, [])
        self.assertEqual(self.logged(), [])

    # -- pause
    def test_pause_stops_the_agent_then_its_children(self):
        e = self.act("pause")
        self.assertEqual(self.m.calls, [("suspend", 500), ("suspend", 501), ("suspend", 502)])
        self.assertTrue(e["ok"])
        self.assertEqual(e["pids"], [500, 501, 502])
        self.assertEqual(e["result"], "paused pid 500 and 2 child processes")
        self.assertTrue(respond.paused_by_us(500, NOW - 600))
        self.assertFalse(respond.paused_by_us(500, NOW - 60), "a later process with that pid")
        self.assertFalse(respond.paused_by_us(900))

    def test_pause_twice_is_refused(self):
        self.act("pause")
        e = self.act("pause")
        self.assertFalse(e["ok"])
        self.assertIn("already suspended", e["result"])
        self.assertEqual(len(self.m.calls), 3, "Windows counts suspensions: a second one needs a second resume")

    def test_pause_of_a_process_stopped_by_someone_else_is_refused(self):
        self.m.procs[500]["stat"] = "T"
        self.assertFalse(self.act("pause")["ok"])
        self.assertEqual(self.m.calls, [])

    def test_resume(self):
        self.act("pause")
        self.m.calls.clear()
        e = self.act("resume")
        self.assertEqual(self.m.calls, [("resume", 500), ("resume", 501), ("resume", 502)])
        self.assertEqual((e["ok"], e["result"]), (True, "resumed pid 500 and 2 child processes"))
        self.assertFalse(respond.paused_by_us(500))

    # -- an agent in a terminal (macOS, Linux): its shell is stopped with it, or the shell
    #    would take the keyboard and Resume could not give it back
    def in_a_terminal(self):
        self.m.procs[300] = proc(300, 1, "login", 5000)
        self.m.procs[400] = proc(400, 300, "zsh", 900)
        self.m.procs[500]["ppid"] = 400
        self.m.front, self.m.shells = {500}, {500: [400, 300]}
        self.m.deny = {300}                        # login runs as root

    def test_pause_holds_the_shell_and_resume_lets_it_go_last(self):
        self.in_a_terminal()
        e = self.act("pause")
        self.assertEqual(self.m.calls, [("suspend", 400), ("suspend", 500), ("suspend", 501), ("suspend", 502)])
        self.assertEqual(e["held"], [[400, int(NOW - 900)]])
        self.assertEqual(e["pids"], [500, 501, 502], "the shell is not one of the agent's processes")
        self.assertIn("stands still until you resume", e["result"])
        self.assertNotIn("foreground", e)
        self.m.calls.clear()
        e = self.act("resume")
        self.assertEqual(self.m.calls, [("resume", 500), ("resume", 501), ("resume", 502), ("resume", 400)])
        self.assertEqual(e["result"], "resumed pid 500 and 2 child processes")
        self.assertFalse(respond.needs_fg(500))
        self.m.calls.clear()
        self.act("pause"), self.act("resume")
        self.assertEqual(self.m.calls.count(("resume", 400)), 1, "and again")

    def test_kill_of_a_paused_agent_lets_the_shell_go(self):
        self.in_a_terminal()
        self.act("pause")
        self.m.calls.clear()
        self.act("kill", confirm=True)
        self.assertEqual(self.m.calls, [("kill", 500), ("kill", 501), ("kill", 502), ("resume", 400)])

    def test_a_pause_that_fails_lets_the_shell_go(self):
        self.in_a_terminal()
        self.m.deny = {300, 500}
        e = self.act("pause")
        self.assertFalse(e["ok"])
        self.assertEqual(self.m.calls, [("suspend", 400), ("resume", 400)])
        self.assertEqual(self.m.procs[400]["stat"], "S")

    def test_the_shell_is_remembered_across_a_restart(self):
        self.in_a_terminal()
        self.act("pause")
        self.m.calls.clear()
        with mock.patch("cadre.respond.actions._paused", None), mock.patch("cadre.respond.actions._held", {}):
            self.act("resume")
        self.assertEqual(self.m.calls[-1], ("resume", 400))

    def test_a_held_pid_that_is_someone_else_now_is_left_alone(self):
        self.in_a_terminal()
        self.act("pause")
        self.m.procs[400] = proc(400, 1, "vim", 3, stat="T")       # the shell died, its pid was reused
        self.m.calls.clear()
        self.act("resume")
        self.assertNotIn(("resume", 400), self.m.calls)
        self.act("pause")
        self.m.procs[400]["stat"] = "S"                            # someone continued the shell already
        self.m.calls.clear()
        self.act("resume")
        self.assertNotIn(("resume", 400), self.m.calls)

    def test_an_agent_that_ended_while_paused_frees_its_shell(self):
        self.in_a_terminal()
        self.act("pause")
        respond.release_orphans(self.m.procs, NOW)
        self.assertEqual(self.m.procs[400]["stat"], "T", "the agent is still there: nothing to do")
        for pid in (501, 502):
            del self.m.procs[pid]                  # kill -9 from another terminal
        self.m.calls.clear()
        with mock.patch("cadre.procs.alive", return_value=False):   # 500: listed, but a zombie
            respond.release_orphans(self.m.procs, NOW)
        del self.m.procs[500]
        self.assertEqual(self.m.calls, [("resume", 400)])
        last = self.logged()[-1]
        self.assertEqual((last["action"], last["source"], last["pids"]), ("resume", "cadre", [400]))
        respond.release_orphans(self.m.procs, NOW)
        self.assertEqual(self.m.calls, [("resume", 400)], "once")
        self.assertFalse(respond.paused_by_us(500))

    def test_a_shell_tmux_wakes_up_is_not_called_held(self):
        self.in_a_terminal()
        self.m.wakes = {400}                       # tmux: SIGCONT to its pane's shell when it stops
        e = self.act("pause")
        self.assertNotIn("held", e)
        self.assertTrue(e["shell_woken"] and e["foreground"])
        self.assertIn("back at the shell prompt", e["result"])
        self.assertNotIn("stands still", e["result"])
        self.m.calls.clear()
        e = self.act("resume")
        self.assertIn("type fg in its terminal", e["result"])
        self.assertNotIn(("resume", 400), self.m.calls, "it runs already")

    # -- Linux: the terminal tab's / tmux pane's scope is frozen instead (no signal at all)
    def in_a_scope(self, members=(400, 500, 501, 502)):
        self.in_a_terminal()
        self.m.scope = {"cg": "/user.slice/app.slice/tmux-spawn-1.scope", "members": list(members), "frozen": False}
        sc = self.m.scope

        def freeze(cg, on=True):
            if sc.get("deny"):
                raise OSError("Permission denied")
            sc["frozen"] = on
            for x in sc["members"]:
                if x in self.m.procs:
                    self.m.procs[x]["stat"] = "T" if on else "S"
            (respond.actions.procs._frozen.add if on else respond.actions.procs._frozen.discard)(cg)
        for name, value in (("cadre.procs.IS_LINUX", True),
                            ("cadre.procs.terminal_scope", lambda pid: sc["cg"]),
                            ("cadre.procs.cgroup_pids", lambda cg: list(sc["members"])),
                            ("cadre.procs.is_frozen", lambda cg: sc["frozen"]),
                            ("cadre.procs.freeze", freeze),
                            ("cadre.procs._frozen", set()),
                            ("cadre.respond.actions._scopes", {})):
            p = mock.patch(name, value)
            p.start()
            self.addCleanup(p.stop)
        return sc

    def test_pause_freezes_the_scope_and_sends_no_signal(self):
        sc = self.in_a_scope()
        e = self.act("pause")
        self.assertEqual(self.m.calls, [], "no SIGSTOP to the agent or its shell")
        self.assertTrue(sc["frozen"])
        self.assertEqual(e["frozen"], sc["cg"])
        self.assertNotIn("held", e)
        self.assertNotIn("also_frozen", e)
        self.assertEqual(e["pids"], [500, 501, 502])
        self.assertEqual(e["result"], "paused pid 500 and 2 child processes; its terminal stands still until you resume it")
        self.assertIn("already suspended", self.act("pause")["result"])
        e = self.act("resume")
        self.assertEqual((e["ok"], e["result"]), (True, "resumed pid 500 and 2 child processes"))
        self.assertFalse(sc["frozen"])
        self.assertEqual(self.m.calls, [])
        self.assertNotIn("needs_fg", e)

    def test_what_else_runs_in_that_tab_is_named(self):
        self.m.procs[600] = proc(600, 400, "npm", 50)
        self.in_a_scope((400, 500, 501, 502, 600))
        e = self.act("pause")
        self.assertEqual(e["also_frozen"], [[600, "npm"]])
        self.assertIn("also frozen in that terminal: npm (600)", e["result"])

    def test_no_freeze_when_cadre_or_a_child_is_outside_or_cadre_inside(self):
        for members in ((400, 500, 501), (400, 500, 501, 502, ME)):
            with self.subTest(members=members):
                self.setUp()
                sc = self.in_a_scope(members)
                e = self.act("pause")
                self.assertFalse(sc["frozen"])
                self.assertNotIn("frozen", e)
                self.assertIn(("suspend", 400), self.m.calls, "the old way: hold the shell")
                self.act("resume")

    def test_a_scope_that_wont_freeze_falls_back(self):
        sc = self.in_a_scope()
        sc["deny"] = True
        e = self.act("pause")
        self.assertTrue(e["ok"])
        self.assertNotIn("frozen", e)
        self.assertEqual(e["held"], [[400, int(NOW - 900)]])

    def test_kill_of_a_frozen_agent_kills_then_thaws(self):
        sc = self.in_a_scope()
        self.act("pause")
        e = self.act("kill", confirm=True)
        self.assertTrue(e["ok"])
        self.assertEqual(self.m.calls, [("kill", 500), ("kill", 501), ("kill", 502)], "SIGKILL: frozen, it can't act on SIGTERM")
        self.assertFalse(sc["frozen"], "thawed, so the shell collects it")

    def test_an_agent_killed_while_frozen_thaws_its_tab(self):
        sc = self.in_a_scope()
        self.act("pause")
        for x in (500, 501, 502):
            del self.m.procs[x]
        respond.release_orphans(self.m.procs, NOW + 5)
        self.assertFalse(sc["frozen"])
        self.assertIn("ended while paused", self.logged()[-1]["result"])

    def test_a_frozen_scope_is_remembered_across_a_restart(self):
        sc = self.in_a_scope()
        self.act("pause")
        with mock.patch("cadre.respond.actions._paused", None), mock.patch("cadre.respond.actions._scopes", {}):
            self.assertEqual(self.act("resume")["result"], "resumed pid 500 and 2 child processes")
        self.assertFalse(sc["frozen"])
        self.assertEqual(self.m.calls, [])

    def test_cadre_is_never_held(self):
        self.in_a_terminal()
        self.m.shells = {500: [ME, 300]}           # the agent was started by cADRe's own shell wrapper
        self.act("pause")
        self.assertNotIn(("suspend", ME), self.m.calls)

    # -- ... and when the shell can't be held, the user has to type fg
    def test_resume_of_a_terminal_job_says_to_type_fg(self):
        self.m.front = {500}
        e = self.act("pause")
        self.assertTrue(e["foreground"])
        self.assertIn("back at the shell prompt", e["result"])
        self.assertFalse(respond.needs_fg(500), "paused: nothing to type yet")
        e = self.act("resume")
        self.assertTrue(e["ok"] and e["needs_fg"])
        self.assertIn("type fg in its terminal", e["result"])
        self.assertTrue(respond.needs_fg(500))
        self.m.procs[500]["stat"] = "T"            # it read the keyboard: stopped again (SIGTTIN)
        self.assertIn("type fg", self.act("resume")["result"], "a second Resume says it again")
        self.m.front = {500}                       # the user typed fg
        self.assertFalse(respond.needs_fg(500))
        self.m.front = set()                       # later put in the background by the user
        self.assertFalse(respond.needs_fg(500), "no longer our business")

    def test_no_fg_hint_for_an_agent_that_was_not_in_front(self):
        e = self.act("pause")
        self.assertNotIn("foreground", e)
        e = self.act("resume")
        self.assertNotIn("needs_fg", e)
        self.assertEqual(e["result"], "resumed pid 500 and 2 child processes")

    def test_the_fg_hint_survives_a_restart_and_ends_with_a_kill(self):
        self.m.front = {500}
        self.act("pause")
        with mock.patch("cadre.respond.actions._paused", None), mock.patch("cadre.respond.actions._front", set()):
            self.assertIn("type fg", self.act("resume")["result"])
        self.act("kill", confirm=True)
        self.assertFalse(respond.needs_fg(500))

    def test_an_agent_the_user_continued_is_not_paused(self):
        self.act("pause")
        self.m.procs[500]["stat"] = "S"            # fg in its terminal, or kill -CONT
        with mock.patch("cadre.procs.sees_stopped", return_value=True):
            self.assertFalse(respond.paused_by_us(500, NOW - 600, "S"))
            self.assertTrue(respond.paused_by_us(500, NOW - 600, "T"))
            self.assertTrue(self.act("pause")["ok"], "it can be paused again")
        with mock.patch("cadre.procs.sees_stopped", return_value=False):     # Windows: trust the log
            self.assertTrue(respond.paused_by_us(500, NOW - 600, "S"))

    # -- kill
    def test_kill_ends_the_whole_tree(self):
        e = self.act("kill", confirm=True)
        self.assertEqual(self.m.calls, [("terminate", 500), ("terminate", 501), ("terminate", 502)])
        self.assertEqual((e["ok"], e["result"]), (True, "ended pid 500 and 2 child processes"))
        self.assertEqual(sorted(self.m.procs), sorted([900, ME]), "nothing else was touched")

    def test_kill_forces_what_ignores_the_request(self):
        self.m.survive = {501}
        e = self.act("kill", confirm=True)
        self.assertEqual(self.m.calls[-1], ("kill", 501))
        self.assertIn("(1 by force)", e["result"])
        self.assertNotIn(501, self.m.procs)

    def test_kill_of_a_paused_agent_is_by_force(self):
        self.act("pause")
        self.m.calls.clear()
        self.act("kill", confirm=True)
        self.assertEqual(self.m.calls, [("kill", 500), ("kill", 501), ("kill", 502)])
        self.assertFalse(respond.paused_by_us(500))

    # -- the process is checked again before anything is done
    def test_process_gone(self):
        del self.m.procs[500]
        e = self.act("kill", confirm=True)
        self.assertEqual((e["ok"], e["result"]), (False, "process 500 is already gone"))
        self.assertEqual(self.m.calls, [])

    def test_pid_reused_by_another_program(self):
        self.m.procs[500] = proc(500, 1, "notepad.exe", 2)
        e = self.act("kill", confirm=True)
        self.assertFalse(e["ok"])
        self.assertIn("no longer Claude Code", e["result"])
        self.assertEqual(self.m.calls, [])

    def test_pid_reused_by_a_newer_run_of_the_same_agent(self):
        self.m.procs[500]["uptime_s"] = 4         # the snapshot's agent had been up for 600 s
        e = self.act("pause")
        self.assertFalse(e["ok"])
        self.assertIn("newer process", e["result"])
        self.assertEqual(self.m.calls, [])

    def test_a_stale_child_is_left_alone(self):
        # 700 says its parent is 500, but it is older than 500: its real parent is long gone
        self.m.procs[700] = proc(700, 500, "postgres", 90000)
        e = self.act("kill", confirm=True)
        self.assertNotIn(700, e["pids"])
        self.assertIn(700, self.m.procs)

    def test_cadre_never_stops_itself(self):
        # the dashboard was started from the agent's own terminal: agent 500 > bash 501 > cADRe
        self.m.procs[ME]["ppid"] = 501
        self.m.procs[950] = proc(950, ME, "powershell.exe", 1)
        for action, kw in (("pause", {}), ("kill", {"confirm": True})):
            e = self.act(action, **kw)
            self.assertTrue(e["ok"], e)
            self.assertNotIn(ME, e["pids"])
            self.assertNotIn(950, e["pids"])
            self.assertIn("cADRe runs under this agent", e["result"])
        self.assertFalse({pid for _, pid in self.m.calls} & {ME, 950})

    def test_cadre_is_never_the_target(self):
        self.snap["agents"].append(agent("claude-code:%d" % ME, ME))
        self.m.procs[ME]["exe"] = self.m.procs[ME]["args"] = "claude"
        self.m.procs[ME]["uptime_s"] = 600
        e = self.act("kill", "claude-code:%d" % ME, confirm=True)
        self.assertEqual((e["ok"], e["result"]), (False, "that is cADRe itself"))
        self.assertEqual(self.m.calls, [])

    # -- failures
    def test_agent_that_cannot_be_touched(self):
        self.m.deny = {500}
        e = self.act("pause")
        self.assertEqual((e["ok"], e["result"], e["pids"]), (False, "Access is denied", []))
        self.assertEqual(self.m.calls, [], "its children aren't touched when the agent itself can't be")
        self.assertFalse(respond.paused_by_us(500))

    def test_child_that_cannot_be_touched(self):
        self.m.deny = {502}
        e = self.act("pause")
        self.assertTrue(e["ok"])
        self.assertEqual(e["pids"], [500, 501])
        self.assertIn("1 child process couldn't be reached", e["result"])

    # -- the log
    def test_every_call_is_logged(self):
        self.snap["agents"][0]["flags"] = ["recursive force delete (rm -rf /)"]
        self.act("pause")
        del self.m.procs[500]
        self.act("resume")
        a, b = self.logged()
        self.assertEqual((a["action"], a["ok"], a["agent_id"], a["agent"], a["project"], a["pid"], a["source"]),
                         ("pause", True, "claude-code:500", "Claude Code", "shop", 500, "dashboard"))
        self.assertEqual((a["session_id"], a["started"], a["ts"]), ("s-1", int(NOW - 600), NOW))
        self.assertEqual(a["flags"], ["recursive force delete (rm -rf /)"])
        self.assertEqual((b["action"], b["ok"]), ("resume", False))

    def test_paused_agents_are_remembered_across_a_restart(self):
        self.act("pause")
        with mock.patch("cadre.respond.actions._paused", None):     # a freshly started dashboard
            self.assertTrue(respond.paused_by_us(500, NOW - 600))
            self.act("resume")
        with mock.patch("cadre.respond.actions._paused", None):
            self.assertFalse(respond.paused_by_us(500, NOW - 600))

    def test_an_unwritable_log_does_not_undo_the_answer(self):
        with mock.patch("cadre.respond.audit.append", side_effect=OSError("disk full")):
            e = self.act("pause")
        self.assertTrue(e["ok"])
        self.assertIn("not logged", e["result"])


# ---------------------------------------------------------------- procs: one pid
class FakePsutil:
    STATUS_ZOMBIE = "zombie"

    class NoSuchProcess(Exception):
        pass

    def __init__(self):
        self.calls, self.gone = [], set()

    def Process(self, pid):
        ps = self
        if pid in self.gone:
            raise self.NoSuchProcess("process no longer exists (pid=%d)" % pid)

        class P:
            def __getattr__(self, name):
                return lambda: ps.calls.append((name, pid))
        return P()


class ProcsCalls(unittest.TestCase):
    def test_psutil_is_used_when_there(self):
        ps = FakePsutil()
        with mock.patch.object(procs, "psutil", ps):
            procs.suspend(5)
            procs.resume(5)
            procs.terminate(5)
            procs.terminate(5, force=True)
        self.assertEqual(ps.calls, [("suspend", 5), ("resume", 5), ("terminate", 5), ("kill", 5)])

    def test_psutil_errors_become_oserror(self):
        ps = FakePsutil()
        ps.gone.add(5)
        with mock.patch.object(procs, "psutil", ps):
            for fn in (procs.suspend, procs.resume, procs.terminate):
                with self.assertRaises(OSError) as cm:
                    fn(5)
                self.assertIn("no longer exists", str(cm.exception))

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_signals_without_psutil(self):
        import signal
        with mock.patch.object(procs, "psutil", None), mock.patch("os.kill") as kill:
            procs.suspend(5)
            procs.resume(5)
            procs.terminate(5)
            procs.terminate(5, force=True)
        self.assertEqual([c.args for c in kill.call_args_list],
                         [(5, signal.SIGSTOP), (5, signal.SIGCONT), (5, signal.SIGTERM), (5, signal.SIGKILL)])

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_without_psutil_never_signals_zero(self):
        with mock.patch.object(procs, "psutil", None), mock.patch("os.kill") as kill:
            self.assertFalse(procs.alive(0x7FFFFFF0))       # os.kill(pid, 0) would END a process here
            kill.assert_not_called()
            with self.assertRaises(OSError):
                procs.suspend(0x7FFFFFF0)


class RealProcess(unittest.TestCase):
    """Not a mock: a child this test starts is suspended, resumed and ended, with the
    machine's backend and with the no-psutil fallback."""

    def run_through(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(procs.alive(child.pid))
            procs.suspend(child.pid)
            if procs.psutil:
                deadline = time.time() + 3
                while procs.psutil.Process(child.pid).status() != procs.psutil.STATUS_STOPPED and time.time() < deadline:
                    time.sleep(0.05)
                self.assertEqual(procs.psutil.Process(child.pid).status(), procs.psutil.STATUS_STOPPED)
            self.assertTrue(procs.alive(child.pid), "suspended is not gone")
            procs.resume(child.pid)
            if procs.psutil:
                self.assertNotEqual(procs.psutil.Process(child.pid).status(), procs.psutil.STATUS_STOPPED)
            procs.terminate(child.pid)
            deadline = time.time() + 5             # not waited for yet: on POSIX it is a zombie
            while procs.alive(child.pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(procs.alive(child.pid), "ended but not yet reaped is not alive")
            child.wait(10)
            self.assertFalse(procs.alive(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(10)

    def test_with_this_machines_backend(self):
        self.run_through()

    @unittest.skipUnless(procs.psutil, "already covered: no psutil on this machine")
    def test_with_the_fallback(self):
        real = procs.psutil
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with mock.patch.object(procs, "psutil", None):
                self.assertTrue(procs.alive(child.pid))
                procs.suspend(child.pid)
            deadline = time.time() + 3
            while real.Process(child.pid).status() != real.STATUS_STOPPED and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(real.Process(child.pid).status(), real.STATUS_STOPPED)
            with mock.patch.object(procs, "psutil", None):
                self.assertTrue(procs.alive(child.pid))
                procs.resume(child.pid)
                self.assertNotEqual(real.Process(child.pid).status(), real.STATUS_STOPPED)
                procs.terminate(child.pid)
                child.wait(10)
                self.assertFalse(procs.alive(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(10)


# ---------------------------------------------------------------- the server
class Endpoint(unittest.TestCase):
    def test_bad_bodies_are_rejected(self):
        for body in ([], "kill", {}, {"id": "claude-code:1"}, {"action": "kill"}, {"id": 1, "action": "kill"},
                     {"id": "claude-code:1", "action": ["kill"]}):
            with self.assertRaises(ValueError, msg=repr(body)):
                server.respond_action(body, {"agents": []})

    def test_the_agent_comes_from_the_snapshot_not_the_request(self):
        with mock.patch("cadre.respond.act", return_value={"ok": True}) as act:
            server.respond_action({"id": "claude-code:500", "action": "kill", "confirm": True, "pid": 4}, "SNAP")
            act.assert_called_once_with("SNAP", "claude-code:500", "kill", confirm=True)
        with self.assertRaises(ValueError):           # not in the snapshot: refused before any process call
            server.respond_action({"id": "claude-code:4", "action": "pause"}, {"agents": [agent()]})

    def test_origin(self):
        names = server.allowed_hosts("127.0.0.1")
        for ok in (None, "", "http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
            self.assertTrue(server.origin_ok(ok, names), ok)
        for bad in ("null", "https://evil.com", "http://evil.com:8765", "http://127.0.0.1.evil.com",
                    "file://", "evil.com"):
            self.assertFalse(server.origin_ok(bad, names), bad)


# ---------------------------------------------------------------- the snapshot
class Snapshot(unittest.TestCase):
    def test_paused_agent_shows_as_suspended_on_a_backend_that_cannot_see_it(self):
        from cadre import collectors
        claude = proc(4242, 1, r"C:\Users\x\.local\bin\claude.exe")      # stat "S": the CIM backend
        with mock.patch("cadre.collectors.list_processes", return_value={4242: claude}), \
                mock.patch("cadre.collectors.fill_details"), \
                mock.patch("cadre.collectors.proc_cwds", return_value={}), \
                mock.patch("cadre.collectors.proc_env", return_value={}), \
                mock.patch("cadre.collectors.newest_unclaimed_transcript", return_value=None), \
                mock.patch("cadre.collectors.claude_live_sessions", return_value={}), \
                mock.patch("cadre.collectors.all_claude_transcripts", return_value=[]), \
                mock.patch("cadre.collectors.codex.session_files", return_value=[]), \
                mock.patch("cadre.collectors.zcode.read_sessions", return_value=[]), \
                mock.patch("cadre.collectors.zcode.read_events", return_value=[]), \
                mock.patch("cadre.collectors.hook_coverage", return_value={"targets": [], "pending": 0}), \
                mock.patch("cadre.collectors.installed_evidence", return_value=None), \
                mock.patch("cadre.procs.sees_stopped", return_value=False):
            with mock.patch("cadre.respond.actions._paused", {}):
                rec = collectors.snapshot()["agents"][0]
                self.assertNotEqual(rec["status"], "suspended")
                self.assertNotIn("paused_by_cadre", rec)
            with mock.patch("cadre.respond.actions._paused", {4242: time.time() - 600}):
                snap = collectors.snapshot()
        rec = snap["agents"][0]
        self.assertEqual((rec["status"], rec["paused_by_cadre"]), ("suspended", True))
        self.assertEqual(snap["counts"].get("suspended"), 1)
        self.assertIsInstance(snap["alerts"], list)
        self.assertIsInstance(snap["responses"], list)
        json.dumps(snap)


# ---------------------------------------------------------------- the status icons
class TrayAlerts(unittest.TestCase):
    """cadre/statusmenu.py: shared by the Windows tray and the Linux status icon."""
    def alert(self, ts, text="recursive force delete (rm -rf /)", agent_="Claude Code", project="shop"):
        return {"id": "a%d" % ts, "ts": ts, "agent": agent_, "project": project, "text": text}

    def test_each_alert_is_shown_once(self):
        from cadre import statusmenu as tray
        snap = {"alerts": [self.alert(30), self.alert(20), self.alert(10)]}
        new, mark = tray.fresh_alerts(snap, 15)
        self.assertEqual(([a["ts"] for a in new], mark), ([30, 20], 30))
        self.assertEqual(tray.fresh_alerts(snap, mark), ([], 30))
        self.assertEqual(tray.fresh_alerts(None, 30), ([], 30))
        self.assertEqual(tray.fresh_alerts({}, 30), ([], 30))

    def test_text(self):
        from cadre import statusmenu as tray
        title, text = tray.alert_text([self.alert(1)])
        self.assertEqual((title, text), ("cADRe: Claude Code · shop", "recursive force delete (rm -rf /)"))
        many = [self.alert(i, "flag %d" % i, project="p%d" % (i % 2)) for i in range(5)]
        title, text = tray.alert_text(many)
        self.assertEqual(title, "cADRe: 5 risk flags")
        self.assertEqual(text.split("\n"), ["p0: flag 0", "p1: flag 1", "p0: flag 2", "+2 more"])

    @unittest.skipUnless(os.name == "nt", "the tray icon is Windows only")
    def test_refresh_notifies_for_a_new_alert_only(self):
        from cadre import tray
        with mock.patch.object(tray, "_api"):
            t = tray.Tray(8765)
        t.notify, t.balloon, t.alert_mark = mock.Mock(), mock.Mock(), 15
        t.snap = {"counts": {}, "agents": [], "alerts": [self.alert(20), self.alert(10)]}
        t.refresh()
        t.balloon.assert_called_once_with("cADRe: Claude Code · shop", "recursive force delete (rm -rf /)")
        t.refresh()
        t.refresh()
        self.assertEqual(t.balloon.call_count, 1)


if __name__ == "__main__":
    unittest.main()
