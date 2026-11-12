# cADRe runbook

Every command needed to set up cADRe, run the dashboard, wire agent hooks, keep it running,
and take it all back out. macOS, Linux and Windows.

Commands are run from the repo root (`cd cADRe`). On Windows, use `py` (the Python
launcher) where macOS/Linux use `python3`.

---

## 1. Requirements

| | macOS | Linux | Windows |
|---|---|---|---|
| Python | 3.9+ (`/usr/bin/python3` from Xcode CLT is fine) | 3.9+ | 3.9+ from python.org (tick "Add to PATH"); avoid the Microsoft Store build, whose path changes on update |
| Extra packages | none | none | `psutil`: the release brings it, `setup` fetches it |
| Tools used | `ps`, `lsof` (built in) | `ps`, `/proc` | PowerShell (built in) |

Without `psutil` on Windows, cADRe still lists agents, CPU and memory (via PowerShell CIM),
but can't read a process's working folder or environment. So project folders of live CLI
agents come from their transcripts, and `CLAUDE_CONFIG_DIR`-style config dirs of running
agents are not discovered. The dashboard then shows a "Limited view: no psutil" chip in its
header. So on Windows psutil is part of every install: the release has it in its own Python,
and `py -m cadre setup` fetches it from PyPI into `%LOCALAPPDATA%\cADRe\site` when this
Python has none (your Python's own packages are left alone; `--no-psutil` skips it; offline
or without pip, setup says so and carries on). A checkout run with `py -m cadre` uses that
copy too. A `psutil` installed in the Python itself (`py -m pip install psutil`) is used
first, on any OS.

Check which process backend is in use:

```bash
python3 -c "from cadre.procs import backend; print(backend())"   # psutil | ps | proc | windows-cim
```

## 2. Get the code

```bash
git clone <this repository's URL> cADRe
cd cADRe
```

Nothing to build: it's a standard-library Python package run with `-m cadre`.

## 3. Install for this user (recommended)

One command sets everything up; no admin rights needed:

```bash
python3 -m cadre setup --dry-run     # show what it would do (Windows: py -m cadre setup --dry-run)
python3 -m cadre setup               # do it
```

It:
1. copies cADRe to an install folder (Windows `%LOCALAPPDATA%\cADRe`, macOS
   `~/Library/Application Support/cADRe`, Linux `~/.local/share/cadre`), so moving or
   re-cloning this checkout no longer breaks anything;
2. installs the hooks (section 5) for every agent found, pointing at that copy;
3. starts the dashboard at login: Windows `HKCU\...\CurrentVersion\Run\cADRe` (runs
   `pythonw.exe`, no window), macOS LaunchAgent `com.cadre.dashboard`, Linux systemd user
   service `cadre`;
4. starts it now and opens http://127.0.0.1:8765. In Edge or Chrome, "Install app" in the
   address bar turns it into an app with its own window and taskbar/dock icon.

**Re-run `setup`** after `git pull` (to update the installed copy) and after upgrading Python
(the hooks contain the interpreter's path). Options: `--no-autostart`, `--no-start`,
`--no-browser`, `--no-psutil` (Windows), `--agents claude-code,codex`. `CADRE_PORT=9000` installs on another port.
Codex asks you to trust the hooks again in `/hooks`, because their command changed.

If a dashboard you started by hand already uses the port, setup says so and doesn't start a
second one: stop yours and re-run setup (or just log out and in).

```bash
python3 -m cadre uninstall --dry-run
python3 -m cadre uninstall           # hooks, autostart and install folder; keeps ~/.cadre
python3 -m cadre uninstall --purge   # also delete ~/.cadre (the event log)
```

Sections 4 to 6 are the same steps done by hand.

## 4. Run

```bash
python3 -m cadre                    # dashboard -> http://127.0.0.1:8765 (Ctrl+C to stop)
python3 -m cadre --port 9000        # another port
python3 -m cadre --once             # one-shot table in the terminal
python3 -m cadre --json             # raw snapshot, for scripts
python3 -m cadre --rules            # the detection rules (id, severity, file)
python3 -m cadre --responses        # the response log: every Pause / Resume / Kill
```

**Detection** (watch-only): risky tool calls get a red mark in the live feed (hover lists the
rules that fired), the agent's card and office tag show the flag, and the police droid goes to
agents with a `warn` or `high` finding. The Live activity panel has an **Alerts** tab with only
flagged events (rule title and the matched command; its count is the last hour's alerts).
Click the **Risk flags** tile to open the explorer on "Flagged by a rule". Rules and how to add one: `cadre/detect/README.md`. Hooks installed
before 2026-11-10 don't record the command; re-run `setup` so shell rules can see it.

**Response** (by hand): when a rule newly flags an agent, the tray icon shows a Windows
notification (the macOS menu bar icon a Notification Centre banner, listed under Script Editor;
the Linux status icon a desktop notification), the dashboard a toast, and a red banner row names each running agent for as long
as its flag stands: until you mark it as a false positive or the agent ends (a session that has ended keeps its flag for an hour; an agent you paused keeps a row until you resume or kill it, flag or not). The bell in the header switches on desktop notifications from the browser (for
machines without the tray icon). Click an agent in the office to pin its card: **Pause agent**
suspends its process and child processes, **Resume agent** continues them, **Kill agent**
ends them after a confirmation. The same buttons are on the banner. Nothing is paused or
killed on cADRe's own decision. A flagged call has already run when you see it: pausing stops
what comes next.
On macOS and Linux a paused agent's terminal stands still (cADRe holds its shell too, or the
shell would take the keyboard and the agent would look killed); **Resume agent** continues it
where it was, and keys typed meanwhile reach it then. If cADRe can't hold the shell, the
terminal shows "suspended" and a prompt instead, and Resume asks you to type `fg` there.

**False positive**: if a flag is fine, click **False positive** on its row in the Alerts tab or
on the banner, or the ✕ on the flag in the agent's card. It leaves the Alerts tab, the banner,
the Risk flags count and the agent's flags. A command or path is marked for that exact finding
in that session (`rm -rf build` marked fine leaves `rm -rf ~` flagged); a rule about how the
agent runs (many child processes, high CPU, a loop) is marked for that session.

**Responses tab** (Live activity): every pause, resume, kill and false positive, newest
first, also in `~/.cadre/responses.jsonl` and `--responses`. **Undo** on a false positive
brings the flag back, **Resume** on a Pause row continues an agent that is still paused. In
the Alerts tab a row says what you already did about its agent ("Killed 10:51", "Paused 10:51"). More: `docs/architecture/10-response.md`.

```powershell
py -m cadre                         # Windows
py -m cadre --once
```

**macOS menu bar icon** (optional, needs nothing beyond macOS):

```bash
python3 -m cadre start              # installed dashboard + icon, e.g. after "Quit cADRe"
python3 -m cadre menubar            # cADRe ring at the top right; count = agents working or waiting
python3 -m cadre menubar --stop
CADRE_MENUBAR_DEBUG=1 osascript -l JavaScript cadre/menubar.js   # foreground, prints each menu it builds
```

The icon is `cadre/menubar.js`, run by `/usr/bin/osascript` (JavaScript for Automation). It
only reads `/api/snapshot`, so the dashboard must be running (the menu offers "Start
dashboard", which kickstarts the LaunchAgent). Icon: the Formation mark (`cadre/static/menubar*.png`,
drawn by `scripts/make_icons.py`; a template image, so it follows the light or dark menu bar) =
watching, amber = an agent is waiting for you, hollow and dimmed = dashboard down or recording paused. The menu lists
the live agents (click one to open the dashboard), "Pause/Resume recording hook events",
"Stop dashboard" (unloads the LaunchAgent until "Start dashboard" or the next login),
"Quit menu bar only" and "Quit cADRe" (stops the dashboard, then the icon).

At login the LaunchAgent runs the installed launcher (`cadre_service.py`), which starts the
icon before the dashboard (skipped if one already runs), so it's still one autostart entry.
To keep the icon from coming up, set `CADRE_MENUBAR=0` in the plist's environment or pass
`--no-menubar` to the launcher. It also comes back when launchd restarts the dashboard.

**Windows tray icon** (optional, needs nothing beyond Windows and Python):

```powershell
py -m cadre start                   # installed dashboard + icon, e.g. after "Quit cADRe"
py -m cadre tray                    # cADRe ring in the notification area (Windows 11: maybe under ^)
py -m cadre tray --stop
py cadre\tray.py --debug            # foreground, prints the menu after every poll; Ctrl+C quits
```

The icon is `cadre/tray.py`, run by `pythonw.exe`: `Shell_NotifyIconW` and a popup menu
through ctypes, no pywin32. The menu is the macOS one: live agents (click = open the
dashboard), Open dashboard, Pause/Resume recording hook events, Start/Stop dashboard, "Quit
tray icon only", "Quit cADRe". Icon: blue ring = watching, amber ring = an agent is waiting
for you, grey ring = dashboard down or recording paused; the tooltip has the counts. Left or right
click opens the menu. "Stop dashboard" ends the dashboard process (it comes back at the next
login, with "Start dashboard" or `cadre start`). The HKCU Run value starts the launcher, which
starts the icon (same `CADRE_MENUBAR=0` / `--no-menubar` switch). Errors go to
`~/.cadre/tray.log`. `--stop` posts `WM_CLOSE` to its window (class `cADReTray`), so the icon
leaves the tray at once; posting `WM_COMMAND` with a menu id (`tray.CMD_*`) runs that item,
which is how it was tested without clicking. The icons are `cadre/static/tray*.ico`, drawn by
`scripts/make_icons.py`.

**Linux status icon** (optional, needs python3-gi, which every Ubuntu / Fedora desktop has):

```bash
python3 -m cadre start                 # installed dashboard + icon, e.g. after "Quit cADRe"
python3 -m cadre menubar               # cADRe ring in the top bar (alias: tray)
python3 -m cadre menubar --stop
python3 cadre/indicator.py --debug     # foreground, prints the menu after every poll; Ctrl+C quits
gdbus call --session -d org.kde.StatusNotifierWatcher -o /StatusNotifierWatcher \
  -m org.freedesktop.DBus.Properties.Get org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems
gdbus call --session -d org.kde.StatusNotifierItem-<pid>-1 -o /MenuBar \
  -m com.canonical.dbusmenu.GetLayout -- 0 -1 "@as []"    # the menu as the top bar sees it
```

The icon is `cadre/indicator.py`: a StatusNotifierItem with a D-Bus menu, over Gio, and
desktop notifications for new alerts (click = open the dashboard). Same menu, icons and colours
as the Windows tray. GNOME shows it through the AppIndicator extension (Ubuntu: on by default;
elsewhere install "AppIndicator and KStatusNotifierItem Support"); KDE, Xfce, Cinnamon, MATE and
waybar show it as they are. The systemd unit's launcher starts it, in a `systemd-run --user
--scope` of its own (`cadre-icon-<pid>.scope`), so stopping the dashboard leaves it up. Same
`CADRE_MENUBAR=0` / `--no-menubar` switch. Errors go to `~/.cadre/indicator.log`.

Pause works through the hooks of the *installed* copy: after pulling new code, re-run
`python3 -m cadre setup`; setup also quits the icon so the launcher restarts it from the new
copy. Pause stops hook events only; the dashboard still shows what it reads itself (processes,
Claude Code transcripts, ZCode/Codex session files).

The server binds to `127.0.0.1` only. CPU % on Windows without psutil (and in `--once`
there) reads 0 on the first sample; the dashboard's later polls show real values.

## 5. Hooks (live events)

Hooks make agents report events (prompt sent, tool used, waiting for you, turn finished)
instead of cADRe only polling. Each hook appends one line to `~/.cadre/events.jsonl`, which
the dashboard reads, so events are kept even while the dashboard isn't running. They are
watch-only: they never print, always exit 0, never block or approve anything, and don't use
the network.

```bash
python3 -m cadre --install-hooks --dry-run          # show what would change, touch nothing
python3 -m cadre --install-hooks                    # hook every agent found
python3 -m cadre --install-hooks --agents claude-code,copilot   # only some agents
python3 -m cadre --uninstall-hooks --dry-run
python3 -m cadre --uninstall-hooks                  # remove cADRe hooks everywhere
```

Agent keys for `--agents`: `claude-code`, `codex`, `gemini`, `copilot`, `cursor`, `windsurf`.

Agents installed after `setup` show up on the dashboard by themselves, but they get no hooks
until the installer runs again. The dashboard shows this:
- A live agent without the hook gets a **no hooks** badge (also "stale hook" for an old copy,
  "hook outdated" when, for example, a new Python changed the command) with an **Install hook**
  button that hooks only that agent's config file.
- **Hooks** in the header (with a count of files still to do) opens the coverage list: every
  hook file found, its state and how it was found. **Recalibrate** scans the whole machine again
  (new agents, new `CLAUDE_CONFIG_DIR`s), and **Install all pending** fixes every file that
  needs it. Each one is the same installer as `--install-hooks`: foreign hooks stay, backups
  go to `.cadre-bak`, and files that don't parse are left alone (shown as "config unreadable").
- The installed-tools table has a Hooks column with the same buttons.

The buttons only ever write the hook files the installer itself discovers, nothing a request
names, and like Pause they need a JSON POST from a loopback page.

**What gets found.** Agents that are running (config dirs read from their environment:
`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME`, `GEMINI_CLI_HOME`) and agents present on
disk (binary on PATH, app / install dir, or a config dir, even a leftover one). For Claude Code
that is `~/.claude` plus every `~/.claude-*` folder (e.g. `~/.claude-glm`).

| Agent | Hook file | Notes |
|---|---|---|
| Claude Code | `<config dir>/settings.json` | async; also fires in Devin, labelled `devin` |
| Codex CLI | `~/.codex/hooks.json` | **run `/hooks` in Codex once and trust them**, or they stay off |
| Gemini CLI | `~/.gemini/settings.json` | |
| Copilot CLI | `~/.copilot/hooks/cadre.json` | file owned by cADRe; `bash` command on macOS/Linux, `powershell` on Windows |
| Cursor | `~/.cursor/hooks.json` | observe-only events |
| Windsurf | `~/.codeium/windsurf/hooks.json` | post-hooks only |

On Windows `~` is `%USERPROFILE%`. Claude Desktop, ZCode, Aider, Goose, opencode and Ollama
have no command hooks; ZCode activity is read from its own database instead.

**Safe to re-run.** Your own hooks in those files are never touched. Any hook running a
`.../cadre/hooks/emit.py` (or the old AgentTracker path) counts as cADRe's and is replaced, so
re-running `--install-hooks` repoints stale entries. Each changed file is first copied to
`<file>.cadre-bak`. A file that isn't valid JSON is skipped, never overwritten.

**Re-run `--install-hooks` whenever you:** move or re-clone the repo, change the Python used
to install (the interpreter path is written into the hook command; on Windows it is the
`python.exe` that ran the installer), or install a new agent.

After installing: restart agents that were already running (they load hooks at start).

**Verify:**

```bash
python3 -m cadre --install-hooks --dry-run      # every target should say "up to date"
tail -f ~/.cadre/events.jsonl                   # macOS/Linux: events as they arrive
```

```powershell
Get-Content $HOME\.cadre\events.jsonl -Wait -Tail 5     # Windows
```

Then use an agent: events appear in the file and in the dashboard's live activity feed.

**Turn off temporarily** without uninstalling: set `CADRE_HOOKS=0` in the environment the
agent runs in (`export CADRE_HOOKS=0`, or `setx CADRE_HOOKS 0` on Windows, then restart the
agent), or use "Pause recording hook events" in the status icon's menu, which creates
`~/.cadre/paused`; every hook skips its event while that file exists (no agent restart
needed). Delete the file to resume. `CADRE_HOME` moves `~/.cadre` (set it for the agents and the dashboard alike);
`CADRE_URL=http://host:port/api/events` additionally POSTs each event there.

## 6. Keep the dashboard running (by hand)

`cadre setup` does this for you; these are the manual equivalents.

### macOS (launchd)

```bash
REPO="$PWD"
cat > ~/Library/LaunchAgents/com.cadre.dashboard.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.cadre.dashboard</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>-m</string><string>cadre</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/.cadre/dashboard.log</string>
</dict></plist>
EOF
mkdir -p ~/.cadre
launchctl load ~/Library/LaunchAgents/com.cadre.dashboard.plist     # start now and at login
launchctl unload ~/Library/LaunchAgents/com.cadre.dashboard.plist   # stop
```

### Linux (systemd user service)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/cadre.service <<EOF
[Unit]
Description=cADRe dashboard
[Service]
WorkingDirectory=$PWD
ExecStart=/usr/bin/python3 -m cadre
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now cadre      # start now and at login
systemctl --user disable --now cadre     # stop
```

### Windows (Task Scheduler)

```powershell
$repo = (Get-Location).Path
$py   = (Get-Command pyw).Source          # windowless launcher; or the full path to pythonw.exe
$act  = New-ScheduledTaskAction -Execute $py -Argument "-m cadre" -WorkingDirectory $repo
$trg  = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "cADRe" -Action $act -Trigger $trg -Description "cADRe dashboard"
Start-ScheduledTask -TaskName "cADRe"                       # start now
Unregister-ScheduledTask -TaskName "cADRe" -Confirm:$false  # remove
```

## 7. Troubleshooting

| Symptom | Check / fix |
|---|---|
| `Address already in use` | Another dashboard is running: open http://127.0.0.1:8765, or `lsof -nP -iTCP:8765 -sTCP:LISTEN` (Windows: `Get-NetTCPConnection -LocalPort 8765`). Or use `--port`. |
| Live activity feed stays empty | Is the dashboard running? `--install-hooks --dry-run` says "up to date"? Agent restarted since install? `CADRE_HOOKS` not `0`? Codex: hooks trusted in `/hooks`? |
| Events in `~/.cadre/events.jsonl` but not in the feed | The feed shows the newest 40; the dashboard loads the last 512 KB of the log at start. Do the hooks and the dashboard use the same `CADRE_HOME`? |
| `403 forbidden host` | The dashboard only answers to `127.0.0.1`, `localhost` and `[::1]` (protection against DNS rebinding). Open it by one of those names. |
| Installed dashboard doesn't start at login | Windows: `reg query HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v cADRe`; macOS: `launchctl print gui/$UID/com.cadre.dashboard`; Linux: `systemctl --user status cadre`. Its output goes to `~/.cadre/dashboard.log`. |
| An agent isn't listed | `python3 -m cadre --json` to see raw data. Add or fix its pattern in `cadre/registry.py`. |
| Hooks stopped after moving the repo or upgrading Python | `python3 -m cadre --install-hooks` (repoints them). |
| `skipped: can't parse` | That agent's config file isn't plain JSON (comments, trailing comma). Fix it, then re-run. |
| Windows: no project folders / config dirs for live agents | The header shows "Limited view: no psutil". Run `py -m cadre setup` again (it fetches psutil), or `py -m pip install psutil`, then restart the dashboard. |
| Windows: hooks don't fire | Python path in the hook must still exist: re-run `py -m cadre --install-hooks` from the Python you'll keep. |

## 8. Remove everything

```bash
python3 -m cadre uninstall --purge        # hooks, autostart, install folder and ~/.cadre
```

By hand instead: `python3 -m cadre --uninstall-hooks`, remove the autostart from section 6,
then `rm -rf ~/.cadre` (Windows: `Remove-Item -Recurse $HOME\.cadre`).

Backups `*.cadre-bak` next to agent config files can be deleted once you're happy.

## 9. Working on cADRe

Tests (stdlib `unittest`, no install needed, about 2 s, 6 s on Windows). Run them before every
commit:

```bash
python3 -m unittest                    # Windows: py -m unittest
python3 -m unittest tests.test_installer -v   # one file, verbose
```

They never touch real agent configs or `~/.cadre`: `CADRE_HOME` points at a scratch folder and
installer tests edit temporary files. What they cover: the installer (foreign hooks and keys
kept, backup, idempotent, stale paths repointed, unparsable files skipped, Windows command
lines, discovery in a fake home), emit.py (payload mapping per agent, and run as a real hook:
no output, exit 0 on any input, off switches), process matching (incl. Claude Code vs. Desktop
on Windows), `parse_windows`, the event log reader and history paging, and the Codex reader.

### Building a Windows release

A release is a per-user installer (`cADRe-<version>-windows-x64-setup.exe`, no admin rights)
that brings its own Python: python.org's embeddable build plus psutil, unpacked to
`%LOCALAPPDATA%\cADRe\python\`. Hooks, the dashboard and the tray then never depend on an
installed Python. macOS has no release package; it keeps using `python3 -m cadre setup`
(reasons in `docs/PLAN.md`, "Releases and platforms").

```powershell
winget install JRSoftware.InnoSetup       # once: the installer compiler (Inno Setup 6)
py scripts\build_release.py               # dist\cADRe-<v>-windows-x64-setup.exe and a .zip
py scripts\build_release.py --no-installer   # stage + zip only, no Inno Setup needed
```

The version is `__version__` in `cadre/__init__.py`; bump it before building a release. The
build downloads `python-<ver>-embed-amd64.zip` (default: the version of the `py` running it)
and refuses it unless its SHA-256 is in `packaging/windows/python-embed.sha256`. For a new
Python version, compare the hash it prints with the "SHA-256 checksum" column on python.org's
release page, then add the line. Before packaging it runs the unit tests and a real hook with
the bundled Python.

What the installer does: stops a running older release, unpacks `python\` and `release\`,
runs `release\cadre_release.py install` (= `cadre setup`: copy to `app\`, hooks, autostart,
start), adds a Start menu entry "cADRe" (starts the dashboard if needed and opens it) and an
entry in Settings → Apps. Uninstalling there removes the hooks and the autostart entry, then
the files; `~\.cadre` (the event log) stays. Logs: `%LOCALAPPDATA%\cADRe\release\setup.log`
(written line by line; a step that takes over 120 s is ended and the log then shows where every
thread was). If the window seems stuck or the dashboard doesn't come up, that log and
`~\.cadre\dashboard.log` say why; the installer's own log: add `/LOG="%TEMP%\cadre-inno.log"`.
Setup waits at most 15 s for the dashboard to answer, then finishes anyway.
Silent install: `cADRe-<v>-windows-x64-setup.exe /VERYSILENT`. The installer isn't signed, so
SmartScreen shows "Windows protected your PC": More info → Run anyway.

## 10. Where things live

| Path | What |
|---|---|
| `cadre/__main__.py` | CLI and HTTP server (`/`, `/api/snapshot`; `GET /api/events?before=&limit=&q=&agent=&kind=&project=` pages back through the event log for the feed and the explorer; `POST /api/events` for older hooks; `POST /api/pause`) |
| `cadre/service.py`, `cadre/paths.py` | `setup` / `uninstall` and the install folder |
| `cadre/collectors.py` | builds the snapshot: processes, transcripts, ZCode, installs, projects |
| `cadre/procs.py` | cross-platform process access (psutil / ps / /proc / lsof / PowerShell) |
| `cadre/registry.py` | known agents and how to recognise them (add new agents here) |
| `cadre/installer.py` | `--install-hooks` / `--uninstall-hooks` |
| `cadre/hooks/emit.py` | the hook every agent runs |
| `cadre/codex.py` | read-only Codex session reader (`~/.codex/sessions`) |
| `cadre/zcode.py` | read-only ZCode session DB reader |
| `~/.cadre/events.jsonl` | log of every hook event, written by the hooks (rotates at 20 MB) |
| `~/.cadre/events.jsonl.lock` | Windows only: empty file the hooks lock for the moment of a write; safe to delete |
| `~/.cadre/dashboard.log` | output of the dashboard started by `setup` |
| `scripts/build_release.py`, `packaging/windows/` | Windows release: build script, Inno Setup script, release bootstrap, Python hashes |
| `%LOCALAPPDATA%\cADRe\python\` | a release's own Python (embeddable + psutil) |
