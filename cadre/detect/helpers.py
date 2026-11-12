"""Helpers shared by the rules: what command or file an event is about, path tests, and
`command_patterns()`, which registers one event rule per row of a regex table."""
import os
import re

from .engine import event_rule

# tools whose input is a shell command, across agents (Claude Code Bash, Codex exec_command,
# Gemini run_shell_command, Windsurf run_command, ZCode bash, ...)
SHELL_TOOLS = re.compile(r"(?i)^(bash|shell|sh|zsh|powershell|pwsh|cmd|terminal|exec|exec_command|"
                         r"local_shell|local_shell_call|run_shell_command|run_command|run_terminal_cmd|command)$")
# tools that change files
WRITE_TOOLS = re.compile(r"(?i)(write|edit|replace|patch|create|delete|remove|move|rename|notebook)")
HOME = os.path.expanduser("~")


def is_shell(ev):
    return bool(ev.get("command")) or bool(SHELL_TOOLS.match(ev.get("tool") or ""))


def command_of(ev):
    """The shell command of a tool event: the hook's `command` field, or for older events and
    ZCode (which only have a summary) the detail of a shell tool. "" if it isn't a shell call."""
    if ev.get("command"):
        return ev["command"]
    if SHELL_TOOLS.match(ev.get("tool") or "") and ev.get("detail"):
        return ev["detail"]
    return ""


HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?(?:^[ \t]*\2[ \t]*$|\Z)", re.S | re.M)


def strip_heredocs(cmd):
    """The command without heredoc bodies: `cat > f <<'EOF' ... EOF` writes text, it doesn't run it."""
    return HEREDOC.sub(lambda m: "<<%s …" % m.group(2), cmd) if "<<" in cmd else cmd


def path_of(ev):
    """The file a tool event touched ("" if none)."""
    return ev.get("path") or ""


def is_write(ev):
    return bool(path_of(ev)) and bool(WRITE_TOOLS.search(ev.get("tool") or ""))


def norm(p):
    return os.path.normcase(os.path.normpath(os.path.expanduser(p))).replace("\\", "/")


def under(path, folder):
    """path is folder or inside it (case-insensitive on Windows, ~ expanded)."""
    if not path or not folder:
        return False
    p, f = norm(path), norm(folder).rstrip("/")
    return p == f or p.startswith(f + "/")


def is_absolute(p):
    return bool(p) and (p.startswith(("/", "~", "\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", p)))


TEMP_DIRS = [d for d in {"/tmp", "/private/tmp", "/var/folders", "/private/var/folders", "/dev/null",
                         os.environ.get("TMPDIR"), os.environ.get("TEMP"), os.environ.get("TMP")} if d]


def is_temp(p):
    return any(under(p, d) for d in TEMP_DIRS)


def command_patterns(category, table):
    """Register one event rule per row: (id, severity, title, regex, hits, misses).
    A row hits when its regex matches the event's shell command; hits/misses are plain
    command strings (turned into Bash tool events for the tests)."""
    for rid, severity, title, regex, hits, misses in table:
        rx = re.compile(regex)

        def fn(ev, rx=rx):
            cmd = strip_heredocs(command_of(ev))
            m = rx.search(cmd) if cmd else None
            return excerpt(cmd, m) if m else None
        fn.__doc__ = "Shell command matching /%s/." % regex
        as_ev = lambda c: {"tool": "Bash", "command": c}
        event_rule(rid, category, severity, title, [as_ev(c) for c in hits], [as_ev(c) for c in misses])(fn)


def excerpt(cmd, m, width=80):
    """The matched part of a command with a little context, for the finding's detail."""
    a, b = max(0, m.start() - 15), min(len(cmd), m.end() + 25)
    s = ("…" if a else "") + " ".join(cmd[a:b].split()) + ("…" if b < len(cmd) else "")
    return s if len(s) <= width else s[: width - 1] + "…"
