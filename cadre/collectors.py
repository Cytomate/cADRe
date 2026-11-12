"""Collectors: turn the local machine state into a list of agent records.

Sources used (all read-only):
  * processes (cadre/procs.py: psutil, ps, /proc, lsof or PowerShell CIM) -> live
    processes, CPU, memory, uptime, parent/child tree, working dir, env
  * PATH / app bundles / install dirs / ~/.<agent> -> what is installed
  * ~/.claude/projects/*.jsonl -> Claude Code sessions: prompt, last tool, branch
  * ~/.codex/sessions/**/rollout-*.jsonl -> Codex sessions (cadre/codex.py)
  * ZCode's session DB (cadre/zcode.py)
"""
import glob
import json
import os
import platform
import re
import shutil
import time
from typing import Dict, List, Optional

from .registry import AGENTS, AgentSpec, HOME, expand
from .events import LOG_DIR, STORE
from . import codex, detect, respond, zcode
from . import procs as _procs          # `procs` is a local name in snapshot()
from .procs import IS_WIN, fill_details, list_processes, proc_cwds, proc_env  # noqa: F401 (re-exported)

ACTIVE_CPU = 2.0          # % CPU above which a live agent counts as working
ACTIVE_LOG_SECS = 45      # transcript written this recently => working
SLEEPING_WINDOW = 24 * 3600   # resumable sessions shown as "sleeping"
TAIL_BYTES = 512 * 1024
# a GUI app's or model server's working dir (/, C:\Windows\System32) says nothing about a project
NO_PROJECT_KINDS = ("desktop", "runtime")
CODEX_NAME = next(a.name for a in AGENTS if a.key == "codex")


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------
def match_spec(proc) -> Optional[AgentSpec]:
    for spec in AGENTS:
        if spec.exclude_args and re.search(spec.exclude_args, proc.get("args") or proc["exe"]):
            continue
        if spec.exe_pattern and re.search(spec.exe_pattern, proc["exe"]):
            return spec
        base = re.sub(r"\.exe$", "", os.path.basename(proc["exe"].replace("\\", "/")).lower())
        if spec.args_pattern and base in ("node", "bun", "python", "python3", "deno") \
                and re.search(spec.args_pattern, proc["args"]):
            return spec
    return None


def descendants(pid, children):
    out, stack = [], list(children.get(pid, []))
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(children.get(p, []))
    return out


# --------------------------------------------------------------------------
# Claude Code transcripts
# --------------------------------------------------------------------------
DEFAULT_CLAUDE_HOME = os.path.join(HOME, ".claude")


def claude_homes():
    """~/.claude plus alternate config dirs such as ~/.claude-glm (CLAUDE_CONFIG_DIR)."""
    homes = [h for h in glob.glob(os.path.join(HOME, ".claude*"))
             if os.path.isdir(os.path.join(h, "projects"))]
    return sorted(set(homes))


def _encode(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def claude_project_dir(cwd, home=DEFAULT_CLAUDE_HOME):
    return os.path.join(home, "projects", _encode(cwd))


# ---------------------------------------------------------------- model family
FAMILY_RULES = [   # (regex on model / endpoint / config dir, key, label)
    (r"glm|z\.ai|bigmodel|zhipu|claude-glm", "glm", "GLM"),
    (r"claude|opus|sonnet|haiku|fable|anthropic", "claude", "Claude"),
    (r"gpt|openai|codex|\bo[1-9]", "openai", "GPT"),
    (r"gemini|google", "gemini", "Gemini"),
    (r"qwen", "qwen", "Qwen"),
    (r"kimi|moonshot", "kimi", "Kimi"),
    (r"deepseek", "deepseek", "DeepSeek"),
    (r"llama|mistral|ollama|localhost|127\.0\.0\.1", "local", "Local"),
]
AGENT_DEFAULT_FAMILY = {
    "claude-code": "claude", "claude-desktop": "claude", "codex": "openai",
    "gemini": "gemini", "zcode": "glm", "copilot": "copilot", "cursor": "cursor",
    "windsurf": "windsurf", "ollama": "local",
}
FAMILY_LABELS = {k: l for _, k, l in FAMILY_RULES}
FAMILY_LABELS.update({"copilot": "Copilot", "cursor": "Cursor", "windsurf": "Windsurf", "other": "Other"})


def model_family(agent_key, *hints):
    """Pick the family from the most specific hint first (model, then endpoint, then config dir)."""
    for h in hints:
        if not h or h == "<synthetic>":
            continue
        for rx, key, _ in FAMILY_RULES:
            if re.search(rx, h, re.I):
                return key
    return AGENT_DEFAULT_FAMILY.get(agent_key, "other")


def pretty_model(m):
    if not m or m == "<synthetic>":
        return None
    x = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)-(\d+)", m)
    if x:
        return "%s %s.%s" % (x.group(1).title(), x.group(2), x.group(3))
    return re.sub(r"\[.*?\]$", "", m).upper() if m.lower().startswith("glm") else m


def apply_family(rec, *hints):
    fam = model_family(rec.get("agent_key"), rec.get("model"), *hints)
    rec["family"], rec["family_label"] = fam, FAMILY_LABELS.get(fam, fam.title())
    rec["model_label"] = pretty_model(rec.get("model"))
    harness = rec.get("agent")
    if rec["family"] != AGENT_DEFAULT_FAMILY.get(rec.get("agent_key")):
        harness = "%s · %s" % (rec.get("agent"), rec["family_label"])   # e.g. "Claude Code · GLM"
    rec["display_name"] = harness


def _tail_lines(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()                      # drop partial line
        data = f.read().decode("utf-8", "replace")
    return data.splitlines()


def _short(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _describe_tool(block):
    name = block.get("name", "?")
    inp = block.get("input") or {}
    detail = inp.get("description") or inp.get("command") or inp.get("file_path") \
        or inp.get("pattern") or inp.get("url") or inp.get("prompt") or ""
    return _short("%s: %s" % (name, detail) if detail else name, 140)


def read_claude_session(path):
    """Summarise a Claude Code session transcript (reads only its tail)."""
    info = {
        "session_id": os.path.basename(path).rsplit(".", 1)[0],
        "transcript": path, "last_activity": os.path.getmtime(path),
        "cwd": None, "git_branch": None, "last_prompt": None,
        "last_tool": None, "model": None, "permission_mode": None,
        "tool_calls": 0, "current_dir": None,
    }
    home_dir, cwds = os.path.dirname(path), []
    for line in _tail_lines(path):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("cwd"):
            cwds.append(e["cwd"])
        info["git_branch"] = e.get("gitBranch") or info["git_branch"]
        if e.get("type") == "permission-mode":
            info["permission_mode"] = e.get("permissionMode")
        msg = e.get("message") or {}
        if e.get("type") == "user" and not e.get("isMeta"):
            c = msg.get("content")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c
                             if isinstance(b, dict) and b.get("type") == "text")
            if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                info["last_prompt"] = _short(c, 240)
        elif e.get("type") == "assistant":
            if msg.get("model") and msg.get("model") != "<synthetic>":
                info["model"] = msg["model"]
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    info["tool_calls"] += 1
                    info["last_tool"] = _describe_tool(b)
    if cwds:
        # project = the folder the session belongs to; current_dir = where it is now
        info["cwd"] = cwds[0]
        for c in reversed(cwds):
            root = _project_root(c, os.path.basename(home_dir))
            if root:
                info["cwd"] = root
                break
        info["current_dir"] = cwds[-1]
    return info


def _project_root(cwd, name):
    """cwd or the nearest folder above it that Claude Code stores under projects/<name>;
    None if there is none. The climb ends when dirname() stops moving, not at "/": on
    Windows dirname("C:\\\\") is "C:\\\\" again (the same for a UNC share), and waiting for "/"
    or "" there never ended, which froze every snapshot (2026-11-10)."""
    cand = cwd
    while cand:
        if _encode(cand) == name:
            return cand
        parent = os.path.dirname(cand)
        if parent == cand:
            return None
        cand = parent
    return None


def _newest(files, seen, since):
    """The newest of `files` that no other process has claimed and, when `since` is given,
    that was written at or after it."""
    best = None
    for f in files:
        if f in seen:
            continue
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if (since is None or m >= since - 2) and (best is None or m > best[0]):
            best = (m, f)
    return best[1] if best else None


def latest_claude_transcript(cwd, session_id=None, home=DEFAULT_CLAUDE_HOME, seen=(), since=None):
    """A guess, for a Claude Code that has no session file (claude_live_sessions): the resumed
    session if the command line names one, else the newest transcript of its folder that no
    other process has and that was written since the process started (`since`). A transcript
    older than the process belongs to an earlier session in that folder, whose flags must not
    land on this one."""
    d = claude_project_dir(cwd, home)
    if session_id:
        p = os.path.join(d, session_id + ".jsonl")
        if os.path.exists(p):
            return p
    return _newest(glob.glob(os.path.join(d, "*.jsonl")), seen, since)


def newest_unclaimed_transcript(home, seen, since=None):
    return _newest(glob.glob(os.path.join(home, "projects", "*", "*.jsonl")), seen, since)


SESSION_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")
START_SLACK = 5      # seconds a session may seem to start before its process (clock readings differ)


def claude_live_sessions():
    """{pid: {"session_id", "cwd", "started", "home"}} from <home>/sessions/<pid>.json, which
    Claude Code writes for every running session (seen on 2.1.322). It is the only exact link
    from a process to its session: everything else is a guess from folders and file times."""
    out = {}
    for h in claude_homes():
        for f in glob.glob(os.path.join(h, "sessions", "*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    d = json.load(fh)
                pid, sid = int(d["pid"]), d["sessionId"]
                started = float(d.get("startedAt") or 0) / 1000.0
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(sid, str) and SESSION_ID.match(sid):
                cwd = d.get("cwd")
                out[pid] = {"session_id": sid, "cwd": cwd if isinstance(cwd, str) and cwd else None,
                            "started": started, "home": h}
    return out


def live_session(sessions, proc, now):
    """The session file of this process, or None. A file left behind by an earlier process
    that had the same pid is older than this process and doesn't count."""
    s = sessions.get(proc["pid"])
    if not s:
        return None
    if proc.get("uptime_s") and s["started"] < now - proc["uptime_s"] - START_SLACK:
        return None
    return s


def claude_transcript(session_id, home):
    files = glob.glob(os.path.join(home, "projects", "*", session_id + ".jsonl"))
    return files[0] if files else None


def all_claude_transcripts():
    out = []
    for h in claude_homes():
        out += glob.glob(os.path.join(h, "projects", "*", "*.jsonl"))
    return out


def home_of(transcript):
    return os.path.dirname(os.path.dirname(os.path.dirname(transcript)))


# --------------------------------------------------------------------------
# installed
# --------------------------------------------------------------------------
def installed_evidence(spec):
    ev = []
    for b in spec.binaries:
        p = shutil.which(b)
        if p:
            ev.append(p)
    ev += [expand(a) for a in spec.apps if os.path.exists(expand(a))]
    ev += [d for d in spec.config_dirs if os.path.isdir(expand(d))]
    return ev


# --------------------------------------------------------------------------
# risk signals: see cadre/detect (rules) and detect.review (how records get their flags)
# --------------------------------------------------------------------------
FEED_EVENTS = 50     # newest events in every snapshot (the dashboard's feed); older: GET /api/events


def _same_dir(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# --------------------------------------------------------------------------
# hook coverage: which agents have cADRe's hook in their config (installer.check)
# --------------------------------------------------------------------------
HOOK_SCAN_SECS = 30      # the whole-machine scan is cached; live agents' own files are read every time
STATE_RANK = {"ok": 0, "outdated": 1, "stale": 2, "missing": 3, "unreadable": 4}


def hook_coverage(procs, records, now):
    """Scan hook files (cached) and set rec["hooks"] on live agents that support hooks."""
    from . import installer           # installer imports this module
    try:
        cov = installer.scan(procs, max_age=HOOK_SCAN_SECS)
    except Exception:                 # never let a config file break the snapshot
        return None
    by_path = {os.path.normcase(t["path"]): t for t in cov["targets"]}
    for rec in records:
        key = rec.get("agent_key")
        if rec.get("pid") is None or key not in installer.FORMATS or key == "devin":
            continue
        home = rec.get("config_dir") if key in ("claude-code", "codex") else None
        path = installer.hook_file(key, home)
        t = by_path.get(os.path.normcase(path))
        if t is None:                 # started after the cached scan: read its file now
            t = dict(installer.check(installer.Target(key, rec["agent"], path, [])), path=path)
        h = {"state": t["state"], "detail": t["detail"], "path": path,
             "display": path.replace(HOME, "~")}
        if t["state"] == "ok" and not rec.get("hooked"):
            if key == "codex":
                h["hint"] = "installed; trust them in Codex with /hooks if no events arrive"
            elif key != "claude-code":
                try:
                    if now - (rec.get("uptime_s") or 0) < os.path.getmtime(path):
                        h["hint"] = "installed after this session started: restart it to start them"
                except OSError:
                    pass
        rec["hooks"] = h
    return cov


def agent_hooks(key, cov):
    """One state per agent for the installed-tools table: the worst of its hook files."""
    from . import installer
    if key in installer.NO_HOOKS:
        return {"state": "unsupported", "detail": installer.NO_HOOKS[key]}
    ts = [t for t in (cov or {}).get("targets", []) if t["agent"] == key]
    if not ts:
        return None
    worst = max(ts, key=lambda t: STATE_RANK.get(t["state"], 0))
    return {"state": worst["state"], "detail": worst["detail"], "files": len(ts),
            "paths": [t["path"] for t in ts if t["state"] in installer.FIXABLE]}


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------
HOOK_STALE_SECS = 600   # a "working" hook event older than this no longer counts


def hook_status(ev, now):
    """Status from the latest hook event, which beats CPU guessing when available."""
    e = ev.get("event")
    if e == "Notification":
        return "waiting"
    if e in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart"):
        return "working" if now - ev.get("ts", 0) < HOOK_STALE_SECS else None
    if e in ("Stop", "SessionStart"):
        return "idle"
    return None


def apply_hook(rec, now, claimed=()):
    if not rec.get("session_id") and rec.get("pid") and rec.get("cwd"):
        # just started, no transcript yet: its SessionStart hook still names the session
        start = now - (rec.get("uptime_s") or 0) - 5
        sid = STORE.fresh_session(rec["agent_key"], lambda d: _same_dir(d, rec["cwd"]), start, claimed)
        if sid:
            rec["session_id"] = sid
    ev = STORE.latest(rec.get("session_id")) if rec.get("session_id") else None
    if not ev:
        return
    rec["hooked"] = True
    rec["live_event"] = ev
    if ev.get("tool"):
        rec["last_tool"] = _short("%s: %s" % (ev["tool"], ev["detail"]) if ev.get("detail") else ev["tool"], 140)
    if ev.get("ts", 0) > (rec.get("last_activity") or 0):
        rec["last_activity"] = ev["ts"]
    if rec.get("status") == "suspended" or rec.get("pid") is None:
        return
    st = hook_status(ev, now)
    if st:
        rec["status"] = st
    if st == "waiting":
        rec["waiting_for"] = ev.get("detail") or "your input"


def review(rec, session_events, now):
    """The record's findings and flags (cadre/detect), minus what the user marked as a false
    positive (cadre/respond/dismiss.py): those move to rec["dismissed"]."""
    detect.review_record(rec, session_events, now)
    return respond.dismiss.apply_record(rec)


def classify(proc, last_activity, now, busy_children):
    if "T" in proc["stat"]:
        return "suspended"
    if proc["cpu"] >= ACTIVE_CPU or busy_children or \
            (last_activity and now - last_activity < ACTIVE_LOG_SECS):
        return "working"
    return "idle"


def snapshot():
    now = time.time()
    STORE.sync()                       # hook events appended to ~/.cadre/events.jsonl since last time
    procs = list_processes()
    respond.release_orphans(procs, now)    # an agent that ended while paused: let its shell go
    children: Dict[int, List[int]] = {}
    for p in procs.values():
        children.setdefault(p["ppid"], []).append(p["pid"])

    # live agent processes; skip ones whose parent is the same agent (helpers/forks)
    live = []
    for p in procs.values():
        spec = match_spec(p)
        if not spec:
            continue
        parent = procs.get(p["ppid"])
        if parent and match_spec(parent) is spec:
            continue
        live.append((spec, p))

    # CPU and stopped state are only needed for agents, their children and the tracker itself
    wanted = {os.getpid()}
    for _, p in live:
        wanted.add(p["pid"])
        wanted.update(descendants(p["pid"], children))
    fill_details(procs, wanted)

    cwds = proc_cwds([p["pid"] for _, p in live])
    records, seen_transcripts, codex_homes = [], set(), set()

    # Claude Code: the session each process says it runs (exact), taken before any guessing so
    # that no other process can be given its transcript
    claude_procs = [p for spec, p in live if spec.session_source == "claude"]
    sessions = claude_live_sessions() if claude_procs else {}
    exact = {}
    for p in claude_procs:
        s = live_session(sessions, p, now)
        if s:
            t = claude_transcript(s["session_id"], s["home"])
            exact[p["pid"]] = (s, t)
            if t:
                seen_transcripts.add(t)
    # with neither a session file nor a folder there is nothing to tell two processes apart
    blind = [p["pid"] for p in claude_procs if p["pid"] not in exact and not cwds.get(p["pid"])]

    for spec, p in live:
        own = exact.get(p["pid"])
        cwd = cwds.get(p["pid"]) or (own[0]["cwd"] if own else None)
        kids = [procs[k] for k in descendants(p["pid"], children) if k in procs]
        busy_kids = [k for k in kids if k["cpu"] >= ACTIVE_CPU]
        rec = {
            "id": "%s:%d" % (spec.key, p["pid"]), "agent": spec.name,
            "agent_key": spec.key, "kind": spec.kind, "pid": p["pid"],
            "cpu": p["cpu"], "rss_mb": p["rss_mb"], "uptime_s": p["uptime_s"],
            "cwd": cwd, "project": os.path.basename(cwd.rstrip("\\/")) or None
            if cwd and cwd != "/" and spec.kind not in NO_PROJECT_KINDS else None,
            "args": p["args"], "child_count": len(kids),
            "running_children": [_short(os.path.basename(k["exe"]) + " " +
                                        k["args"].split(" ", 1)[-1], 80) for k in busy_kids][:5],
            "launched_by": "Claude Desktop" if re.search(r"Application Support/Claude|AnthropicClaude", p["exe"]) else None,
        }
        last_act, hints = None, []
        if spec.session_source == "claude":
            env = proc_env(p["pid"], ["CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL",
                                      "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"])
            home = env.get("CLAUDE_CONFIG_DIR") or (own[0]["home"] if own else DEFAULT_CLAUDE_HOME)
            rec["config_dir"], rec["endpoint"] = home, env.get("ANTHROPIC_BASE_URL")
            hints = [env.get("ANTHROPIC_MODEL"), env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                     env.get("ANTHROPIC_BASE_URL"), home]
        if spec.session_source == "claude":
            if own:
                # no transcript yet = no prompt yet: the session is known, there is nothing to read
                t, rec["session_id"] = own[1], own[0]["session_id"]
            else:
                # older Claude Code: guess. A wrong guess puts one agent's flags, and the Kill
                # button that goes with them, on another agent, so only take what fits.
                m = re.search(r"--resume[= ]([0-9a-f-]{36})", p["args"])
                resumed = m or re.search(r"\s(--continue|-c|--resume|-r)(\s|=|$)", p["args"])
                since = None if resumed or not p["uptime_s"] else now - p["uptime_s"]
                if cwd:
                    t = latest_claude_transcript(cwd, m.group(1) if m else None, home, seen_transcripts, since)
                elif blind == [p["pid"]]:
                    # no cwd (Windows without psutil), and the only such process
                    t = newest_unclaimed_transcript(home, seen_transcripts, since)
                else:
                    t = None
            if t:
                seen_transcripts.add(t)
                rec.update(read_claude_session(t))
                last_act = rec["last_activity"]
                if cwd:
                    rec["cwd"] = cwd  # process cwd wins over transcript
                elif rec.get("cwd"):
                    rec["project"] = os.path.basename(rec["cwd"].rstrip("\\/"))
        elif spec.session_source == "codex":
            chome = proc_env(p["pid"], ["CODEX_HOME"]).get("CODEX_HOME")
            if chome:
                codex_homes.add(chome)
                rec["config_dir"] = os.path.expanduser(chome)
            where = codex.session_dir(p["args"], cwd)
            f = codex.find_live_session(codex.session_files([chome] if chome else ()), where,
                                        now - p["uptime_s"] if p["uptime_s"] else None,
                                        codex.resume_id(p["args"]), seen_transcripts)
            s = codex.read_session(f) if f else None
            if s:
                seen_transcripts.add(f)
                rec.update(s)
                last_act = s["last_activity"]
                rec["cwd"] = where or s["cwd"]
                rec["project"] = os.path.basename(rec["cwd"].rstrip("\\/")) if rec["cwd"] else None
        rec["status"] = classify(p, last_act, now, busy_kids)
        if respond.paused_by_us(p["pid"], now - p["uptime_s"] if p["uptime_s"] else None, p.get("stat")):
            rec["paused_by_cadre"] = True          # the Pause button; some backends can't see "T"
            rec["status"] = "suspended"
        elif respond.needs_fg(p["pid"]):           # resumed, but its terminal has to hear `fg`
            rec["needs_fg"] = True
        if spec.session_source == "codex" and rec["status"] == "idle" and codex.turn_running(rec, now):
            rec["status"] = "working"
        apply_hook(rec, now, {r.get("session_id") for r in records})
        review(rec,STORE.session_events(rec.get("session_id")), now)
        apply_family(rec, *hints)
        records.append(rec)

    # ZCode: attach its active session to the running app, the rest of today's become sleeping
    zsessions = zcode.read_sessions()
    zused = set()
    zlive = [r for r in records if r["agent_key"] == "zcode"]
    if zsessions and zlive:
        s0 = next((z for z in zsessions if z["turn_running"]), zsessions[0])
        zused.add(s0["session_id"])
        rec = zlive[0]
        for k in ("session_id", "cwd", "project", "title", "last_prompt", "last_tool", "model", "last_activity"):
            if s0.get(k):
                rec[k] = s0[k]
        if rec["status"] != "suspended":
            rec["status"] = "waiting" if s0["waiting_for"] else "working" if s0["turn_running"] else "idle"
        if s0["waiting_for"]:
            rec["waiting_for"] = s0["waiting_for"]
        rec["destructive_recent"] = s0["destructive_recent"]
        review(rec,(), now)
        rec["session_source"] = "zcode-db"
        apply_family(rec, s0.get("provider"))
    for z in zsessions:
        if z["session_id"] in zused or not z["recent"]:
            continue
        rec = {"id": "zcode:" + z["session_id"], "agent": "ZCode", "agent_key": "zcode", "kind": "ide",
               "pid": None, "status": "sleeping", "flags": [], "findings": [], "session_source": "zcode-db",
               **{k: z[k] for k in ("session_id", "cwd", "project", "title", "last_prompt",
                                    "last_tool", "model", "last_activity")}}
        apply_family(rec, z.get("provider"))
        records.append(rec)

    # sleeping: recent Claude Code sessions with no live process
    for t in all_claude_transcripts():
        if t in seen_transcripts or now - os.path.getmtime(t) > SLEEPING_WINDOW:
            continue
        s = read_claude_session(t)
        rec = {"id": "claude-code:" + s["session_id"], "agent": "Claude Code",
               "agent_key": "claude-code", "kind": "cli", "pid": None,
               "status": "sleeping", **s}
        rec["project"] = os.path.basename(s["cwd"]) if s["cwd"] else None
        home = home_of(t)
        prefix = "" if home == DEFAULT_CLAUDE_HOME else "CLAUDE_CONFIG_DIR=%s " % home.replace(HOME, "~")
        rec["resume_cmd"] = "cd %s && %sclaude --resume %s" % (s["cwd"], prefix, s["session_id"]) \
            if s["cwd"] else None
        rec["config_dir"] = home
        review(rec,STORE.session_events(rec.get("session_id")), now)
        apply_family(rec, home)
        records.append(rec)

    # sleeping: recent Codex sessions with no live process
    codex_files = codex.session_files(codex_homes)
    for f in codex_files:
        try:
            if f in seen_transcripts or now - os.path.getmtime(f) > SLEEPING_WINDOW:
                continue
        except OSError:
            continue
        s = codex.read_session(f)
        if not s:
            continue
        rec = {"id": "codex:" + s["session_id"], "agent": CODEX_NAME, "agent_key": "codex",
               "kind": "cli", "pid": None, "status": "sleeping", **s}
        rec["project"] = os.path.basename(s["cwd"].rstrip("\\/")) if s["cwd"] else None
        rec["resume_cmd"] = ("cd %s && " % s["cwd"] if s["cwd"] else "") + "codex resume " + s["session_id"]
        review(rec,STORE.session_events(rec.get("session_id")), now)
        apply_family(rec)
        records.append(rec)

    hooks = hook_coverage(procs, records, now)

    # installed but not running
    live_keys = {r["agent_key"] for r in records}
    installed = []
    for spec in AGENTS:
        ev = installed_evidence(spec)
        if ev:
            i = {"agent": spec.name, "agent_key": spec.key, "kind": spec.kind,
                 "evidence": ev, "active": spec.key in live_keys,
                 "hooks": agent_hooks(spec.key, hooks)}
            apply_family(i)
            installed.append(i)

    # project folders view: every folder any agent has touched
    projects = {}

    def project(path, name=None):
        k = os.path.normcase(os.path.normpath(path))
        if k not in projects:
            projects[k] = {"path": path, "name": name or os.path.basename(path.rstrip("\\/")) or path,
                           "agents": set(), "sessions": None, "last_activity": None, "git_branch": None}
        return projects[k]

    def touch(p, agent, sessions, last, branch):
        p["agents"].add(agent)
        p["sessions"] = (p["sessions"] or 0) + sessions
        if last and last > (p["last_activity"] or 0):
            p["last_activity"], p["git_branch"] = last, branch or p["git_branch"]

    newest = {}
    for t in all_claude_transcripts():
        d, m = os.path.dirname(t), os.path.getmtime(t)
        if d not in newest or m > newest[d][0]:
            newest[d] = (m, t)
    for d, (m, t) in newest.items():
        s = read_claude_session(t)
        touch(project(s["cwd"] or d), "Claude Code", len(glob.glob(os.path.join(d, "*.jsonl"))), m, s["git_branch"])
    for f in codex_files:
        meta = codex.read_meta(f)
        if meta["cwd"]:
            try:
                touch(project(meta["cwd"]), CODEX_NAME, 1, os.path.getmtime(f), meta["git_branch"])
            except OSError:
                pass
    for r in records:
        if r.get("cwd") and r.get("project"):
            project(r["cwd"], r["project"])["agents"].add(r["agent"])
    project_list = [dict(p, agents=sorted(p["agents"])) for p in projects.values()]
    project_list.sort(key=lambda p: p["last_activity"] or now, reverse=True)

    order = {"waiting": 0, "working": 1, "idle": 2, "suspended": 3, "sleeping": 4}
    records.sort(key=lambda r: (order.get(r["status"], 9), -(r.get("last_activity") or 0)))
    counts = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    counts["installed"] = len(installed)
    me = procs.get(os.getpid(), {})
    tracker = {"pid": os.getpid(), "cpu": me.get("cpu"), "rss_mb": me.get("rss_mb"),
               "uptime_s": me.get("uptime_s"), "watching": len(records),
               "backend": _procs.backend(), "limited": _procs.limited(),
               "events_received": STORE.total,
               "hooked_sessions": sum(1 for r in records if r.get("hooked")),
               "paused": os.path.exists(os.path.join(LOG_DIR, "paused")),   # emit.py skips events
               "flagged": [{"id": r["id"], "name": r.get("project") or r["agent"],
                            "flags": [detect.flag_text(f) for f in detect.alerting(r.get("findings"))]}
                           for r in records if detect.alerting(r.get("findings"))]}
    return {"generated_at": now, "host": platform.node(), "counts": counts, "tracker": tracker,
            "hooks": hooks,
            "alerts": respond.ALERTS.update(records, now),          # findings that are new (cadre/respond)
            "responses": respond.audit.entries(50)[::-1],           # the response log, newest first
            "dismissed": respond.dismiss.active(),                  # false positives in force, newest first
            "events": [respond.dismiss.apply_event(e) for e in
                       sorted(STORE.tail(FEED_EVENTS) + [detect.review_event(e) for e in zcode.read_events(FEED_EVENTS)],
                              key=lambda e: e.get("ts") or 0, reverse=True)[:FEED_EVENTS]],
            "agents": records, "installed": installed, "projects": project_list}
