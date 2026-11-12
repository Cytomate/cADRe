"""Cross-platform process access: list processes, their working dirs and selected env vars.

Backends, best first:
  * psutil (optional, any OS; recommended on Windows: gives CPU, cwd and environment)
  * Linux:   ps + /proc/<pid>/cwd + /proc/<pid>/environ
  * macOS:   ps + lsof + `ps -E`
  * Windows: PowerShell Get-CimInstance Win32_Process (no cwd / environment without psutil)

Every process is a dict: pid, ppid, stat ("T" = stopped), cpu (%), rss_mb, uptime_s, exe, args.
CPU % is computed from CPU-time deltas between calls where the OS doesn't report it, so the
very first snapshot on those backends shows 0%.

Everything above only reads. The last section (suspend / resume / terminate / alive) is what
the user's Pause, Resume and Kill buttons end up calling, through cadre/respond.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

def _load_psutil(site=None, name="psutil"):
    """psutil from this Python, else the copy `cadre setup` put in the install folder
    (paths.SITE; Windows, where the fallback can't read a process's folder), else None."""
    import importlib
    try:
        return importlib.import_module(name)
    except ImportError:
        pass
    if site is None:
        from . import paths
        site = paths.SITE
    if os.path.isdir(os.path.join(site, name)):
        sys.path.append(site)                  # last: it must never shadow anything
        try:
            return importlib.import_module(name)
        except Exception:                      # built for another Python or CPU
            sys.path.remove(site)
            for m in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
                del sys.modules[m]
    return None


psutil = _load_psutil()

# what the dashboard says when the backend can't see everything (None = nothing is missing)
LIMITED_WIN = ("psutil is missing: cADRe can't read the folder or settings of a running agent, so "
               "projects and sessions of live agents may be missing. Run `py -m cadre setup` again "
               "(it fetches psutil), or `py -m pip install psutil`, then restart the dashboard.")


def limited():
    return LIMITED_WIN if IS_WIN and not psutil else None

_prev_cpu = {}          # pid -> (wall time, cpu seconds) for delta-based CPU %
NCPU = os.cpu_count() or 1


def backend():
    if psutil:
        return "psutil"
    return "windows-cim" if IS_WIN else "proc" if IS_LINUX else "ps"


def _run(cmd, timeout=10):
    try:
        kw = {"creationflags": 0x08000000} if IS_WIN else {}   # CREATE_NO_WINDOW
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", **kw).stdout or ""
    except Exception:
        return ""


def _cpu_pct(pid, cpu_seconds, now):
    prev = _prev_cpu.get(pid)
    _prev_cpu[pid] = (now, cpu_seconds)
    if not prev or now <= prev[0]:
        return 0.0
    return round(max(0.0, (cpu_seconds - prev[1]) / (now - prev[0]) * 100), 1)


# ------------------------------------------------------------------ list
def list_processes():
    """Return {pid: proc dict} for every process visible to this user."""
    if psutil:
        procs = _list_psutil()
    elif IS_WIN:
        procs = _list_windows()
    else:
        procs = _list_ps()
    if _frozen:
        _mark_frozen(procs)
    return procs


# On Windows, psutil's status() walks every thread of a process (that is how it detects
# "suspended"), and cpu_times(), memory_info() and create_time() fall back to a slow call
# for processes we may not fully inspect: together ~1.3 s per snapshot for ~250 processes.
# There they are read only for the pids that matter, via fill_details().
_LAZY = IS_WIN
_DETAILS = ["status", "cpu_times", "memory_info", "create_time"]


def _details(rec, i, now):
    """Set stat / cpu / rss_mb / uptime_s on a proc dict from psutil values in `i`."""
    ct, mem = i.get("cpu_times"), i.get("memory_info")
    rec["stat"] = "T" if i.get("status") == getattr(psutil, "STATUS_STOPPED", "stopped") else "S"
    rec["cpu"] = _cpu_pct(rec["pid"], (ct.user + ct.system) if ct else 0.0, now)
    rec["rss_mb"] = round(mem.rss / 1048576, 1) if mem else 0.0
    rec["uptime_s"] = int(now - i["create_time"]) if i.get("create_time") else 0


def _list_psutil():
    now, procs = time.time(), {}
    attrs = ["pid", "ppid", "name", "exe", "cmdline"] + ([] if _LAZY else _DETAILS)
    for p in psutil.process_iter(attrs, ad_value=None):
        i = p.info
        cmd = i.get("cmdline") or []
        rec = procs[i["pid"]] = {
            "pid": i["pid"], "ppid": i.get("ppid") or 0,
            "stat": "S", "cpu": 0.0, "rss_mb": 0.0, "uptime_s": 0,
            # argv[0] = the name it was started as, like `ps comm` (exe() resolves symlinks,
            # e.g. claude -> ~/.local/share/claude/versions/2.1.319)
            "exe": (cmd[0] if cmd else None) or i.get("exe") or i.get("name") or "",
            "args": " ".join(cmd) if cmd else (i.get("name") or ""),
        }
        if not _LAZY:
            _details(rec, i, now)
    return procs


def fill_details(procs, pids):
    """Fill in stat / cpu / rss_mb / uptime_s for these pids where list_processes() skipped
    them (see _LAZY). A no-op on backends that already read them for every process."""
    if not (psutil and _LAZY):
        return
    now = time.time()
    for pid in pids:
        rec = procs.get(pid)
        if not rec:
            continue
        try:
            i = psutil.Process(pid).as_dict(_DETAILS, ad_value=None)
        except Exception:
            continue
        _details(rec, i, now)


def _etime_to_secs(s):
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def _list_ps():
    """macOS / Linux. Two ps calls because 'comm' may contain spaces."""
    procs = {}
    out = _run(["ps", "-axo", "pid=,ppid=,stat=,pcpu=,rss=,etime=,comm="])
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, stat, cpu, rss, etime, comm = parts
        try:
            procs[int(pid)] = {
                "pid": int(pid), "ppid": int(ppid), "stat": stat,
                "cpu": float(cpu), "rss_mb": round(int(rss) / 1024, 1),
                "uptime_s": _etime_to_secs(etime), "exe": comm.strip(), "args": "",
            }
        except ValueError:
            continue
    out = _run(["ps", "-axww", "-o", "pid=,args="])
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) in procs:
            procs[int(parts[0])]["args"] = parts[1]
    return procs


WIN_PS = (
    "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,"
    "CommandLine,WorkingSetSize,KernelModeTime,UserModeTime,"
    "@{n='Start';e={if($_.CreationDate){[int64](($_.CreationDate.ToUniversalTime())-"
    "[datetime]'1970-01-01').TotalSeconds}}} | ConvertTo-Json -Compress"
)


def parse_windows(text, now=None):
    """Parse the JSON from WIN_PS into proc dicts (separate so it can be tested anywhere)."""
    now = now or time.time()
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    procs = {}
    for r in rows:
        try:
            pid = int(r.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        cpu_s = ((r.get("KernelModeTime") or 0) + (r.get("UserModeTime") or 0)) / 1e7  # 100ns units
        start = r.get("Start")
        procs[pid] = {
            "pid": pid, "ppid": int(r.get("ParentProcessId") or 0), "stat": "S",
            "cpu": _cpu_pct(pid, cpu_s, now),
            "rss_mb": round((r.get("WorkingSetSize") or 0) / 1048576, 1),
            "uptime_s": int(now - start) if start else 0,
            "exe": r.get("ExecutablePath") or r.get("Name") or "",
            "args": r.get("CommandLine") or r.get("Name") or "",
        }
    return procs


def _list_windows():
    out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WIN_PS], timeout=20)
    return parse_windows(out)


# ------------------------------------------------------------------ cwd / env
def proc_cwds(pids):
    """{pid: working directory} where the OS lets us read it."""
    pids = list(pids)
    if not pids:
        return {}
    if psutil:
        out = {}
        for pid in pids:
            try:
                out[pid] = psutil.Process(pid).cwd()
            except Exception:
                pass
        return out
    if IS_LINUX:
        out = {}
        for pid in pids:
            try:
                out[pid] = os.readlink("/proc/%d/cwd" % pid)
            except OSError:
                pass
        return out
    if IS_MAC:
        out = _run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(str(p) for p in pids)])
        cwds, cur = {}, None
        for line in out.splitlines():
            if line.startswith("p"):
                cur = int(line[1:])
            elif line.startswith("n") and cur is not None:
                cwds[cur] = line[1:]
        return cwds
    return {}   # Windows without psutil: not available


def proc_env(pid, names):
    """Read selected environment variables of a process we own."""
    env = {}
    if psutil:
        try:
            e = psutil.Process(pid).environ()
            return {n: e[n] for n in names if e.get(n)}
        except Exception:
            return {}
    if IS_LINUX:
        try:
            with open("/proc/%d/environ" % pid, "rb") as f:
                pairs = f.read().decode("utf-8", "replace").split("\0")
            e = dict(p.split("=", 1) for p in pairs if "=" in p)
            return {n: e[n] for n in names if e.get(n)}
        except OSError:
            return {}
    if IS_MAC:
        out = _run(["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)])
        for n in names:
            m = re.search(r"(?:^|\s)%s=(\S+)" % n, out)
            if m:
                env[n] = m.group(1)
    return env


# ------------------------------------------------------------------ suspend / resume / end
# The only functions here that change a process. Called by cadre/respond/actions.py alone,
# which first checks that the pid is still the agent the user picked. Each one acts on a
# single pid and raises OSError when it can't.
def _os_error(e):
    return e if isinstance(e, OSError) else OSError(str(e) or type(e).__name__)


def _win_handle(pid, access):
    import ctypes
    from ctypes import wintypes as W
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype, k.OpenProcess.argtypes = W.HANDLE, (W.DWORD, W.BOOL, W.DWORD)
    k.CloseHandle.restype, k.CloseHandle.argtypes = W.BOOL, (W.HANDLE,)
    k.WaitForSingleObject.restype, k.WaitForSingleObject.argtypes = W.DWORD, (W.HANDLE, W.DWORD)
    h = k.OpenProcess(access, False, pid)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return k, h


def _nt_process(pid, call):
    """Windows without psutil: NtSuspendProcess / NtResumeProcess (what psutil calls too).
    Suspensions count: a process suspended twice needs two resumes."""
    import ctypes
    from ctypes import wintypes as W
    k, h = _win_handle(pid, 0x0800)                 # PROCESS_SUSPEND_RESUME
    try:
        f = getattr(ctypes.WinDLL("ntdll"), call)
        f.restype, f.argtypes = ctypes.c_long, (W.HANDLE,)
        status = f(h)
        if status < 0:
            raise OSError("%s failed (0x%08X)" % (call, status & 0xFFFFFFFF))
    finally:
        k.CloseHandle(h)


def suspend(pid):
    """Stop a process where it is (SIGSTOP; on Windows every thread is suspended)."""
    try:
        if psutil:
            psutil.Process(pid).suspend()
        elif IS_WIN:
            _nt_process(pid, "NtSuspendProcess")
        else:
            os.kill(pid, signal.SIGSTOP)
    except Exception as e:
        raise _os_error(e)


def resume(pid):
    try:
        if psutil:
            psutil.Process(pid).resume()
        elif IS_WIN:
            _nt_process(pid, "NtResumeProcess")
        else:
            os.kill(pid, signal.SIGCONT)
    except Exception as e:
        raise _os_error(e)


def terminate(pid, force=False):
    """Ask a process to exit (SIGTERM); force = it gets no say (SIGKILL). On Windows both
    are TerminateProcess."""
    try:
        if psutil:
            p = psutil.Process(pid)
            p.kill() if force else p.terminate()
        else:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM)
    except Exception as e:
        raise _os_error(e)


# ------------------------------------------------------------------ Linux: frozen scopes
# A terminal tab (GNOME Terminal / VTE, Ptyxis) or a tmux pane has a systemd scope of its own:
# its shell, the agent and the agent's children. Freezing that cgroup (cgroup v2) pauses the
# agent without a signal, so neither the shell nor tmux sees a stopped job and the terminal
# just stands still; SIGSTOP can't do that (bash and tmux react to it). cadre/respond decides
# when to use it. Processes in a scope cADRe froze are listed with stat "T", like stopped ones.
CGROOT = "/sys/fs/cgroup"
TERMINAL_SCOPE = re.compile(r"^(vte-spawn|ptyxis-spawn|tmux-spawn)-[0-9a-fA-F-]+\.scope$")
FREEZE_WAIT = 2.0          # s for the kernel to report a cgroup frozen / thawed
_frozen = set()            # cgroups (paths below CGROOT) cADRe froze and hasn't thawed


def cgroup(pid):
    """The cgroup v2 path of a process ("/user.slice/.../vte-spawn-….scope"), or None."""
    if not IS_LINUX:
        return None
    try:
        with open("/proc/%d/cgroup" % int(pid), encoding="utf-8") as f:
            for line in f:
                if line.startswith("0::"):
                    return line[3:].strip() or None
    except OSError:
        pass
    return None


def terminal_scope(pid):
    """The cgroup of the terminal tab or tmux pane this process runs in, if it has one."""
    cg = cgroup(pid)
    return cg if cg and TERMINAL_SCOPE.match(os.path.basename(cg)) else None


def _cg_file(cg, name):
    path = os.path.normpath(os.path.join(CGROOT, cg.lstrip("/"), name))
    if not path.startswith(CGROOT + os.sep):
        raise OSError("not a cgroup: %s" % cg)
    return path


def cgroup_pids(cg):
    try:
        with open(_cg_file(cg, "cgroup.procs"), encoding="utf-8") as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


def is_frozen(cg):
    try:
        with open(_cg_file(cg, "cgroup.events"), encoding="utf-8") as f:
            return any(line.split() == ["frozen", "1"] for line in f)
    except OSError:
        return False


def freeze(cg, on=True):
    """Freeze (on) or thaw a cgroup and wait until the kernel says it is done. OSError if it
    can't be written or doesn't get there in FREEZE_WAIT seconds."""
    with open(_cg_file(cg, "cgroup.freeze"), "w", encoding="utf-8") as f:
        f.write("1" if on else "0")
    deadline = time.monotonic() + FREEZE_WAIT
    while is_frozen(cg) != on:
        if time.monotonic() >= deadline:
            raise OSError("%s didn't %s" % (os.path.basename(cg), "freeze" if on else "thaw"))
        time.sleep(0.01)
    (_frozen.add if on else _frozen.discard)(cg)


def remember_frozen(cg):
    """A scope frozen before a restart (from the response log): list its processes as stopped."""
    if cg and is_frozen(cg):
        _frozen.add(cg)


def _mark_frozen(procs):
    for cg in list(_frozen):
        if not is_frozen(cg):              # thawed by someone else, or the tab is gone
            _frozen.discard(cg)
            continue
        for pid in cgroup_pids(cg):
            rec = procs.get(pid)
            if rec:
                rec["stat"] = "T" + "".join(c for c in rec.get("stat") or "" if c not in "RSDITtZ")


def sees_stopped():
    """Does `stat` in a proc dict tell a stopped process ("T") from a running one? On macOS and
    Linux, yes. Not on Windows: the CIM backend can't see it, and psutil's answer there is
    "every thread is suspended", which the count of suspensions must not hang on."""
    return not IS_WIN


def foreground(pid):
    """Is this process the job its terminal listens to (the `+` in ps's stat)? macOS / Linux.
    False when it has no terminal, runs in the background, or the OS doesn't say."""
    if IS_WIN:
        return False
    return "+" in _run(["ps", "-o", "stat=", "-p", str(int(pid))], timeout=5)


def terminal_parents(pid):
    """macOS / Linux: [(pid, uptime_s)] of the ancestors of `pid` that sit on its terminal (the
    shell it was started from, a wrapper script, login), nearest first, when `pid` is that
    terminal's foreground job; [] otherwise. They are what would take the terminal away from
    a stopped agent, so cadre/respond stops them with it."""
    if IS_WIN:
        return []
    rows = {}
    for line in _run(["ps", "-axo", "pid=,ppid=,tty=,stat=,etime="]).splitlines():
        parts = line.split()
        try:
            rows[int(parts[0])] = (int(parts[1]), parts[2], parts[3], _etime_to_secs(parts[4]))
        except (IndexError, ValueError):
            continue
    me = rows.get(pid)
    if not me or "+" not in me[2] or me[1] in ("?", "??", "-"):
        return []
    out, cur = [], me[0]
    while cur > 1 and cur in rows and rows[cur][1] == me[1] and len(out) < 8:
        out.append((cur, rows[cur][3]))
        cur = rows[cur][0]
    return out


def alive(pid):
    """Is this pid a running process? (Never os.kill(pid, 0) on Windows: there it ends it.)"""
    if psutil:
        try:
            return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False
    if IS_WIN:
        try:
            k, h = _win_handle(pid, 0x00100000 | 0x1000)    # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        except OSError as e:
            return getattr(e, "winerror", None) == 5         # access denied: it exists
        try:
            return k.WaitForSingleObject(h, 0) == 0x102      # WAIT_TIMEOUT: not exited
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    # a zombie still takes signals: it has exited, its parent just hasn't asked yet. Without
    # this, Kill waited out its grace period and reported "by force" for a process long gone.
    return "Z" not in _run(["ps", "-o", "stat=", "-p", str(int(pid))], timeout=5)
