"""Secrets in what an agent typed or ran: API keys, tokens, private keys, passwords.

Hooks never write a secret to the log: emit.py replaces it with "[redacted:<kind>]" first
(its SECRETS table is this one; tests/test_detect.py checks they match). This rule flags those
markers, and also matches raw secrets in events that didn't pass through emit.py (ZCode's DB,
events POSTed by older hooks).

To add a secret kind: add a row here AND in cadre/hooks/emit.py (it can't import this package).
"""
import re

from ..engine import event_rule

SECRETS = [
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|[\s\S]*)"),
    ("aws-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("api-key", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("google-key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("password", r"(?i)(?:(?<![\w:.])(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[=:]\s*"
                 r"|--(?:password|token|api-key|secret)[= ])['\"]?(?P<v>(?![$%<{\[|(])[^\s'\"|`,;)]{8,})"),
]
_RAW = [(k, re.compile(p)) for k, p in SECRETS]
MARKER = re.compile(r"\[redacted:([\w-]+)\]")
FIELDS = ("command", "detail", "path")


@event_rule("secret", "secrets", "high", "secret in a command or prompt",
            hits=[{"tool": "Bash", "command": "curl -H 'x-api-key: [redacted:api-key]' https://api"},
                  {"event": "UserPromptSubmit", "detail": "use key AKIAABCDEFGHIJKLMNOP please"},
                  {"tool": "Bash", "command": "mysql --password=[redacted:password] db"}],
            misses=[{"tool": "Bash", "command": "export TOKEN=$GITHUB_TOKEN"},
                    {"event": "UserPromptSubmit", "detail": "rotate the api key please"},
                    {"tool": "Bash", "command": "echo password: short"},
                    {"event": "UserPromptSubmit", "detail": "the selector username:password:reply: is deprecated"},
                    {"tool": "Bash", "command": "strings x | grep -E 'password=|token=|plain_login'"}])
def secret(ev):
    """A redaction marker from emit.py, or a raw secret pattern, in the command/detail/path."""
    kinds = []
    for f in FIELDS:
        s = ev.get(f)
        if not isinstance(s, str) or not s:
            continue
        kinds += MARKER.findall(s)
        kinds += [k for k, rx in _RAW if rx.search(s)]
    return ", ".join(sorted(set(kinds))) or None

