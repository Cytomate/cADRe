# cadre/detect: detection rules

Rules that flag risky agent behaviour. **Watch-only**: a finding is shown, nothing is blocked
(blocking via "before" hooks is a later, opt-in phase; see `docs/PLAN.md`, Phase 3).
What happens with a finding (alerts, Pause / Resume / Kill by hand) is `cadre/respond`.

```bash
python3 -m cadre --rules                    # every rule: id, severity, kind, title, file
python3 -m unittest tests.test_detect -v    # runs every rule's own examples
```

## Files

| File | What it is for |
|---|---|
| `__init__.py` | The public API (`check_*`, `review_*`, `RULES`); importing it registers every rule |
| `engine.py` | `Rule`, the registry, the decorators, `check_event` / `check_session` / `check_record` |
| `helpers.py` | Shared by rules: command and path of an event, heredoc stripping, path tests, `command_patterns()` |
| `review.py` | The glue the rest of cADRe calls: findings on events (feed, explorer) and on agent records (cards, office, tray) |
| `rules/commands.py` | Dangerous shell commands, as a regex table |
| `rules/scope.py` | Credentials and keys, `.env` files, writes outside the project |
| `rules/secrets.py` | Keys, tokens and passwords in commands or prompts (the same table as `hooks/emit.py`) |
| `rules/runaway.py` | Loops and bursts across a session's events |
| `rules/process.py` | How the agent process runs: permissions bypassed, CPU, children, folder |

`engine.py`, `helpers.py` and `rules/` import nothing else from cADRe (a test checks this),
so rules are pure functions of their input. Findings are worked out when events are read,
never stored, so a changed rule also applies to old events.

## How findings flow

```
hook ─► emit.py ─► events.jsonl ─► events.EventStore / events.history ─► review_event(ev)
          │  records command + path,                                     ev["findings"]
          │  redacts secrets ([redacted:kind])                           feed row mark, explorer "Flagged"
          ▼
collectors.snapshot ─► review_record(rec, that session's events) ─► rec["findings"], rec["flags"]
                                                                    card, office police droid,
                                                                    Risk flags total, tray, menu bar
```

A record gets: its record rules (only while it has a process), the findings of its session's
events (repeats merged, `×N`), and the session rules. A running agent keeps an event's finding
for as long as it runs (until the user marks it as a false positive or the agent ends); a
session without a process keeps it for an hour, and session rules look at the last hour.

## Kinds and severities

| Kind | Decorator | Looks at | Example |
|---|---|---|---|
| event | `@event_rule` | one hook event (`tool`, `command`, `path`, `detail`, `cwd`, `failed`) | `rm -rf` |
| session | `@session_rule` | one session's recent events, oldest first | the same call failing 3 times |
| record | `@record_rule` | a live agent record (`args`, `cpu`, `cwd`, `permission_mode`) | permissions bypassed |

`info` = a risky setup, shown on the agent only. `warn` and `high` = the tracker goes after it
(police droid, Risk flags tile, red mark in the feed). High sorts first.

## Adding a rule

1. Pick the category file in `rules/` (or add one: a new module, imported in `rules/__init__.py`).
2. Write it, with examples it must flag (`hits`) and must not flag (`misses`):

```python
from ..engine import event_rule
from ..helpers import command_of

@event_rule("npm-publish", "commands", "warn", "publishes a package",
            hits=[{"tool": "Bash", "command": "npm publish --access public"}],
            misses=[{"tool": "Bash", "command": "npm pack"}])
def npm_publish(ev):
    """npm publish from an agent session."""
    return "npm publish" in command_of(ev)       # True, a short detail string, or a list of them
```

   A shell-command pattern is simpler still: one row in `rules/commands.py`'s `TABLE`
   `(id, severity, title, regex, hits, misses)`, with hits and misses as plain commands.
3. Run `python3 -m unittest tests.test_detect`: it checks the id, the severity, that the
   category matches the file, and every hit and miss.

Rules of thumb: anchor commands on the program in command position (see `_CMD` in
`commands.py`) so prose and `echo 'rm -rf'` don't match; `helpers.strip_heredocs()` drops text
being written to a file; no `(?i)` in the middle of a pattern (an error from Python 3.11); a
rule that raises is skipped, never fatal, but fix it. Before committing a new rule, run it over
real data: Claude Code transcripts hold the actual commands (`message.content[].type ==
"tool_use"`, `input.command` / `input.file_path`); heredoc bodies and prose in commands are
the main false-positive source.

A new secret kind goes in **both** `rules/secrets.py` and `cadre/hooks/emit.py` (emit.py runs
with `python -I -S` and can't import this package); a test checks the two tables match.
