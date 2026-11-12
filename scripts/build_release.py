#!/usr/bin/env python3
"""Build a cADRe release for Windows: a per-user installer that brings its own Python.

    py scripts\\build_release.py                    # installer + zip in dist\\
    py scripts\\build_release.py --no-installer     # stage + zip only (no Inno Setup needed)
    py scripts\\build_release.py --python 3.14.6 --psutil 7.2.2

Steps (work files in build\\windows\\, results in dist\\):
  1. python-<ver>-embed-amd64.zip from python.org (cached in build\\cache\\), checked against
     the SHA-256 recorded in packaging\\windows\\python-embed.sha256. A version that isn't
     recorded yet stops the build: compare the printed hash with the one on python.org's
     release page, then add it to that file (or pass --sha256 once).
  2. unpack it to stage\\python\\ and add Lib\\site-packages to its ._pth file,
  3. pip-install psutil (binary wheel for win_amd64 and that Python) into Lib\\site-packages,
  4. copy the cadre package (no __pycache__) and packaging\\windows\\cadre_release.py to
     stage\\release\\,
  5. smoke-test the stage with its own python.exe: imports, the unit tests, and emit.py
     writing one event into a scratch CADRE_HOME,
  6. zip the stage to dist\\cADRe-<version>-windows-x64.zip,
  7. compile packaging\\windows\\cadre.iss with Inno Setup 6 (ISCC.exe) into
     dist\\cADRe-<version>-windows-x64-setup.exe.

The release version is cadre.__version__. Windows only; standard library only (pip is used
to fetch psutil).
"""
import argparse
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "cadre")
WIN = os.path.join(ROOT, "packaging", "windows")
HASHES = os.path.join(WIN, "python-embed.sha256")
BUILD = os.path.join(ROOT, "build")
CACHE = os.path.join(BUILD, "cache")
STAGE = os.path.join(BUILD, "windows", "stage")
DIST = os.path.join(ROOT, "dist")
DEFAULT_PSUTIL = "7.2.2"
EMBED_URL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"


def step(msg):
    print("\n== " + msg)


def version():
    with open(os.path.join(PKG, "__init__.py"), encoding="utf-8") as f:
        return re.search(r'__version__\s*=\s*"([^"]+)"', f.read()).group(1)


def known_hashes():
    """{python version: sha256} from packaging/windows/python-embed.sha256."""
    out = {}
    try:
        with open(HASHES, encoding="utf-8") as f:
            for line in f:
                parts = line.split("#", 1)[0].split()
                if len(parts) == 2:
                    out[parts[0]] = parts[1].lower()
    except OSError:
        pass
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch_embed(ver, expected):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "python-%s-embed-amd64.zip" % ver)
    if not os.path.isfile(path):
        url = EMBED_URL.format(v=ver)
        print("downloading " + url)
        tmp = path + ".part"
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, path)
    got = sha256(path)
    if not expected:
        sys.exit("SHA-256 of %s is\n  %s\nNo hash recorded for Python %s. Compare it with python.org's release page,\n"
                 "then add the line\n  %s %s\nto %s (or pass --sha256 %s)." % (
                     os.path.basename(path), got, ver, ver, got, os.path.relpath(HASHES, ROOT), got))
    if got != expected.lower():
        os.remove(path)
        sys.exit("SHA-256 mismatch for %s:\n  expected %s\n  got      %s\n(the download was deleted)"
                 % (os.path.basename(path), expected, got))
    print("python %s embeddable: sha256 ok (%s...)" % (ver, got[:16]))
    return path


def make_stage(embed_zip, ver, psutil_ver):
    shutil.rmtree(os.path.join(BUILD, "windows"), ignore_errors=True)
    py = os.path.join(STAGE, "python")
    os.makedirs(py)
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(py)
    pth = glob.glob(os.path.join(py, "python*._pth"))
    if len(pth) != 1:
        sys.exit("expected one python*._pth in the embeddable zip, found %d" % len(pth))
    with open(pth[0], encoding="utf-8") as f:
        lines = f.read().splitlines()
    if "Lib\\site-packages" not in lines:
        lines.insert(lines.index(".") + 1 if "." in lines else len(lines), "Lib\\site-packages")
    with open(pth[0], "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    print("%s: %s" % (os.path.basename(pth[0]), " | ".join(lines)))

    site = os.path.join(py, "Lib", "site-packages")
    major_minor = ".".join(ver.split(".")[:2])
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
           "--no-compile", "--target", site, "--only-binary=:all:", "--platform", "win_amd64",
           "--python-version", major_minor, "--implementation", "cp", "psutil==" + psutil_ver]
    print(" ".join(cmd[1:]))
    subprocess.check_call(cmd)
    for junk in glob.glob(os.path.join(site, "*.dist-info")) + glob.glob(os.path.join(site, "bin")):
        shutil.rmtree(junk, ignore_errors=True)

    rel = os.path.join(STAGE, "release")
    shutil.copytree(PKG, os.path.join(rel, "cadre"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.cadre-bak"))
    shutil.copy2(os.path.join(WIN, "cadre_release.py"), rel)


def smoke_test():
    py = os.path.join(STAGE, "python", "python.exe")
    rel = os.path.join(STAGE, "release")
    env = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(k, None)
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import cadre, psutil, sqlite3, ctypes, winreg, json, urllib.request, unittest\n"
            "from cadre import __main__, collectors, installer, service, tray, paths\n"
            "from cadre.procs import backend\n"
            "print('cadre', cadre.__version__, '| python', sys.version.split()[0], '| psutil', psutil.__version__,"
            " '| backend', backend())\n") % rel
    subprocess.check_call([py, "-c", code], env=env)

    # the repo's unit tests, run by the bundled interpreter
    tests = ("import sys, unittest; sys.path[:0] = [%r]\n"
             "r = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.discover(%r, top_level_dir=%r))\n"
             "sys.exit(0 if r.wasSuccessful() else 1)\n") % (ROOT, os.path.join(ROOT, "tests"), ROOT)
    subprocess.check_call([py, "-c", tests], env=env, cwd=ROOT)

    # a hook exactly as agents run it: python.exe -I -S emit.py, payload on stdin, no output
    with tempfile.TemporaryDirectory(prefix="cadre-smoke-") as home:
        env["CADRE_HOME"] = home
        emit = os.path.join(rel, "cadre", "hooks", "emit.py")
        payload = json.dumps({"session_id": "smoke", "cwd": "C:\\x", "tool_name": "Bash",
                              "tool_input": {"command": "echo hi"}}).encode()
        r = subprocess.run([py, "-I", "-S", emit, "--agent", "claude-code", "--event", "PostToolUse"],
                           input=payload, capture_output=True, env=env, timeout=30)
        with open(os.path.join(home, "events.jsonl"), encoding="utf-8") as f:
            ev = json.loads(f.read().splitlines()[-1])
        if r.returncode or r.stdout or r.stderr or ev.get("tool") != "Bash":
            sys.exit("emit.py smoke test failed: rc=%s out=%r err=%r ev=%r" % (r.returncode, r.stdout, r.stderr, ev))
    print("emit.py: one event written, no output, exit 0")


def find_iscc():
    cands = [shutil.which("iscc")]
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            cands += [os.path.join(base, "Programs", "Inno Setup 6", "ISCC.exe"),
                      os.path.join(base, "Inno Setup 6", "ISCC.exe")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def main():
    if os.name != "nt":
        sys.exit("The Windows release is built on Windows (embeddable Python, Inno Setup).")
    ap = argparse.ArgumentParser(description="Build the cADRe Windows release.")
    ap.add_argument("--python", default=platform.python_version(),
                    help="Python version to bundle (default: this one, %(default)s)")
    ap.add_argument("--psutil", default=DEFAULT_PSUTIL, help="psutil version (default %(default)s)")
    ap.add_argument("--sha256", help="expected SHA-256 of the embeddable zip, if not recorded yet")
    ap.add_argument("--no-installer", action="store_true", help="stage and zip only, skip Inno Setup")
    a = ap.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", a.python):
        sys.exit("--python must be a final release like 3.14.6 (got %s)" % a.python)
    ver = version()
    print("cADRe %s release: Python %s, psutil %s" % (ver, a.python, a.psutil))

    step("1. embeddable Python")
    embed = fetch_embed(a.python, a.sha256 or known_hashes().get(a.python))
    step("2-4. stage")
    make_stage(embed, a.python, a.psutil)
    step("5. smoke test with the bundled Python")
    smoke_test()

    step("6. zip")
    os.makedirs(DIST, exist_ok=True)
    name = "cADRe-%s-windows-x64" % ver
    zpath = shutil.make_archive(os.path.join(DIST, name), "zip", STAGE)
    print(os.path.relpath(zpath, ROOT))

    step("7. installer")
    iscc = None if a.no_installer else find_iscc()
    if not iscc:
        print("skipped: %s" % ("--no-installer" if a.no_installer else
                               "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)"))
        return 0
    # /Qp: quiet but still prints errors (plain /Q hides why a compile failed)
    subprocess.check_call([iscc, "/Qp", "/DAppVersion=" + ver, "/DStage=" + STAGE, "/DOutDir=" + DIST,
                           "/DIcon=" + os.path.join(PKG, "static", "tray.ico"), os.path.join(WIN, "cadre.iss")])
    exe = os.path.join(DIST, name + "-setup.exe")
    print("%s (%.1f MB)" % (os.path.relpath(exe, ROOT), os.path.getsize(exe) / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
