"""Install / uninstall cADRe's watch-only hook in every agent on this machine that supports hooks.

    python3 -m cadre --install-hooks [--dry-run] [--agents claude-code,codex]
    python3 -m cadre --uninstall-hooks [--dry-run]

Discovery covers running agents (config dirs taken from their environment, e.g.
CLAUDE_CONFIG_DIR) and installed or leftover ones (binaries, apps, config dirs).
A hook is "ours" when its command runs some `.../hooks/emit.py` of cADRe (or its old
name AgentTracker), so install also repoints stale entries left by a moved repo.
Every file is backed up to `<file>.cadre-bak` before it is changed.
"""
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import paths
from .collectors import match_spec
from .procs import list_processes, proc_env
from .registry import AGENTS, HOME, expand

# Hooks run the installed copy when `cadre setup` made one (so they survive moving the checkout),
# otherwise the emit.py next to this file.
EMIT = paths.APP_EMIT if paths.installed() else paths.PKG_EMIT
# matches .../cADRe/cadre/hooks/emit.py, the old .../AgentTracker/cadre/hooks/emit.py and
# .../agenttracker/hooks/emit.py, with / or \\ separators
OURS = re.compile(r"[/\\](cadre|agenttracker)[/\\]hooks[/\\]emit\.py", re.I)
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
SYSTEM_PYTHON = "/usr/bin/python3"
CLT_PYTHON = "/Library/Developer/CommandLineTools/usr/bin/python3"


def mac_system_python_ok():
    """True if /usr/bin/python3 is a real Python. Until the Command Line Tools (or Xcode) are
    installed it is only a stub that opens Apple's "install developer tools" dialog, which a
    hook or the login item must never run."""
    if os.path.isfile(CLT_PYTHON):
        return True
    try:
        r = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    dev = (r.stdout or "").strip()
    return r.returncode == 0 and bool(dev) and os.path.isfile(dev.rstrip("/") + "/usr/bin/python3")


def hook_python():
    """The interpreter written into hook commands (and, outside Windows, the login item).

    The same answer whoever runs the installer (CLI, `setup`, the dashboard's buttons), so
    `check()` never calls a fresh install outdated:
      * a release's bundled Python when there is one (paths.bundled_python()),
      * Windows: this Python's python.exe, also when running under pythonw.exe (the
        dashboard); hooks need a console program that waits and reads stdin,
      * macOS: /usr/bin/python3 (a fixed path that survives Python upgrades) if the Command
        Line Tools make it real, else this Python,
      * Linux: /usr/bin/python3 if present, else this Python.
    """
    bundled = paths.bundled_python()
    if bundled:
        return bundled
    exe = sys.executable or ("python" if IS_WIN else "python3")
    if IS_WIN:
        folder, name = os.path.split(exe)
        if name.lower() == "pythonw.exe" and os.path.isfile(os.path.join(folder, "python.exe")):
            exe = os.path.join(folder, "python.exe")
        return exe
    if IS_MAC:
        return SYSTEM_PYTHON if mac_system_python_ok() else exe
    return SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else exe


def python_note():
    """A line for `setup` when the hooks' Python is one that an upgrade can move."""
    if paths.bundled_python():
        return "Hooks use cADRe's own Python (%s)." % paths.bundled_python()
    if IS_MAC and PYTHON != SYSTEM_PYTHON:
        return ("The Xcode Command Line Tools aren't installed, so /usr/bin/python3 isn't usable: hooks "
                "use %s. Re-run setup after upgrading it, or run `xcode-select --install` and then "
                "setup again to switch to the system Python." % PYTHON)
    return "Hook commands contain %s: re-run setup after upgrading Python or pulling new code." % PYTHON


# forward slashes on Windows: they work in cmd, PowerShell and Git Bash alike (backslashes break in bash)
PYTHON = hook_python().replace("\\", "/") if IS_WIN else hook_python()
EMIT_CMD = EMIT.replace("\\", "/") if IS_WIN else EMIT


def use_emit(path):
    """Point new hook commands at another emit.py (setup: the installed copy)."""
    global EMIT, EMIT_CMD
    EMIT = path
    EMIT_CMD = path.replace("\\", "/") if IS_WIN else path

# Agents with no hook mechanism cADRe can use (shown so "all agents" is accounted for).
NO_HOOKS = {
    "claude-desktop": "no hook system (MCP only)",
    "zcode": "no hook system; read from its session DB instead",
    "aider": "no hook system",
    "goose": "no hook system",
    "opencode": "only JS plugins, no command hooks",
    "ollama": "model runtime, not an agent",
}


def command(agent, event, shell=None):
    """Hook command line. emit.py itself never prints and always exits 0; on macOS/Linux the
    shell wrapper also covers a missing/broken interpreter.

    shell: "posix" (sh/bash), "powershell", or "plain" (cmd, PowerShell or Git Bash: whatever
    the agent uses on Windows). Defaults to posix on macOS/Linux and plain on Windows.
    """
    shell = shell or ("plain" if IS_WIN else "posix")
    args = "-I -S %s --agent %s --event %s"
    if shell == "posix":
        return ("%s " + args + " >/dev/null 2>&1; exit 0") % (PYTHON, shlex.quote(EMIT), agent, event)
    exe = '"%s"' % PYTHON if " " in PYTHON else PYTHON
    line = ("%s " + args) % (exe, '"%s"' % EMIT_CMD, agent, event)
    return "& " + line if shell == "powershell" else line


@dataclass
class Format:
    events: List[str]
    entry: Callable[[str], dict]            # command -> hook entry
    nested: bool                            # event -> [{matcher?, hooks: [entry]}] vs event -> [entry]
    top: dict = field(default_factory=dict)  # required top-level keys, e.g. {"version": 1}
    own_file: bool = False                  # whole file belongs to cADRe (delete on uninstall)
    win_entry: Optional[Callable[[str, str], dict]] = None   # (agent, event) -> entry on Windows


FORMATS = {
    # https://code.claude.com/docs/en/hooks
    "claude-code": Format(
        ["SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure",
         "Notification", "Stop", "SessionEnd"],
        lambda c: {"type": "command", "command": c, "async": True, "timeout": 5}, nested=True),
    # https://developers.openai.com/codex/hooks  (timeout in seconds; SessionEnd max 3s)
    "codex": Format(
        ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"],
        lambda c: {"type": "command", "command": c, "async": True, "timeout": 3}, nested=True),
    # https://geminicli.com/docs/hooks/reference/  (timeout in ms, runs synchronously)
    "gemini": Format(
        ["SessionStart", "BeforeAgent", "AfterTool", "Notification", "AfterAgent", "SessionEnd"],
        lambda c: {"name": "cadre", "type": "command", "command": c, "timeout": 3000}, nested=True),
    # https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference
    "copilot": Format(
        ["sessionStart", "userPromptSubmitted", "postToolUse", "postToolUseFailure",
         "notification", "agentStop", "errorOccurred", "sessionEnd"],
        lambda c: {"type": "command", "bash": c, "timeoutSec": 3}, nested=False,
        top={"version": 1}, own_file=True,
        win_entry=lambda a, e: {"type": "command", "timeoutSec": 3,
                                "powershell": command(a, e, "powershell")}),
    # https://cursor.com/docs/hooks  (only observe-only events; permission hooks are avoided)
    "cursor": Format(
        ["sessionStart", "beforeSubmitPrompt", "postToolUse", "postToolUseFailure", "stop", "sessionEnd"],
        lambda c: {"command": c, "timeout": 3}, nested=False, top={"version": 1}),
    # Windsurf Cascade hooks (~/.codeium/windsurf/hooks.json); post-hooks only, cannot block
    "windsurf": Format(
        ["pre_user_prompt", "post_run_command", "post_write_code", "post_read_code",
         "post_mcp_tool_use", "post_cascade_response"],
        lambda c: {"command": c, "show_output": False}, nested=False),
}


@dataclass
class Target:
    agent: str
    name: str
    path: str
    why: List[str]                          # how it was found: running / installed / config dir


# ------------------------------------------------------------------ discovery
ENV_HOMES = {"claude-code": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "copilot": "COPILOT_HOME",
             "gemini": "GEMINI_CLI_HOME"}


def _running_homes(procs=None):
    """{agent_key: {config_home: pid}} for running agents that set a config-dir env var."""
    out = {}
    for p in (procs if procs is not None else list_processes()).values():
        spec = match_spec(p)
        if not spec or spec.key not in ENV_HOMES:
            continue
        var = ENV_HOMES[spec.key]
        home = proc_env(p["pid"], [var]).get(var)
        out.setdefault(spec.key, {})
        if home:
            out[spec.key][_norm(os.path.expanduser(home))] = p["pid"]
        else:
            out[spec.key].setdefault(None, p["pid"])
    return out


def _evidence(key):
    spec = next(a for a in AGENTS if a.key == key)
    ev = ["binary " + shutil.which(b) for b in spec.binaries if shutil.which(b)]
    ev += ["app " + expand(a) for a in spec.apps if os.path.exists(expand(a))]
    return ev


# the hook file inside each agent's config dir
FILENAMES = {"claude-code": "settings.json", "codex": "hooks.json", "gemini": "settings.json",
             "copilot": os.path.join("hooks", "cadre.json"), "cursor": "hooks.json", "windsurf": "hooks.json"}


def hook_file(agent, home=None):
    return os.path.join(_norm(home) if home else default_home(agent), FILENAMES[agent])


def discover(procs=None):
    """All hook files for agents found on this machine (running, installed or leftover)."""
    running = _running_homes(procs)
    targets = {}

    def add(agent, home, filename, extra=None):
        home = _norm(home)
        path = os.path.join(home, filename)
        why = []
        pids = running.get(agent, {})
        pid = pids.get(home) or (pids.get(None) if home == default_home(agent) else None)
        if pid:
            why.append("running (pid %d)" % pid)
        why += _evidence(agent)
        if os.path.isdir(home):
            why.append("config dir " + home.replace(HOME, "~"))
        if extra:
            why.append(extra)
        if why:
            t = targets.setdefault(os.path.normcase(path), Target(agent, FORMAT_NAMES[agent], path, []))
            t.why += [w for w in why if w not in t.why]

    # Claude Code: ~/.claude plus alternates like ~/.claude-glm, plus CLAUDE_CONFIG_DIRs of live sessions
    homes = {h for h in glob.glob(os.path.join(HOME, ".claude*")) if os.path.isdir(h)
             and (os.path.isdir(os.path.join(h, "projects")) or os.path.isfile(os.path.join(h, "settings.json")))}
    homes |= {h for h in running.get("claude-code", {}) if h}
    if shutil.which("claude"):
        homes.add(default_home("claude-code"))
    for h in sorted(homes):
        add("claude-code", h, FILENAMES["claude-code"])
    for agent in ("codex", "gemini", "copilot", "cursor", "windsurf"):
        for h in {default_home(agent)} | {h for h in running.get(agent, {}) if h}:
            add(agent, h, FILENAMES[agent])
    return sorted(targets.values(), key=lambda t: (t.agent, t.path))


def default_home(agent):
    return _norm({"claude-code": "~/.claude", "codex": "~/.codex", "gemini": "~/.gemini",
                  "copilot": "~/.copilot", "cursor": "~/.cursor",
                  "windsurf": "~/.codeium/windsurf"}[agent].replace("~", HOME))


def _norm(path):
    """One spelling per directory, so the same file isn't listed twice (on Windows
    "C:\\Users\\x/.claude" from a default and "C:\\Users\\x\\.claude" from glob)."""
    return os.path.normpath(path)


FORMAT_NAMES = {a.key: a.name for a in AGENTS}


# ------------------------------------------------------------------ editing
def _cmd_of(entry):
    if not isinstance(entry, dict):
        return ""
    return " ".join(str(entry.get(k) or "") for k in ("command", "bash", "powershell",
                                                       "commandWindows", "command_windows"))


def _is_stale(entry):
    cmd = _cmd_of(entry).replace("\\", "/")
    return EMIT.replace("\\", "/") not in cmd


def _strip(hooks, nested):
    """Remove our entries from a hooks mapping in place. Returns (removed, stale)."""
    removed = stale = 0
    for ev in list(hooks):
        groups = hooks[ev] if isinstance(hooks[ev], list) else []
        keep_groups = []
        for g in groups:
            if nested and isinstance(g, dict) and isinstance(g.get("hooks"), list):
                keep = [h for h in g["hooks"] if not OURS.search(_cmd_of(h))]
                gone = [h for h in g["hooks"] if OURS.search(_cmd_of(h))]
                removed += len(gone)
                stale += sum(1 for h in gone if _is_stale(h))
                if keep:
                    keep_groups.append(dict(g, hooks=keep))
            elif not nested and OURS.search(_cmd_of(g)):
                removed += 1
                stale += _is_stale(g)
            else:
                keep_groups.append(g)
        if keep_groups:
            hooks[ev] = keep_groups
        else:
            del hooks[ev]
    return removed, stale


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top level is not a JSON object")
    return data


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".cadre-bak")
    tmp = path + ".cadre-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def apply(target, install, dry_run=False):
    """Install or remove our hooks in one file. Returns a one-line result."""
    fmt = FORMATS[target.agent]
    try:
        data = _load(target.path)
    except (OSError, ValueError) as e:
        return "skipped: can't parse (%s); fix the file or edit it by hand" % e
    before = json.dumps(data, sort_keys=True)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    removed, stale = _strip(hooks, fmt.nested)
    if install:
        for ev in fmt.events:
            entry = fmt.win_entry(target.agent, ev) if IS_WIN and fmt.win_entry \
                else fmt.entry(command(target.agent, ev))
            hooks.setdefault(ev, []).append({"hooks": [entry]} if fmt.nested else entry)
        for k, v in fmt.top.items():
            data.setdefault(k, v)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)

    if json.dumps(data, sort_keys=True) == before:
        return "up to date" if install else "nothing to remove"
    # on uninstall, delete a file left with nothing in it but e.g. {"version": 1}
    delete = not install and set(data) <= set(fmt.top)
    if not dry_run:
        if delete:
            os.remove(target.path)
        else:
            _save(target.path, data)
    pre = "would " if dry_run else ""
    if install:
        msg = "%sinstall %d hooks" % (pre, len(fmt.events))
        if removed:
            msg += " (replacing %d existing cADRe hook(s)%s)" % (
                removed, ", %d with a stale path" % stale if stale else "")
        return msg
    return "%sremove %d hooks%s" % (pre, removed, " and delete the file" if delete else "")


# ------------------------------------------------------------------ coverage (dashboard)
def _entry(agent, ev):
    fmt = FORMATS[agent]
    return fmt.win_entry(agent, ev) if IS_WIN and fmt.win_entry else fmt.entry(command(agent, ev))


def check(target):
    """Hook state of one file, without changing it: {"state", "detail"}.

    ok          our hook on every event, current command line
    missing     no cADRe hook in the file (or no file)
    stale       a cADRe hook pointing at another copy (moved checkout, AgentTracker)
    outdated    ours, but some events are missing or the command changed (e.g. a new Python)
    unreadable  the file doesn't parse; the installer leaves it alone
    """
    fmt = FORMATS[target.agent]
    try:
        data = _load(target.path)
    except (OSError, ValueError) as e:
        return {"state": "unreadable", "detail": "can't parse: %s" % e}
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    ours = {}                                       # event -> our entries
    for ev, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            items = g["hooks"] if fmt.nested and isinstance(g, dict) and isinstance(g.get("hooks"), list) else [g]
            ours.setdefault(ev, []).extend(h for h in items if OURS.search(_cmd_of(h)))
    found = sum(len(v) for v in ours.values())
    if not found:
        return {"state": "missing", "detail": "no cADRe hook" if os.path.exists(target.path) else "no hook file yet"}
    if any(_is_stale(h) for v in ours.values() for h in v):
        return {"state": "stale", "detail": "points at an old copy of cADRe"}
    good = [ev for ev in fmt.events if ours.get(ev) == [_entry(target.agent, ev)]]
    extra = [ev for ev in ours if ours[ev] and ev not in fmt.events]
    if len(good) < len(fmt.events) or extra or any(k not in data for k in fmt.top):
        return {"state": "outdated", "detail": "%d of %d events up to date" % (len(good), len(fmt.events))}
    return {"state": "ok", "detail": "%d events" % len(fmt.events)}


_scan = {"at": 0.0, "data": None}


def scan(procs=None, max_age=0.0):
    """Every discovered hook file with its state, for the dashboard. Cached for `max_age` s."""
    if _scan["data"] is not None and time.time() - _scan["at"] < max_age:
        return _scan["data"]
    targets = []
    for t in discover(procs):
        st = check(t)
        targets.append({"agent": t.agent, "name": t.name, "path": t.path,
                        "display": t.path.replace(HOME, "~"), "why": t.why, **st})
    data = {"targets": targets, "emit": EMIT.replace(HOME, "~"),
            "pending": sum(1 for t in targets if t["state"] in FIXABLE),
            "unsupported": [{"agent": a.key, "name": a.name, "reason": NO_HOOKS[a.key]}
                            for a in AGENTS if a.key in NO_HOOKS]}
    _scan.update(at=time.time(), data=data)
    return data


FIXABLE = ("missing", "stale", "outdated")


def install_paths(paths=None, dry_run=False):
    """Install into these discovered hook files (None: every one that needs it). Only files
    discover() found are touched, whatever `paths` says, so the dashboard can't be used to
    write anywhere else. Returns [{"path", "name", "result"}] and notes for the user."""
    _scan["data"] = None
    if not dry_run and not os.path.isfile(EMIT):
        return {"results": [], "notes": ["emit.py not found at %s; run `cadre setup` again" % EMIT]}
    want = {os.path.normcase(os.path.normpath(p)) for p in paths} if paths is not None else None
    out, agents = [], set()
    for t in discover():
        key = os.path.normcase(os.path.normpath(t.path))
        if want is not None and key not in want:
            continue
        if want is None and check(t)["state"] not in FIXABLE:
            continue
        res = apply(t, True, dry_run)
        out.append({"path": t.path.replace(HOME, "~"), "agent": t.agent, "name": t.name, "result": res})
        if not res.startswith(("skipped", "up to date")):
            agents.add(t.agent)
    notes = []
    if "codex" in agents and not dry_run:
        notes.append("Codex: new hooks stay inactive until you trust them with /hooks.")
    if agents - {"claude-code"} and not dry_run:
        notes.append("Restart agents that were already running to start their hooks"
                     " (Claude Code picks them up by itself).")
    return {"results": out, "notes": notes}


# ------------------------------------------------------------------ CLI
def run(install, dry_run=False, agents=None):
    targets = [t for t in discover() if not agents or t.agent in agents]
    if install and not dry_run and not os.path.isfile(EMIT):
        print("emit.py not found at %s" % EMIT)
        return 1
    print("%s cADRe hooks -> %s%s\n" % ("Installing" if install else "Removing",
                                        EMIT.replace(HOME, "~"), "  [dry run]" if dry_run else ""))
    if not targets:
        print("No hook-capable agents found.")
    for t in targets:
        print("%-20s %s" % (t.name, t.path.replace(HOME, "~")))
        print("%-20s   found: %s" % ("", "; ".join(t.why)))
        print("%-20s   %s" % ("", apply(t, install, dry_run)))
    found = {t.agent for t in targets}
    print()
    for a in AGENTS:
        if a.key in NO_HOOKS and (not agents or a.key in agents):
            print("%-20s %s" % (a.name, NO_HOOKS[a.key]))
    missing = [FORMAT_NAMES[k] for k in FORMATS if k not in found and (not agents or k in agents)]
    if missing:
        print("%-20s %s" % ("Not found", ", ".join(missing)))
    if install and not dry_run and "codex" in found:
        print("\nCodex: new hooks stay inactive until you review and trust them with /hooks.")
    print("\nNew sessions pick the hooks up; restart agents that were already running.")
    return 0
