"""Pause, resume or kill one agent, on the user's say-so. The only code in cADRe that changes
an agent's process, and it never runs on its own: act() is called by POST /api/respond, which
a button in the dashboard sends.

Before anything is touched:
  * the target must be an agent in the current snapshot that has a process (never a bare pid
    from the request);
  * the process list is read again, and the pid must still be that agent's program, started
    at the same time (a pid can be reused within seconds on Windows);
  * cADRe's own process and its children are taken out of the tree, so it can never suspend
    or end itself (it may have been started from the agent's own terminal).

An agent in a terminal (macOS, Linux). A shell that sees its foreground job stop prints
"suspended" and takes the keyboard back; a later SIGCONT then only continues the agent as a
background job, which the terminal stops again as soon as it reads a key. To the user the
agent looks killed. So Pause first stops the agent's ancestors on that terminal (its shell, a
wrapper script), outermost first: a stopped shell can't react, the agent stays the foreground
job, and the terminal just stands still. Resume and Kill let them go again, after the agent.
These ancestors ("held", in the log entry) are the one thing outside the agent's tree that
is ever signalled, and only with SIGSTOP / SIGCONT. If the agent ends some other way while
paused, release_orphans() (called with every snapshot) lets its shell go. Should the shell be
out of reach, Resume says "type fg in its terminal" instead.

On Linux, holding the shell isn't enough: bash 5 takes the terminal back as soon as it is
continued after its job was (seen in GNOME Terminal and Ptyxis), and tmux continues a pane's
shell the moment it stops. So when the agent is its terminal's foreground job and sits in a
scope of that tab or tmux pane (procs.terminal_scope), Pause freezes that cgroup instead and
sends no signal at all: nothing notices, and the terminal stands still. Anything else running
in that tab is frozen with it and named in the result ("also_frozen"). Resume thaws it; Kill
sends SIGKILL (which a frozen process still obeys) and then thaws, so the shell can collect it.

The action covers the agent and its child processes: a paused agent whose `rm -rf` child
keeps running is not paused. Every call, worked or not, is written to the response log.
"""
import os
import time

from .. import procs
from . import audit

ACTIONS = ("pause", "resume", "kill")
GRACE = 3.0           # seconds a killed agent has to exit before it is ended by force
START_SLACK = 5       # seconds two readings of one process's start time may differ

_paused = None        # {pid: start time} of what cADRe paused; rebuilt from the response log
_front = set()        # pids paused in front of a shell we couldn't hold, until `fg` is typed
_held = {}            # agent pid -> [[pid, start time], ...]: its shell etc., stopped with it
_scopes = {}          # agent pid -> the cgroup frozen to pause it (Linux)
FG_HINT = "type fg in its terminal to give it the keyboard back"
HOLD_CHECK = 0.1      # s after a pause: is the held shell still stopped? (tmux continues it at once)


class Refused(Exception):
    """The request was fine but the machine says no (process gone, pid reused, ...)."""


def _paused_map():
    global _paused
    if _paused is None:
        _paused = {}
        for e in audit.entries(500):
            if e.get("ok") and e.get("pid"):
                if e["action"] == "pause":
                    _paused[e["pid"]] = e.get("started")
                    front = e.get("foreground") and not e.get("held")
                    (_front.add if front else _front.discard)(e["pid"])
                    _held.pop(e["pid"], None)
                    _scopes.pop(e["pid"], None)
                    if e.get("held"):
                        _held[e["pid"]] = e["held"]
                    if e.get("frozen"):
                        _scopes[e["pid"]] = e["frozen"]
                        procs.remember_frozen(e["frozen"])
                else:
                    _paused.pop(e["pid"], None)
                    _held.pop(e["pid"], None)
                    _scopes.pop(e["pid"], None)
                    if e["action"] == "kill":
                        _front.discard(e["pid"])
    return _paused


def paused_by_us(pid, started=None, stat=None):
    """Did cADRe pause this process? (`started` tells it from a later process with its pid.)
    Also how a backend that can't see "suspended" (Windows without psutil) learns of it.
    `stat`: where the backend does see it (macOS, Linux), a process that isn't stopped isn't
    paused, whatever the log says: the user typed `fg` in its terminal, or sent SIGCONT."""
    m = _paused_map()
    if pid not in m:
        return False
    if stat is not None and procs.sees_stopped() and "T" not in stat:
        return False
    return not (started and m[pid]) or abs(m[pid] - started) <= START_SLACK


def needs_fg(pid):
    """Resumed, but still a background job of the terminal it was paused in: it runs without
    a keyboard, or was stopped again for reading it, until the user types `fg` there."""
    if pid not in _front or pid in _paused_map():
        return False
    if procs.foreground(pid):
        _front.discard(pid)
        return False
    return True


def _children(listing):
    kids = {}
    for p in listing.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    return kids


def _tree(root, listing, kids, strict=True):
    """root and its descendants, parents first. strict: leave out a "child" that is older
    than its parent. Its real parent is gone and the pid was given to this process later."""
    out, seen, queue = [], {root}, [root]
    while queue:
        pid = queue.pop(0)
        out.append(pid)
        up = (listing.get(pid) or {}).get("uptime_s") or 0
        for c in kids.get(pid, []):
            cu = (listing.get(c) or {}).get("uptime_s") or 0
            if c in seen or (strict and up and cu > up + START_SLACK):
                continue
            seen.add(c)
            queue.append(c)
    return out


def _apply(fn, tree):
    """fn on the agent first (if that fails, so does the action), then on its children."""
    fn(tree[0])
    done, failed = [tree[0]], []
    for pid in tree[1:]:
        try:
            fn(pid)
            done.append(pid)
        except OSError:                 # a child that just exited, or one we may not touch
            failed.append(pid)
    return done, failed


def _target(snap, rec, now):
    """Check the agent's process is still the one in the snapshot. -> (proc, tree, skipped)"""
    from ..collectors import match_spec          # here: collectors imports this package
    pid = rec["pid"]
    listing = procs.list_processes()
    p = listing.get(pid)
    if not p:
        raise Refused("process %d is already gone" % pid)
    spec = match_spec(p)
    if not spec or spec.key != rec.get("agent_key"):
        raise Refused("pid %d is no longer %s" % (pid, rec.get("agent") or "that agent"))
    kids = _children(listing)
    loose = _tree(pid, listing, kids, strict=False)
    procs.fill_details(listing, loose)
    was = (snap.get("generated_at") or now) - rec["uptime_s"] if rec.get("uptime_s") else None
    p["started"] = now - p["uptime_s"] if p.get("uptime_s") else None
    if was and p["started"] and abs(p["started"] - was) > START_SLACK:
        raise Refused("pid %d now belongs to a newer process" % pid)
    mine = set(_tree(os.getpid(), listing, kids, strict=False))
    if pid in mine:
        raise Refused("that is cADRe itself")
    tree = _tree(pid, listing, kids)
    return p, [x for x in tree if x not in mine], [x for x in tree if x in mine]


def _count(n):
    return "" if n <= 0 else " and %d child process%s" % (n, "" if n == 1 else "es")


def _hold(pid, now):
    """Stop the agent's ancestors on its terminal, outermost first (so none sees its child
    stop). -> [[pid, start time]] of those stopped, nearest first. One that can't be stopped
    (login runs as root) is skipped: it isn't the one that takes the terminal."""
    held = []
    for hp, up in reversed(procs.terminal_parents(pid)):
        if hp == os.getpid():
            continue
        try:
            procs.suspend(hp)
            held.insert(0, [hp, int(now - up)])
        except OSError:
            pass
    return held


def _scope_for(pid, tree):
    """Linux: (cgroup, other pids in it) when freezing the agent's terminal tab / tmux pane is
    the way to pause it: cADRe isn't in there, and every process of the agent is."""
    cg = procs.terminal_scope(pid) if procs.IS_LINUX else None
    if not cg:
        return None
    members = procs.cgroup_pids(cg)
    if not members or os.getpid() in members or any(x not in members for x in tree):
        return None
    return cg, [x for x in members if x not in tree]


def _names(pids):
    listing = procs.list_processes()
    out = []
    for x in pids:
        r = listing.get(x) or {}
        argv = (r.get("args") or "").split()
        out.append([x, os.path.basename(r.get("exe") or (argv[0] if argv else "") or "?")])
    return out


def _thaw(pid):
    """Thaw the scope frozen for this agent, if any. -> True if there was one."""
    cg = _scopes.get(pid)
    if not cg:
        return False
    if procs.is_frozen(cg):
        procs.freeze(cg, False)
    else:
        procs._frozen.discard(cg)
    _scopes.pop(pid, None)
    return True


def _still_held(held):
    """Are all of these still stopped a moment after the pause?"""
    time.sleep(HOLD_CHECK)
    listing = procs.list_processes()
    return all("T" in ((listing.get(hp) or {}).get("stat") or "") for hp, _ in held)


def _release(pid, listing=None, now=None):
    """Let go of what _hold() stopped for this agent, nearest first. Each pid must still be
    that process (same start time) and still be stopped. -> the pids continued."""
    held = _held.pop(pid, None)
    if not held:
        return []
    now = now or time.time()
    listing = procs.list_processes() if listing is None else listing
    done = []
    for hp, started in held:
        rec = listing.get(hp)
        if not rec or "T" not in (rec.get("stat") or ""):
            continue
        if rec.get("uptime_s") and started and abs(now - rec["uptime_s"] - started) > START_SLACK:
            continue
        try:
            procs.resume(hp)
            done.append(hp)
        except OSError:
            pass
    return done


def release_orphans(listing, now=None):
    """An agent that ended while paused (killed from a terminal, a crash) leaves its shell
    stopped and no Resume button to press: continue the shell. Called with every snapshot's
    process list; the only signal cADRe ever sends unasked, and only SIGCONT to what it
    stopped itself. Logged like a resume."""
    _paused_map()
    # not alive() rather than "not listed": its stopped shell can't collect it, so it stays a zombie
    for pid in [x for x in set(_held) | set(_scopes) if x not in listing or not procs.alive(x)]:
        done = _release(pid, listing, now)
        try:
            if _thaw(pid):
                done.append(pid)
        except OSError:
            continue                            # try again with the next snapshot
        _paused.pop(pid, None)
        _front.discard(pid)
        try:
            audit.append({"ts": now or time.time(), "action": "resume", "source": "cadre", "ok": True,
                          "pid": pid, "pids": done, "agent": "agent",
                          "result": "pid %d ended while paused; its terminal was let go" % pid})
        except OSError:
            pass


def _do(action, pid, p, tree, entry):
    began = time.time()
    _paused_map()                                   # after a restart: rebuilds _held / _scopes from the log
    stat = p.get("stat") or ""
    suspended = "T" in stat or paused_by_us(pid, p.get("started"), stat)
    if action == "pause":
        if suspended:
            raise Refused("it is already suspended")
        _paused_map()
        front = procs.foreground(pid)               # asked before: afterwards the shell has it
        scope = _scope_for(pid, tree) if front else None
        if scope:
            try:
                procs.freeze(scope[0])
            except OSError:
                try:
                    procs.freeze(scope[0], False)
                except OSError:
                    pass
                scope = None
        if scope:
            _scopes[pid] = entry["frozen"] = scope[0]
            _paused[pid] = p.get("started")
            _front.discard(pid)
            shells = {hp for hp, _ in procs.terminal_parents(pid)} | {p.get("ppid")}
            extra = [x for x in scope[1] if x not in shells]
            text = "paused pid %d%s; its terminal stands still until you resume it" % (pid, _count(len(tree) - 1))
            if extra:
                entry["also_frozen"] = _names(extra)
                text += "; also frozen in that terminal: " + ", ".join(
                    "%s (%d)" % (n, x) for x, n in entry["also_frozen"][:5])
            return list(tree), [], text
        held = _hold(pid, entry["ts"]) if front else []
        if held:
            _held[pid] = held
        try:
            done, failed = _apply(procs.suspend, tree)
        except OSError:
            _release(pid, now=entry["ts"])
            raise
        if held and not _still_held(held):
            # tmux continues a pane's shell the moment it stops (server_child_stopped), so the
            # shell saw its job stop and took the terminal: say so, and Resume says to type fg
            _release(pid, now=entry["ts"])
            held, entry["shell_woken"] = [], True
        _paused[pid] = p.get("started")
        text = "paused pid %d%s" % (pid, _count(len(done) - 1))
        _front.discard(pid)
        if held:
            entry["held"] = held
            text += "; its terminal stands still until you resume it"
        elif front:
            _front.add(pid)
            entry["foreground"] = True
            text += "; its terminal is back at the shell prompt"
        return done, failed, text
    if action == "resume" and pid in _scopes:
        _thaw(pid)
        _paused_map().pop(pid, None)
        return list(tree), [], "resumed pid %d%s" % (pid, _count(len(tree) - 1))
    if action == "resume":
        done, failed = _apply(procs.resume, tree)
        _paused_map().pop(pid, None)
        _release(pid, now=entry["ts"])              # after the agent: the shell finds it running
        text = "resumed pid %d%s" % (pid, _count(len(done) - 1))
        if needs_fg(pid):
            entry["needs_fg"] = True
            text += "; " + FG_HINT
        return done, failed, text
    # kill: a suspended process can't act on SIGTERM, so it isn't asked
    done, failed = _apply(lambda x: procs.terminate(x, force=suspended), tree)
    left, deadline = list(done), time.time() + GRACE
    while True:
        left = [x for x in left if procs.alive(x)]
        if not left or time.time() >= deadline:
            break
        time.sleep(0.1)
    for x in left:
        try:
            procs.terminate(x, force=True)
        except OSError:
            pass
    _paused_map().pop(pid, None)
    _front.discard(pid)
    _release(pid, now=entry["ts"] + time.time() - began)     # the wait above took time
    _thaw(pid)                                  # after the kill: the shell collects it, then goes on
    return done, failed, "ended pid %d%s%s" % (pid, _count(len(done) - 1),
                                              " (%d by force)" % len(left) if left else "")


def act(snap, agent_id, action, confirm=False, source="dashboard", now=None):
    """Run one action on one agent of `snap` and log it. Returns the log entry (`ok`, `result`).
    ValueError = a request that makes no sense (nothing is done or logged)."""
    now = now or time.time()
    if action not in ACTIONS:
        raise ValueError("action must be one of: %s" % ", ".join(ACTIONS))
    rec = next((a for a in (snap or {}).get("agents") or [] if a.get("id") == agent_id), None)
    if not rec:
        raise ValueError("no such agent: %s" % agent_id)
    if not rec.get("pid"):
        raise ValueError("%s has no running process" % agent_id)
    if action == "kill" and confirm is not True:
        raise ValueError('kill needs "confirm": true')
    pid = rec["pid"]
    entry = {"ts": now, "action": action, "source": source, "ok": False, "result": "",
             "agent_id": agent_id, "agent": rec.get("display_name") or rec.get("agent"),
             "agent_key": rec.get("agent_key"), "project": rec.get("project"),
             "session_id": rec.get("session_id"), "pid": pid, "pids": [], "started": None,
             "flags": list(rec.get("flags") or [])[:5]}
    try:
        p, tree, skipped = _target(snap, rec, now)
        entry["started"] = int(p["started"]) if p.get("started") else None
        done, failed, text = _do(action, pid, p, tree, entry)
        if failed:
            text += "; %d child process%s couldn't be reached" % (len(failed), "" if len(failed) == 1 else "es")
        if skipped:
            text += "; cADRe runs under this agent and was left alone"
        entry.update(ok=True, pids=done, result=text)
    except (Refused, OSError) as e:
        entry["result"] = str(e) or type(e).__name__
    try:
        audit.append(entry)
    except OSError:
        entry["result"] += " (not logged: the response log can't be written)"
    return entry
