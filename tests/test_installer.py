"""Hook installer: it edits other tools' config files, so these check it leaves them intact."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from cadre import installer
from cadre.installer import Target, apply

FOREIGN = "/usr/local/bin/other-tool --notify"


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cadre-inst-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.emit = os.path.join(self.dir, "app", "cadre", "hooks", "emit.py")
        old = installer.EMIT
        installer.use_emit(self.emit)
        self.addCleanup(installer.use_emit, old)

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def target(self, agent, path):
        return Target(agent, installer.FORMAT_NAMES[agent], path, ["test"])

    def cmds(self, data):
        """Every hook command in a settings file, nested or flat."""
        out = []
        for groups in (data.get("hooks") or {}).values():
            for g in groups:
                for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else [g]):
                    out.append(installer._cmd_of(h).strip())
        return out


class ClaudeCodeSettings(InstallerCase):
    def setUp(self):
        super().setUp()
        self.file = self.path("claude", "settings.json")
        self.original = {
            "model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}, "note": "ünïcode ✓",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": FOREIGN}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": FOREIGN}]}]},
        }
        self.write(self.file, self.original)
        self.t = self.target("claude-code", self.file)

    def test_install_adds_ours_and_keeps_everything_else(self):
        msg = apply(self.t, install=True)
        self.assertEqual(msg, "install 7 hooks")
        data = self.read(self.file)
        for k in ("model", "permissions", "note"):
            self.assertEqual(data[k], self.original[k])
        self.assertEqual(data["hooks"]["PreToolUse"], self.original["hooks"]["PreToolUse"])
        self.assertIn(FOREIGN, self.cmds(data))
        ours = [c for c in self.cmds(data) if installer.OURS.search(c)]
        self.assertEqual(len(ours), 7)
        # EMIT_CMD: the path as written into commands (forward slashes on Windows)
        self.assertTrue(all(installer.EMIT_CMD in c for c in ours))
        for ev in installer.FORMATS["claude-code"].events:
            self.assertIn(ev, data["hooks"])
        # never a blocking/"before" event
        self.assertNotIn("--event PreToolUse", " ".join(ours))

    def test_backup_holds_the_original(self):
        apply(self.t, install=True)
        self.assertEqual(self.read(self.file + ".cadre-bak"), self.original)
        self.assertFalse(os.path.exists(self.file + ".cadre-tmp"))

    def test_second_install_is_up_to_date(self):
        apply(self.t, install=True)
        with open(self.file, "rb") as f:
            first = f.read()
        self.assertEqual(apply(self.t, install=True), "up to date")
        with open(self.file, "rb") as f:
            self.assertEqual(f.read(), first)

    def test_uninstall_restores_the_original(self):
        apply(self.t, install=True)
        self.assertEqual(apply(self.t, install=False), "remove 7 hooks")
        self.assertEqual(self.read(self.file), self.original)
        self.assertEqual(apply(self.t, install=False), "nothing to remove")

    def test_dry_run_writes_nothing(self):
        with open(self.file, "rb") as f:
            before = f.read()
        self.assertEqual(apply(self.t, install=True, dry_run=True), "would install 7 hooks")
        with open(self.file, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(self.file + ".cadre-bak"))

    def test_stale_path_is_repointed(self):
        old = "/usr/bin/python3 -I -S /Users/x/AgentTracker/cadre/hooks/emit.py --agent claude-code --event Stop"
        data = self.read(self.file)
        data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": old}]})
        self.write(self.file, data)
        msg = apply(self.t, install=True)
        self.assertIn("replacing 1 existing cADRe hook(s), 1 with a stale path", msg)
        self.assertNotIn(old, self.cmds(self.read(self.file)))

    def test_mixed_group_keeps_the_foreign_hook(self):
        mine = installer.command("claude-code", "Stop")
        self.write(self.file, {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": FOREIGN}, {"type": "command", "command": mine}]}]}})
        apply(self.t, install=False)
        self.assertEqual(self.read(self.file),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": FOREIGN}]}]}})


class FileEdgeCases(InstallerCase):
    def test_unparsable_file_is_skipped_untouched(self):
        f = self.path("claude", "settings.json")
        self.write(f, '{"model": "opus",, }')
        msg = apply(self.target("claude-code", f), install=True)
        self.assertTrue(msg.startswith("skipped: can't parse"), msg)
        with open(f, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"model": "opus",, }')
        self.assertFalse(os.path.exists(f + ".cadre-bak"))

    def test_non_object_top_level_is_skipped(self):
        f = self.path("claude", "settings.json")
        self.write(f, "[1, 2]")
        self.assertTrue(apply(self.target("claude-code", f), True).startswith("skipped"))

    def test_utf8_bom_is_read(self):
        f = self.path("claude", "settings.json")
        os.makedirs(os.path.dirname(f))
        with open(f, "w", encoding="utf-8-sig") as fh:
            json.dump({"model": "opus"}, fh)
        self.assertEqual(apply(self.target("claude-code", f), True), "install 7 hooks")
        self.assertEqual(self.read(f)["model"], "opus")

    def test_missing_and_empty_files(self):
        f = self.path("new", "settings.json")
        self.assertEqual(apply(self.target("claude-code", f), True), "install 7 hooks")
        self.assertTrue(os.path.isfile(f))
        g = self.path("empty", "settings.json")
        self.write(g, "  \n")
        self.assertEqual(apply(self.target("claude-code", g), True), "install 7 hooks")

    def test_written_as_utf8_text(self):
        f = self.path("claude", "settings.json")
        self.write(f, {"note": "ünïcode ✓"})
        apply(self.target("claude-code", f), True)
        with open(f, "rb") as fh:
            raw = fh.read()
        self.assertIn("ünïcode ✓".encode("utf-8"), raw)
        self.assertNotIn(b"\r\n", raw)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_file_mode_is_kept(self):
        f = self.path("claude", "settings.json")
        self.write(f, {})
        os.chmod(f, 0o600)
        apply(self.target("claude-code", f), True)
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o600)


class OtherFormats(InstallerCase):
    def test_copilot_own_file_is_deleted_on_uninstall(self):
        f = self.path("copilot", "hooks", "cadre.json")
        t = self.target("copilot", f)
        self.assertEqual(apply(t, True), "install 8 hooks")
        data = self.read(f)
        self.assertEqual(data["version"], 1)
        # Copilot runs `powershell` entries on Windows and `bash` ones elsewhere
        self.assertIn("powershell" if installer.IS_WIN else "bash", data["hooks"]["sessionStart"][0])
        self.assertEqual(apply(t, False), "remove 8 hooks and delete the file")
        self.assertFalse(os.path.exists(f))

    def test_cursor_flat_format_keeps_foreign_and_version(self):
        f = self.path("cursor", "hooks.json")
        self.write(f, {"version": 1, "hooks": {"stop": [{"command": FOREIGN}]}})
        t = self.target("cursor", f)
        apply(t, True)
        data = self.read(f)
        self.assertEqual(data["hooks"]["stop"][0], {"command": FOREIGN})
        self.assertEqual(len(data["hooks"]["stop"]), 2)
        apply(t, False)
        self.assertEqual(self.read(f), {"version": 1, "hooks": {"stop": [{"command": FOREIGN}]}})

    def test_gemini_timeout_is_milliseconds(self):
        f = self.path("gemini", "settings.json")
        apply(self.target("gemini", f), True)
        entry = self.read(f)["hooks"]["AfterTool"][0]["hooks"][0]
        self.assertEqual(entry["timeout"], 3000)

    def test_only_observe_only_events(self):
        blocking = {"PreToolUse", "PermissionRequest", "preToolUse", "BeforeTool", "beforeShellExecution",
                    "pre_run_command", "pre_write_code"}
        for key, fmt in installer.FORMATS.items():
            self.assertFalse(blocking & set(fmt.events), key)


class Commands(InstallerCase):
    def test_posix_command_is_silent_and_exits_0(self):
        with mock.patch.object(installer, "IS_WIN", False):
            c = installer.command("codex", "Stop", "posix")
        self.assertIn("-I -S", c)
        self.assertIn("--agent codex --event Stop", c)
        self.assertTrue(c.endswith(">/dev/null 2>&1; exit 0"))

    def test_windows_commands(self):
        with mock.patch.multiple(installer, IS_WIN=True,
                                 PYTHON="C:/Program Files/Python314/python.exe",
                                 EMIT="C:\\Users\\x\\AppData\\Local\\cADRe\\app\\cadre\\hooks\\emit.py",
                                 EMIT_CMD="C:/Users/x/AppData/Local/cADRe/app/cadre/hooks/emit.py"):
            plain = installer.command("claude-code", "Stop")
            ps = installer.command("copilot", "agentStop", "powershell")
        self.assertTrue(plain.startswith('"C:/Program Files/Python314/python.exe" -I -S '
                                         '"C:/Users/x/AppData/Local/cADRe/app/cadre/hooks/emit.py"'))
        self.assertNotIn("\\", plain)
        self.assertTrue(ps.startswith('& "C:/Program Files'))
        self.assertTrue(installer.OURS.search(plain))

    def test_ours_pattern(self):
        yes = ["/Users/x/cADRe/cadre/hooks/emit.py", "C:\\Users\\x\\cADRe\\cadre\\hooks\\emit.py",
               "/old/AgentTracker/agenttracker/hooks/emit.py", "C:/x/cadre/hooks/emit.py"]
        no = ["/usr/bin/notify", "/x/myhooks/emit.py", "/x/cadre/emit.py", "/x/other/hooks/emit.py"]
        for s in yes:
            self.assertTrue(installer.OURS.search(s), s)
        for s in no:
            self.assertFalse(installer.OURS.search(s), s)


class Discovery(InstallerCase):
    """discover() in a fake home: no running agents, nothing on PATH."""

    def run_discover(self):
        home = self.path("home")
        with mock.patch.object(installer, "HOME", home), \
                mock.patch.object(installer, "_running_homes", return_value={}), \
                mock.patch.object(installer, "_evidence", return_value=[]), \
                mock.patch.object(installer.shutil, "which", return_value=None):
            return home, installer.discover()

    def test_empty_home_finds_nothing(self):
        _, found = self.run_discover()
        self.assertEqual(found, [])

    def test_config_dirs_and_claude_alternates(self):
        home = self.path("home")
        for d in (".claude/projects", ".claude-glm", ".claude-empty", ".codex", ".copilot"):
            os.makedirs(os.path.join(home, d))
        self.write(os.path.join(home, ".claude-glm", "settings.json"), {})
        _, found = self.run_discover()
        got = {(t.agent, os.path.relpath(t.path, home)) for t in found}
        self.assertEqual(got, {
            ("claude-code", os.path.join(".claude", "settings.json")),
            ("claude-code", os.path.join(".claude-glm", "settings.json")),
            ("codex", os.path.join(".codex", "hooks.json")),
            ("copilot", os.path.join(".copilot", "hooks", "cadre.json")),
        })
        self.assertEqual(len(found), len(got))   # no path listed twice


class Coverage(InstallerCase):
    """check(): the hook state the dashboard shows, and install_paths() behind its buttons."""

    def state(self, agent, path):
        return installer.check(self.target(agent, path))["state"]

    def test_states(self):
        f = self.path("claude", "settings.json")
        self.assertEqual(self.state("claude-code", f), "missing")            # no file
        self.write(f, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": FOREIGN}]}]}})
        self.assertEqual(self.state("claude-code", f), "missing")
        apply(self.target("claude-code", f), True)
        self.assertEqual(self.state("claude-code", f), "ok")
        data = self.read(f)
        del data["hooks"]["SessionEnd"]
        self.write(f, data)
        self.assertEqual(self.state("claude-code", f), "outdated")
        with mock.patch.object(installer, "PYTHON", "/opt/python9/bin/python9"):   # a Python upgrade
            apply(self.target("claude-code", f), True)
        self.assertEqual(self.state("claude-code", f), "outdated")
        self.write(f, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command":
                       "python3 /old/AgentTracker/cadre/hooks/emit.py --event Stop"}]}]}})
        self.assertEqual(self.state("claude-code", f), "stale")
        self.write(f, "{nope")
        self.assertEqual(self.state("claude-code", f), "unreadable")

    def test_foreign_hooks_added_later_keep_it_ok(self):
        f = self.path("claude", "settings.json")
        apply(self.target("claude-code", f), True)
        data = self.read(f)
        data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": FOREIGN}]})
        self.write(f, data)
        self.assertEqual(self.state("claude-code", f), "ok")

    def test_flat_formats(self):
        f = self.path("copilot", "hooks", "cadre.json")
        apply(self.target("copilot", f), True)
        self.assertEqual(self.state("copilot", f), "ok")
        data = self.read(f)
        del data["version"]
        self.write(f, data)
        self.assertEqual(self.state("copilot", f), "outdated")

    def discover_in(self, home):
        return mock.patch.multiple(installer, HOME=home, _running_homes=mock.Mock(return_value={}),
                                   _evidence=mock.Mock(return_value=[]))

    def test_install_paths_only_touches_discovered_files(self):
        home = self.path("home")
        os.makedirs(os.path.join(home, ".codex"))
        os.makedirs(os.path.join(home, ".claude", "projects"))
        os.makedirs(os.path.dirname(self.emit))
        open(self.emit, "w").close()
        outside = self.path("elsewhere", "settings.json")
        with self.discover_in(home), mock.patch.object(installer.shutil, "which", return_value=None):
            out = installer.install_paths([outside, os.path.join(home, ".codex", "hooks.json")])
            self.assertEqual([r["agent"] for r in out["results"]], ["codex"])
            self.assertFalse(os.path.exists(outside))
            self.assertTrue(any("/hooks" in n for n in out["notes"]))
            cov = installer.scan()
            self.assertEqual({t["agent"]: t["state"] for t in cov["targets"]},
                             {"claude-code": "missing", "codex": "ok"})
            self.assertEqual(cov["pending"], 1)
            out = installer.install_paths(None)                  # "Install all pending"
            self.assertEqual([r["agent"] for r in out["results"]], ["claude-code"])
            self.assertEqual(installer.scan()["pending"], 0)
            self.assertEqual(installer.install_paths(None)["results"], [])

    def test_install_paths_dry_run_and_missing_emit(self):
        home = self.path("home")
        os.makedirs(os.path.join(home, ".codex"))
        with self.discover_in(home), mock.patch.object(installer.shutil, "which", return_value=None):
            out = installer.install_paths(None)                  # emit.py doesn't exist here
            self.assertEqual(out["results"], [])
            self.assertIn("emit.py not found", out["notes"][0])
            out = installer.install_paths(None, dry_run=True)
            self.assertEqual(out["results"][0]["result"], "would install 5 hooks")
            self.assertFalse(os.path.exists(os.path.join(home, ".codex", "hooks.json")))


if __name__ == "__main__":
    unittest.main()
