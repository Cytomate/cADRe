# cADRe (Agentic Detection & Response): Plan

## Goal
One place that answers, for every AI agent on your machines:
*What agents exist? Which are running? In which project? Doing what? Are any misbehaving?*

## Architecture (target)

```
 ┌──────────── per machine ────────────┐        ┌──────── central (optional) ───────┐
 │ Collectors                          │        │                                   │
 │  • process  (ps / lsof / psutil)    │        │  Ingest API  ──►  Store            │
 │  • install  (PATH, apps, configs)   │ events │  (HTTP/WS)       (SQLite→Postgres) │
 │  • sessions (transcripts per agent) ├───────►│                     │             │
 │  • hooks    (agent pushes events)   │        │  Rules / anomaly engine ◄──┘       │
 │  • fs/net   (files touched, egress) │        │       │                            │
 │        ▼                            │        │       ▼                            │
 │ Normaliser -> AgentRecord           │        │  Dashboard + alerts (Slack/push)   │
 └─────────────────────────────────────┘        └───────────────────────────────────┘
```

Core data model: `Agent` (type, install evidence) → `Instance` (pid, host, cwd, status,
uptime) → `Session` (id, project, prompts, tool calls, model, permission mode) → `Event`
(timestamped tool call / file write / command / network call).

## Phases

### Phase 0: Prototype (done)
- Local Python stdlib collector + dashboard, polling.
- Detects ~11 agent types; deep session parsing for Claude Code.
- Statuses: working / idle / suspended / sleeping / installed.
- Simple risk flags.

### Phase 1: Better local tracker

Status 2026-11-08:
- [x] Push via hooks: Claude Code, plus an installer covering Codex, Gemini CLI, Copilot CLI,
  Cursor and Windsurf (`--install-hooks`).
- [x] ZCode session reader (SQLite) incl. live status and feed events.
- [x] Cross-platform process layer (psutil / ps / /proc / PowerShell); verified on Windows, not Linux.
- [x] Background service instructions (launchd / systemd / Task Scheduler) in `docs/RUNBOOK.md`.
- [x] One-step install: `cadre setup` / `uninstall` (install folder, hooks, autostart, start);
  hooks write the event log so the dashboard is optional; installable web app.
- [x] Session parser for Codex (`cadre/codex.py`).
- [ ] Session parsers for Gemini, Aider, Cursor/Windsurf, Copilot.
- [ ] SQLite persistence and history; subagent tracking; token/cost usage.
- [x] macOS menu bar icon (`cadre/menubar.js`), starts at login.
- [x] Windows tray icon (`cadre/tray.py`), starts at login.
- [ ] Statusline view.
- [x] Test suite started (`python3 -m unittest`, `tests/`).
- [x] Windows release (2026-11-09): per-user installer with its own Python
  (`scripts/build_release.py`, `packaging/windows/`); installed for real on Windows.
- [x] Windows event log race (2026-11-10): concurrent hooks could overwrite each other
  (`O_APPEND` isn't atomic there); writers now take a short, capped lock on
  `events.jsonl.lock` (`tests/test_log_race.py`).

### Releases and platforms

Decided 2026-11-09:

| | Windows | macOS |
|---|---|---|
| Install | release installer `cADRe-<v>-windows-x64-setup.exe` (or `py -m cadre setup` from a checkout) | `python3 -m cadre setup` from a checkout; no release package |
| Python | bundled embeddable Python + psutil in `%LOCALAPPDATA%\cADRe\python\` | `/usr/bin/python3` (Command Line Tools); falls back to the Python running `setup`, with a note, when the Tools are missing |
| Signing | unsigned for now (SmartScreen "unknown publisher") | none needed without a package |

**Why Windows bundles Python**: hook commands used to contain `sys.executable`, which moves on
every Python upgrade (Store builds on every update), Windows has no Python by default, and
psutil had to be installed by hand. Why not PyInstaller: a frozen exe can't run `emit.py`,
unpacks to a temp dir on every hook call, and trips antivirus.

**Why macOS stays on `setup`**: hooks use `/usr/bin/python3`, a fixed path that survives
upgrades, every Mac running coding agents has the Command Line Tools (git needs them), and a
Mac release mostly means Developer ID signing and notarization of every bundled binary. A Mac
release, if wanted later, is packaging only: a `.dmg` with `cADRe.app` that puts
python-build-standalone (arm64 + x86_64) in `~/Library/Application Support/cADRe/python/`
(`paths.bundled_python()` already looks for `bin/python3` there).

Original Phase 1 list:
- **Push instead of poll for Claude Code**: register hooks (`PreToolUse`, `PostToolUse`,
  `UserPromptSubmit`, `Stop`, `SessionStart/End`, `Notification`) that POST to the
  tracker. Gives exact, real-time tool events and "waiting for permission" state.
- Session parsers for Codex (`~/.codex/sessions`), Gemini CLI (`~/.gemini`), Aider
  (`.aider.chat.history.md`), Cursor/Windsurf (workspace storage).
- Persist snapshots/events in SQLite → timeline per agent and per project, history view.
- Subagent tracking (Claude Code sidechains and subagent transcripts).
- Token/cost usage per session (from transcript `usage` fields).
- Swap `ps`/`lsof` for `psutil` to support Linux and Windows.
- Run as a background service (launchd / systemd) with a menu-bar or `/statusline` view.

### Phase 2: Multi-machine / team
- Lightweight agent daemon on each host pushes events to a central API.
- Auth (per-host tokens), TLS, Postgres, WebSocket live updates.
- Cloud and remote agents: Claude Code web/cloud sessions, CI agents, containers
  (`docker ps` / Kubernetes labels), Managed Agents via their APIs.
- Team dashboard: filter by person, project, repo, agent type.

### Phase 3: Rogue-agent detection

Status 2026-11-10: the first slice (explainable, watch-only rules) is built in `cadre/detect/`
(25 rules: commands, scope, secrets, runaway, process; `python3 -m cadre --rules`). Still to do:
network egress, drift from task (LLM judge), baselines per agent, alert → pause → kill.

Response, status 2026-11-10 (`cadre/respond/`, `docs/architecture/10-response.md`):
1. Alerts for new findings (tray notification, dashboard toast and risk banner, opt-in browser
   notification): built.
2. Pause / Resume / Kill by hand from the dashboard, with a response log: built.
3. A policy file (rule or severity → action, everything off by default) for automatic
   pause / kill on a few `high` rules: next.
4. A guard hook, separate from `emit.py`, on `PreToolUse`-type events for Claude Code: `ask`
   first (the agent's own permission prompt), then `block`. Must fail open.
5. The guard for other agents, each verified live.

Start with explainable rules, then add baselines:

| Signal | Example rule |
|---|---|
| Scope escape | writes outside the session's project folder; touches `~/.ssh`, `~/.aws`, keychains, `.env` |
| Dangerous commands | `rm -rf`, `git push --force`, `curl … \| sh`, `chmod 777`, disabling security tools |
| Privilege | permission mode `bypassPermissions`, `sudo`, new launch agents/cron entries |
| Network egress | connections to hosts not on an allowlist; large uploads |
| Runaway behaviour | tool-call rate spikes, loops of the same failing command, hours of CPU, many children |
| Drift from task | actions unrelated to the prompt (LLM-judge over prompt + recent tool calls) |
| Secrets | secrets appearing in commands, files written, or outbound requests |

Responses, step by step: **alert** → **pause** (SIGSTOP / deny via `PreToolUse` hook)
→ **kill** (SIGTERM) with an audit log. Hooks let the tracker block a tool call
*before* it runs, which is the strongest control.

Data sources needed: hooks (tool calls), `fs_usage`/FSEvents or auditd (file access),
`nettop`/`lsof -i` or a local proxy (network), EndpointSecurity on macOS for full
coverage.

## Risks & constraints
- Transcript formats are undocumented and change between versions → isolate parsers, version-test them.
- Privacy: transcripts contain code and secrets → keep local by default, redact before sending anywhere.
- Process detection by name can mis-match → prefer hooks and exact binary paths.
- macOS restricts `lsof`/EndpointSecurity for other users' processes → deeper monitoring needs entitlements or root.
