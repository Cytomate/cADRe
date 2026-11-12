"""cADRe tests (stdlib unittest): `python3 -m unittest` from the repo root (Windows: `py -m unittest`).

Nothing here touches the real machine's agent configs or ~/.cadre: CADRE_HOME is pointed at a
scratch folder before any cadre module is imported, and every installer test edits files in a
temporary directory.
"""
import os
import tempfile

_SCRATCH = tempfile.mkdtemp(prefix="cadre-tests-")
os.environ["CADRE_HOME"] = os.path.join(_SCRATCH, "cadre-home")
os.environ.pop("CADRE_URL", None)
