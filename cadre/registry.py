"""Catalogue of known AI agents and how to recognise them on this machine.

Add a new agent by appending an AgentSpec. Detection is purely local:
process names / argv, binaries on PATH, app bundles / install dirs and config directories.
Patterns cover macOS (.app bundles), Linux and Windows (.exe, %LOCALAPPDATA% installs);
paths may use ~ and %VAR% / $VAR, expanded per platform.
"""
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

HOME = os.path.expanduser("~")


@dataclass
class AgentSpec:
    key: str                       # stable id, e.g. "claude-code"
    name: str                      # display name
    kind: str                      # "cli" | "desktop" | "ide" | "runtime"
    # Regex matched against the executable path (ps "comm").
    exe_pattern: Optional[str] = None
    # Regex matched against full argv (for node/python launched CLIs).
    args_pattern: Optional[str] = None
    binaries: List[str] = field(default_factory=list)      # looked up on PATH
    apps: List[str] = field(default_factory=list)          # .app bundles / install dirs
    config_dirs: List[str] = field(default_factory=list)   # evidence of install
    session_source: Optional[str] = None                   # deep-integration reader
    # Regex on full argv: processes of this agent that aren't an agent session (background daemons)
    exclude_args: Optional[str] = None


def _cli(word):
    # "word" as an argv token, e.g. "node /usr/local/bin/codex" or
    # 'node C:\\...\\npm\\node_modules\\@openai\\codex\\bin\\codex.js'
    return r"(^|[/\\\s\"])%s(\.exe|\.cmd|\.js|\.mjs|\.ps1)?([\s\"]|$)" % re.escape(word)


def _exe(word):
    # ".../word" on macOS/Linux, "...\\word.exe" (any case) on Windows
    return r"(^|/)%s$|(?i:(^|[\\/])%s\.exe$)" % (re.escape(word), re.escape(word))


LOCAL = "%LOCALAPPDATA%/Programs/"


AGENTS = [
    AgentSpec(
        key="claude-code", name="Claude Code", kind="cli",
        # Windows: claude.exe, but not Claude Desktop's (under AnthropicClaude, or the MSIX
        # package in WindowsApps\Claude_<version>_x64__<id>\app)
        exe_pattern=r"(^|/)claude$|(?i:^(?!.*(anthropicclaude|windowsapps[\\/]claude_))(.*[\\/])?claude\.exe$)",
        args_pattern=r"@anthropic-ai[\\/]claude-code[\\/]",
        binaries=["claude"],
        config_dirs=["~/.claude"],
        session_source="claude",
    ),
    AgentSpec(
        key="claude-desktop", name="Claude Desktop", kind="desktop",
        exe_pattern=r"/Claude\.app/Contents/MacOS/Claude$"
                    r"|(?i:(anthropicclaude|windowsapps[\\/]claude_[^\\/]*)[\\/].*claude\.exe$)",
        # WindowsApps can't be listed by users, so the MSIX build is found by its package data dir
        apps=["/Applications/Claude.app", "%LOCALAPPDATA%/AnthropicClaude",
              "%LOCALAPPDATA%/Packages/Claude_pzs8sxrjxfjjc"],
    ),
    AgentSpec(
        key="codex", name="OpenAI Codex CLI", kind="cli",
        exe_pattern=_exe("codex"), args_pattern=_cli("codex"),
        # Windows installer: %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe (not on PATH)
        binaries=["codex"], apps=[LOCAL + "OpenAI/Codex"], config_dirs=["~/.codex"],
        session_source="codex",
        # the TUI's background app-server daemon outlives the window that started it
        # (~/.codex/packages/app-server-daemon/...); VS Code's own app-server stays an agent
        exclude_args=r"(?i)[\\/]app-server-daemon[\\/]",
    ),
    AgentSpec(
        key="gemini", name="Gemini CLI", kind="cli",
        exe_pattern=_exe("gemini"), args_pattern=_cli("gemini"),
        binaries=["gemini"], config_dirs=["~/.gemini"],
    ),
    AgentSpec(
        key="aider", name="Aider", kind="cli",
        exe_pattern=_exe("aider"), args_pattern=_cli("aider"),
        binaries=["aider"],
    ),
    AgentSpec(
        key="copilot", name="GitHub Copilot CLI", kind="cli",
        exe_pattern=_exe("copilot"), args_pattern=_cli("copilot"),
        binaries=["copilot"], config_dirs=["~/.copilot"],
    ),
    AgentSpec(
        key="cursor", name="Cursor", kind="ide",
        exe_pattern=r"/Cursor\.app/Contents/MacOS/Cursor$|(?i:[\\/]cursor\.exe$)|" + _exe("cursor-agent"),
        binaries=["cursor-agent"], apps=["/Applications/Cursor.app", LOCAL + "cursor"],
        config_dirs=["~/.cursor"],
    ),
    AgentSpec(
        key="windsurf", name="Windsurf", kind="ide",
        exe_pattern=r"/Windsurf\.app/Contents/MacOS/|(?i:[\\/]windsurf\.exe$)",
        apps=["/Applications/Windsurf.app", LOCAL + "Windsurf"], config_dirs=["~/.codeium/windsurf"],
    ),
    AgentSpec(
        key="zcode", name="ZCode", kind="ide",
        exe_pattern=r"(^|/ZCode\.app/Contents/MacOS/)ZCode$|(?i:[\\/]zcode\.exe$)",
        apps=["/Applications/ZCode.app", LOCAL + "ZCode"], config_dirs=["~/.zcode"],
    ),
    AgentSpec(
        key="goose", name="Goose", kind="cli",
        exe_pattern=_exe("goose"), binaries=["goose"],
        config_dirs=["~/.config/goose"],
    ),
    AgentSpec(
        key="opencode", name="opencode", kind="cli",
        exe_pattern=_exe("opencode"), args_pattern=_cli("opencode"),
        binaries=["opencode"],
    ),
    AgentSpec(
        key="ollama", name="Ollama (local models)", kind="runtime",
        exe_pattern=_exe("ollama") + r"|(?i:[\\/]ollama app\.exe$)", binaries=["ollama"],
        apps=["/Applications/Ollama.app", LOCAL + "Ollama"], config_dirs=["~/.ollama"],
    ),
]


def expand(p):
    """Expand ~ and %VAR% / $VAR. An unset variable leaves the path unusable (never matches)."""
    out = os.path.expanduser(os.path.expandvars(p))
    return os.path.normpath(out) if "%" not in out and "$" not in out else "\0unset"
