"""Shared harness for artifact-receipts integration tests.

Builds tiny wheels by hand (full control over RECORD and zip quirks),
installs them by copying files into a directory that acts as a
site-packages stand-in, and runs the tool in a subprocess so every
scenario gets a clean interpreter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INIT_A = b'"""demopkg."""\nVALUE = 1\n'
SUB_A = b"def value():\n    return 1\n"

# Same byte length as INIT_A/SUB_A but different content (for stale-pyc and
# same-size tampering controls).
INIT_B = b'"""demopkg."""\nVALUE = 2\n'
SUB_B = b"def value():\n    return 2\n"

assert len(INIT_A) == len(INIT_B)
assert len(SUB_A) == len(SUB_B)

METADATA = b"Metadata-Version: 2.1\nName: demopkg\nVersion: 1.0\n"
WHEEL_MD = (b"Wheel-Version: 1.0\nGenerator: harness\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n")


def b64_sha256(data: bytes) -> str:
    d = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode("ascii")


def build_wheel(path, members: dict, dist_info="demopkg-1.0.dist-info",
                record_rows=None, skip_record=False):
    """members: arcname -> bytes. record_rows: prebuilt RECORD lines override."""
    path = str(path)
    with zipfile.ZipFile(path, "w") as z:
        for arc, data in members.items():
            z.writestr(arc, data)
        if not skip_record:
            if record_rows is None:
                record_rows = []
                for arc, data in members.items():
                    record_rows.append(
                        "%s,sha256=%s,%d" % (arc, b64_sha256(data), len(data))
                    )
            record_rows.append("%s/RECORD,," % dist_info)
            z.writestr("%s/RECORD" % dist_info,
                       ("\n".join(record_rows) + "\n").encode("utf-8"))
    return path


def standard_members(init=INIT_A, sub=SUB_A):
    return {
        "demopkg/__init__.py": init,
        "demopkg/sub.py": sub,
        "demopkg-1.0.dist-info/METADATA": METADATA,
        "demopkg-1.0.dist-info/WHEEL": WHEEL_MD,
    }


def install_to(directory, init=INIT_A, sub=SUB_A):
    pkg = os.path.join(str(directory), "demopkg")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "wb") as f:
        f.write(init)
    with open(os.path.join(pkg, "sub.py"), "wb") as f:
        f.write(sub)
    return pkg


TEST_PASS = "import demopkg, demopkg.sub\n\ndef test_ok():\n    assert demopkg.sub.value() in (1, 2)\n"
TEST_FAIL = "import demopkg\n\ndef test_bad():\n    assert False\n"
TEST_NOIMPORT = "def test_unrelated():\n    assert True\n"


def write_test(directory, source=TEST_PASS):
    directory = str(directory)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "test_sample.py"), "w") as f:
        f.write(source)
    return directory


def run_tool(tmp_path, wheel, packages=("demopkg",), testdir=None,
             pythonpath=(), extra_env=None, pytest_args=(),
             json_report=True, preload=None, human_report_path=None):
    """Run the CLI in a subprocess. Returns (exit_code, report_dict_or_None,
    stdout+stderr)."""
    env = dict(os.environ)
    pp = [REPO_ROOT] + [str(p) for p in pythonpath]
    env["PYTHONPATH"] = os.pathsep.join(pp)
    if extra_env:
        env.update(extra_env)
    json_path = None
    if json_report is True:
        json_path = os.path.join(str(tmp_path), "report.json")
    elif isinstance(json_report, str):
        json_path = json_report

    if preload:
        # Import the named modules before pytest runs (sys.modules hit).
        script = (
            "import sys; sys.path.insert(0, %r); import %s; "
            "from artifact_receipts.cli import main; "
            "raise SystemExit(main(sys.argv[1:]))"
        ) % (str(pythonpath[0]) if pythonpath else "", preload)
        cmd = [sys.executable, "-c", script, "run"]
    else:
        cmd = [sys.executable, "-m", "artifact_receipts", "run"]
    cmd += ["--wheel", str(wheel)]
    for p in packages:
        cmd += ["--package", p]
    if json_path:
        cmd += ["--json-report", json_path]
    if human_report_path:
        cmd += ["--human-report", str(human_report_path)]
    cmd += ["--"]
    if testdir:
        cmd += [str(testdir)]
    cmd += list(pytest_args)

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(tmp_path))
    report = None
    if json_path and os.path.isfile(json_path):
        with open(json_path) as f:
            report = json.load(f)
    return proc.returncode, report, proc.stdout + proc.stderr


def make_stale_pyc(pkg_dir, new_init):
    """Replace __init__.py bytes with new_init while keeping size and mtime,
    leaving the previously generated pyc looking valid to the timestamp
    check. Returns True if a pyc existed to spoof."""
    init_path = os.path.join(str(pkg_dir), "__init__.py")
    st = os.stat(init_path)
    with open(init_path, "wb") as f:
        f.write(new_init)
    os.utime(init_path, (st.st_atime, st.st_mtime))
    return True
