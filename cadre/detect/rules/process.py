"""The agent process itself: how it was started and how it behaves (moved here from
collectors.risk_flags). These look at a live agent record (see collectors.snapshot), not events.

  permissions-bypassed  started with approvals/sandbox turned off
  high-cpu, many-children
  home-folder, root-folder  ("info": a risky place to run, not something the agent did)
  zcode-destructive     ZCode's own "destructive" mark on recent tool calls
"""
import re

from ..engine import record_rule
from ..helpers import HOME, norm

BYPASS_ARGS = re.compile(r"--dangerously-skip-permissions|--dangerously-bypass-approvals-and-sandbox|(^|\s)--yolo\b")
BYPASS_MODES = ("bypassPermissions", "danger-full-access")       # Claude Code, Codex without sandbox
CPU_PCT = 80
CHILDREN = 15


@record_rule("permissions-bypassed", "process", "warn", "permissions bypassed",
             hits=[{"args": "claude --dangerously-skip-permissions"}, {"permission_mode": "danger-full-access"},
                   {"args": "gemini --yolo"}],
             misses=[{"args": "claude", "permission_mode": "default"}, {"args": "foo --yolonger"}])
def permissions_bypassed(rec):
    """Approval prompts or the sandbox switched off by a flag or the session's permission mode."""
    return bool(BYPASS_ARGS.search(rec.get("args") or "")) or rec.get("permission_mode") in BYPASS_MODES


@record_rule("high-cpu", "process", "warn", "high CPU",
             hits=[{"cpu": 95.0}], misses=[{"cpu": 30.0}, {}])
def high_cpu(rec):
    return (rec.get("cpu") or 0) > CPU_PCT


@record_rule("many-children", "process", "warn", "many child processes",
             hits=[{"child_count": 40}], misses=[{"child_count": 3}, {}])
def many_children(rec):
    return (rec.get("child_count") or 0) > CHILDREN


def _cli_cwd(rec):
    return rec.get("cwd") if rec.get("kind") == "cli" else None


@record_rule("home-folder", "process", "info", "running in your home folder",
             hits=[{"kind": "cli", "cwd": HOME}], misses=[{"kind": "cli", "cwd": HOME + "/proj"},
                                                          {"kind": "desktop", "cwd": HOME}])
def home_folder(rec):
    """A CLI agent whose project is the whole home folder."""
    cwd = _cli_cwd(rec)
    return cwd if cwd and norm(cwd) == norm(HOME) else None


@record_rule("root-folder", "process", "info", "running at the filesystem root",
             hits=[{"kind": "cli", "cwd": "/"}, {"kind": "cli", "cwd": "C:\\"}],
             misses=[{"kind": "cli", "cwd": "/w/proj"}, {"kind": "desktop", "cwd": "/"}])
def root_folder(rec):
    cwd = _cli_cwd(rec)
    return cwd if cwd and (cwd == "/" or re.fullmatch(r"[A-Za-z]:[\\/]?", cwd)) else None


@record_rule("zcode-destructive", "process", "warn", "destructive tool calls",
             hits=[{"destructive_recent": 2}], misses=[{"destructive_recent": 0}, {}])
def zcode_destructive(rec):
    """ZCode marks tool calls as destructive in its own DB (see zcode.read_sessions)."""
    n = rec.get("destructive_recent") or 0
    return "%d in the last hour" % n if n else None
