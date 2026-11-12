# cadre/respond: alerts and manual actions

What cADRe does about a finding from `cadre/detect`. **Nothing here acts on its own**: alerts
only tell, and Pause / Resume / Kill run when the user clicks them in the dashboard.
Diagrams and limits: `docs/architecture/10-response.md`.

```bash
python3 -m cadre --responses                # the response log
python3 -m unittest tests.test_respond -v
```

| File | What it is for |
|---|---|
| `__init__.py` | The public API: `ALERTS`, `act()`, `paused_by_us()`, `needs_fg()`, `audit` |
| `alerts.py` | `AlertLog.update(records, now)`: which findings are new. Pure, like the detection rules |
| `actions.py` | `act(snap, agent_id, action)`: checks the process is still that agent, then acts on it and its children |
| `audit.py` | `~/.cadre/responses.jsonl`: every action, worked or not |
| `dismiss.py` | False positives: `mark()` sets or takes back a mark (`~/.cadre/dismissed.json`) and logs it; `apply_record()` / `apply_event()` hide what is marked |

The calls that touch a process are `suspend`, `resume` and `terminate` at the end of
`cadre/procs.py` (next to `alive`, `foreground`, `terminal_parents` and `sees_stopped`, which only ask). Only
`actions.py` calls them.

## Rules for this folder

* `act()` takes an agent id from the snapshot, never a pid from a request, and reads the
  process list again before it does anything.
* cADRe's own process and its children are never in the tree it acts on.
* Kill needs `confirm=True`; the dashboard asks the user first.
* An agent that is a terminal's foreground job (macOS, Linux) is paused together with its
  ancestors on that terminal (its shell), or the shell takes the keyboard and Resume can't
  give it back. Those are stopped first and continued last, never ended, and logged as `held`.
  `release_orphans()` continues them if the agent ends some other way. Only when the shell
  can't be held does Resume say "type fg" (`needs_fg`).
* Every call that gets as far as the process check is logged, failures included.
* A false positive hides a finding, it never changes a rule or a stored event; an event rule
  is marked by rule + detail + session, any other rule by rule + session.
* A new action goes in `ACTIONS`, `_do()` and the tests; a new way of telling (Slack, a push)
  reads `snapshot["alerts"]` like the tray icon does.
