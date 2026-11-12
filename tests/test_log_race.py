"""The event log under concurrent writers: no hook may lose or damage another hook's line.

On Windows `O_APPEND` is a seek followed by a write, so two hooks firing together could
overwrite each other; both append functions (cadre/hooks/emit.py and cadre/events.py) take a
short lock there. Everywhere, two writers that see a full log at once must not both rotate it
(the second would move the fresh log over the first one's .1), so rotation takes the lock too. The lock must never stall an agent: a held lock delays a write by a capped
time, then the event is written anyway.
"""
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from cadre import events

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMIT = os.path.join(ROOT, "cadre", "hooks", "emit.py")
_spec = importlib.util.spec_from_file_location("cadre_emit_race", EMIT)
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

# One writer process: waits for the shared start time, then appends `count` events as fast as it
# can, through emit.py's append (argv[1] == "emit") or the package's.
WORKER = r"""
import importlib.util, os, sys, time
mode, root, log, who, count, start, max_bytes = sys.argv[1:8]
sys.path.insert(0, root)
if mode == "emit":
    spec = importlib.util.spec_from_file_location("emit_worker", os.path.join(root, "cadre", "hooks", "emit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    write = mod.append
else:
    from cadre import events as mod
    write = lambda ev: mod.append(ev, log)
if int(max_bytes):
    mod.LOG_MAX_BYTES = int(max_bytes)
time.sleep(max(0.0, float(start) - time.time()))
for i in range(int(count)):
    write({"ts": time.time(), "agent": "race-test", "event": "PostToolUse", "session_id": who,
           "tool": "Bash", "n": i, "detail": "x" * (20 + (i * 37 + int(who)) % 240)})
"""


class LogCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cadre-race-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.log = os.path.join(self.home, "events.jsonl")

    def lines(self, path=None):
        try:
            with open(path or self.log, "rb") as f:
                return f.read().splitlines()
        except OSError:
            return []


class ConcurrentWriters(LogCase):
    WRITERS, EACH = 20, 150

    def hammer(self, max_bytes=0):
        env = dict(os.environ, CADRE_HOME=self.home)
        env.pop("CADRE_URL", None)
        start = time.time() + 1.5                  # every interpreter is up by then
        procs = [subprocess.Popen([sys.executable, "-c", WORKER, "emit" if w % 2 else "events", ROOT, self.log,
                                   str(w), str(self.EACH), repr(start), str(max_bytes)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for w in range(self.WRITERS)]
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual((p.returncode, out, err), (0, b"", b""))

    def check(self, lines):
        seen = set()
        for line in lines:
            ev = json.loads(line.decode("utf-8"))      # a damaged line fails here
            seen.add((ev["session_id"], ev["n"]))
        self.assertEqual(len(lines), self.WRITERS * self.EACH)
        self.assertEqual(len(seen), self.WRITERS * self.EACH)

    def test_every_line_survives(self):
        self.hammer()
        self.check(self.lines())

    def test_rotation_loses_nothing(self):
        # about 1.6x the limit gets written, so the log rotates exactly once: every line must be
        # in one of the two files
        total = self.WRITERS * self.EACH * 250
        self.hammer(max_bytes=int(total / 1.6))
        rotated = self.lines(self.log + ".1")
        self.assertTrue(rotated, "the log never rotated")
        self.check(rotated + self.lines())


@unittest.skipUnless(os.name == "nt", "only Windows takes the lock")
class HeldLock(LogCase):
    """Somebody else holds the lock (a stuck hook, say): the write waits a little, then goes ahead."""

    def setUp(self):
        super().setUp()
        import msvcrt
        self.fd = os.open(self.log + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        self.addCleanup(os.close, self.fd)

    def timed(self, write):
        t = time.monotonic()
        write()
        return time.monotonic() - t

    def test_package_append_waits_then_writes(self):
        took = self.timed(lambda: events.append({"event": "Stop", "session_id": "s"}, self.log))
        self.assertGreaterEqual(took, events.LOCK_WAIT * 0.8)
        self.assertLess(took, events.LOCK_WAIT + 0.4)
        self.assertEqual(len(self.lines()), 1)

    def test_hook_append_waits_then_writes(self):
        old = emit.LOG_DIR, emit.LOG_FILE
        emit.LOG_DIR, emit.LOG_FILE = self.home, self.log
        self.addCleanup(lambda: setattr(emit, "LOG_DIR", old[0]) or setattr(emit, "LOG_FILE", old[1]))
        took = self.timed(lambda: emit.append({"event": "Stop", "session_id": "s"}))
        self.assertGreaterEqual(took, emit.LOCK_WAIT * 0.8)
        self.assertLess(took, emit.LOCK_WAIT + 0.4)
        self.assertEqual(len(self.lines()), 1)


class TwoCopies(unittest.TestCase):
    """emit.py can't import the package, so the lock exists twice: the copies must stay identical."""

    def test_same_code_and_a_small_cap(self):
        for fn in ("_lock", "_unlock", "_full"):
            self.assertEqual(inspect.getsource(getattr(emit, fn)), inspect.getsource(getattr(events, fn)))
        self.assertLessEqual(emit.LOCK_WAIT, 0.1)
        self.assertEqual(emit.LOCK_WAIT, events.LOCK_WAIT)


if __name__ == "__main__":
    unittest.main()
