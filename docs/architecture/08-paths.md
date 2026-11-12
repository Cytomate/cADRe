# 08 · Paths reference

Every path cADRe uses, reads or writes, per OS. `~` is `%USERPROFILE%` on Windows
(e.g. `C:\Users\me`) and `$HOME` on macOS/Linux (e.g. `/Users/me`). Paths are expanded
through `registry.expand()` (handles `~`, `%VAR%`, `$VAR`).

## cADRe itself

| What | Windows | macOS | Linux | Defined in |
|---|---|---|---|---|
| Git checkout (development) | wherever cloned, e.g. `C:\Users\me\source\repos\cADRe` | e.g. `~/source/repos/cADRe` | anywhere | – |
| Install root | `%LOCALAPPDATA%\cADRe` → `C:\Users\me\AppData\Local\cADRe` | `~/Library/Application Support/cADRe` | `$XDG_DATA_HOME/cadre` → `~/.local/share/cadre` | `paths.app_root()` |
| Installed package | `%LOCALAPPDATA%\cADRe\app\cadre\` | `~/Library/Application Support/cADRe/app/cadre/` | `~/.local/share/cadre/app/cadre/` | `paths.APP` |
| Launcher | `%LOCALAPPDATA%\cADRe\app\cadre_service.py` | `~/Library/Application Support/cADRe/app/cadre_service.py` | `~/.local/share/cadre/app/cadre_service.py` | `paths.LAUNCHER` |
| Hook program (installed) | `%LOCALAPPDATA%\cADRe\app\cadre\hooks\emit.py` (written as `C:/Users/…/emit.py` in commands) | `…/cADRe/app/cadre/hooks/emit.py` | `…/cadre/app/cadre/hooks/emit.py` | `paths.APP_EMIT` |
| Bundled Python (release) | `%LOCALAPPDATA%\cADRe\python\python.exe` (hooks), `…\pythonw.exe` (dashboard, tray) | `…/cADRe/python/bin/python3` (looked for, no Mac release yet) | `…/cadre/python/bin/python3` (same) | `paths.bundled_python()` |
| Release files (Windows) | `%LOCALAPPDATA%\cADRe\release\` (`cadre_release.py`, `cadre\`, `setup.log`), `…\unins000.exe` | – | – | `packaging/windows/cadre.iss` |
| Hook program (no install yet) | `<checkout>\cadre\hooks\emit.py` | `<checkout>/cadre/hooks/emit.py` | same | `paths.PKG_EMIT` |
| Data folder | `%USERPROFILE%\.cadre` | `~/.cadre` | `~/.cadre` | `events.LOG_DIR` (`CADRE_HOME`) |
| Event log | `~\.cadre\events.jsonl` (+ `.1`, and `events.jsonl.lock`, the writers' lock) | `~/.cadre/events.jsonl` (+ `.1`) | same | `events.LOG_FILE` |
| Pause flag | `~\.cadre\paused` | `~/.cadre/paused` | same | `emit.PAUSED` |
| Dashboard log | `~\.cadre\dashboard.log` | `~/.cadre/dashboard.log` | same | launcher |
| Tray log | `~\.cadre\tray.log` | – | – | `tray.log()` |
| Response log | `~\.cadre\responses.jsonl` | `~/.cadre/responses.jsonl` | `~/.cadre/responses.jsonl` | `respond.audit.path()` |
| False positives | `~\.cadre\dismissed.json` | `~/.cadre/dismissed.json` | `~/.cadre/dismissed.json` | `respond.dismiss.path()` |

## Autostart and processes

| What | Windows | macOS | Linux |
|---|---|---|---|
| Autostart entry | registry `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `cADRe` | `~/Library/LaunchAgents/com.cadre.dashboard.plist` (label `com.cadre.dashboard`) | `~/.config/systemd/user/cadre.service` |
| Dashboard process | `pythonw.exe …\cadre_service.py --port 8765` | `/usr/bin/python3 …/cadre_service.py --port 8765` (under launchd) | `python3 …/cadre_service.py --port 8765` (systemd) |
| Status icon process | `pythonw.exe …\app\cadre\tray.py --port 8765` (window class `cADReTray`) | `/usr/bin/osascript -l JavaScript …/app/cadre/menubar.js 8765` | none |
| Dashboard URL | `http://127.0.0.1:8765/` | same | same |

## Agent files cADRe reads

| What | Windows | macOS / Linux |
|---|---|---|
| Claude Code transcripts | `~\.claude\projects\<encoded cwd>\<id>.jsonl` | `~/.claude/projects/<encoded cwd>/<id>.jsonl` |
| Claude Code alternates | `~\.claude-*\projects\…` (`CLAUDE_CONFIG_DIR`) | `~/.claude-*/projects/…` |
| Codex rollouts | `~\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl` (`CODEX_HOME`) | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| ZCode DB | `~\.zcode\cli\db\db.sqlite` | `~/.zcode/cli/db/db.sqlite` |

`<encoded cwd>` replaces every non-alphanumeric character with `-`:
`C:\Users\me\source\repos\cADRe` → `C--Users-me-source-repos-cADRe`,
`/Users/me/source/repos/cADRe` → `-Users-me-source-repos-cADRe`.

## Agent hook files cADRe writes (on request only)

| Agent | Windows | macOS / Linux | Backup |
|---|---|---|---|
| Claude Code | `~\.claude\settings.json` | `~/.claude/settings.json` | `settings.json.cadre-bak` |
| Codex | `~\.codex\hooks.json` | `~/.codex/hooks.json` | `hooks.json.cadre-bak` |
| Gemini CLI | `~\.gemini\settings.json` | `~/.gemini/settings.json` | `.cadre-bak` |
| Copilot CLI | `~\.copilot\hooks\cadre.json` | `~/.copilot/hooks/cadre.json` | `.cadre-bak` |
| Cursor | `~\.cursor\hooks.json` | `~/.cursor/hooks.json` | `.cadre-bak` |
| Windsurf | `~\.codeium\windsurf\hooks.json` | `~/.codeium/windsurf/hooks.json` | `.cadre-bak` |

## Agent install evidence (detection only)

| Agent | Windows | macOS |
|---|---|---|
| Claude Code | `claude.exe` on PATH (e.g. `~\.local\bin\claude.exe`) | `claude` on PATH |
| Claude Desktop | `%LOCALAPPDATA%\AnthropicClaude`, or MSIX `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc` (binary in `C:\Program Files\WindowsApps\Claude_<ver>_x64__pzs8sxrjxfjjc\app\`) | `/Applications/Claude.app` |
| Codex | `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe` (real binary under `~\.codex\packages\standalone\releases\<ver>\bin\`) | `codex` on PATH |
| Cursor | `%LOCALAPPDATA%\Programs\cursor` | `/Applications/Cursor.app` |
| Windsurf | `%LOCALAPPDATA%\Programs\Windsurf` | `/Applications/Windsurf.app` |
| ZCode | `%LOCALAPPDATA%\Programs\ZCode` | `/Applications/ZCode.app` |
| Ollama | `%LOCALAPPDATA%\Programs\Ollama` | `/Applications/Ollama.app` |

## Environment variables

| Variable | Read by | Effect |
|---|---|---|
| `CADRE_HOME` | everything | moves `~/.cadre` |
| `CADRE_PORT` | `service.py`, tray | port for setup/start (default 8765) |
| `CADRE_HOOKS=0` | `emit.py` | that agent's hooks record nothing |
| `CADRE_URL` | `emit.py` | also POST each event there (0.5 s timeout) |
| `CADRE_MENUBAR=0` | launcher | don't start the status icon |
| `CADRE_MENUBAR_DEBUG` | `menubar.js` | log each menu to stdout |
| `CADRE_RUN_KEY` | `service.py` | another registry key for autostart (tests) |
| `XDG_DATA_HOME`, `XDG_CONFIG_HOME` | `paths.py`, `service.py` (Linux) | install root, systemd unit folder |
| `LOCALAPPDATA` | `paths.py` (Windows) | install root |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME`, `GEMINI_CLI_HOME` | read from running agents | extra config homes for discovery |
