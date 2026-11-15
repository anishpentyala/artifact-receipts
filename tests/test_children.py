"""Fresh-process integration controls for explicitly declared child jobs."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import harness as H

ROOT = Path(__file__).parents[1]


def scenario(tmp_path, child_source, *, parent_extra="", required=("worker",),
             child_path=None, ignore_result=False, timeout=5):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", H.standard_members())
    H.install_to(tmp_path / "site")
    script = tmp_path / "job" / "worker.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(child_source)
    child_env = "None"
    if child_path:
        child_env = "dict(os.environ, PYTHONPATH=%r)" % os.pathsep.join(
            [str(ROOT), str(child_path)])
    code = """import os, subprocess, sys
import demopkg
from artifact_receipts.children import run_observed_python

def test_parent():
    assert demopkg.VALUE == 1
    %s
""" % ("pass" if not child_source else (
        "result = run_observed_python('worker', %r, env=%s, timeout=%r)\n    %s"
        % (str(script), child_env, timeout, "pass" if ignore_result else "assert result.returncode == 0, result.stderr")))
    code += parent_extra
    tests = H.write_test(tmp_path / "tests", code)
    report_path = tmp_path / "result.json"
    cmd = [sys.executable, "-m", "artifact_receipts", "run", "--wheel", str(wheel),
           "--package", "demopkg", "--json-report", str(report_path)]
    for label in required:
        cmd += ["--require-child", label]
    cmd += ["--", tests, "-q"]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(tmp_path / "site")]),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    proc = subprocess.run(cmd, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    return proc.returncode, report, proc.stdout + proc.stderr


def test_correct_parent_and_declared_child_verify(tmp_path):
    code, report, out = scenario(tmp_path, "import demopkg, demopkg.sub\nassert demopkg.VALUE == 1\n")
    assert code == 0, out
    assert report["result"] == "verified"
    child = report["children"][0]
    assert child["result"] == "verified"
    assert {m["name"] for m in child["receipt"]["modules"]} == {"demopkg", "demopkg.sub"}
    assert child["receipt"]["execution"]["exit_code"] == 0
    assert child["receipt"]["pid"] != os.getpid()
    assert "not resistant to forgery" in report["child_scope"]


def test_parent_only_cannot_detect_child_shadowing_baseline(tmp_path):
    """Demonstrate the real old scope gap, not argparse/setup failure."""
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", H.standard_members())
    H.install_to(tmp_path / "site")
    H.install_to(tmp_path / "shadow", init=H.INIT_B)
    tests = H.write_test(tmp_path / "tests", """import demopkg, subprocess, sys, os

def test_parent():
    assert demopkg.VALUE == 1
    env = dict(os.environ, PYTHONPATH=%r)
    child = subprocess.run([sys.executable, '-c', 'import demopkg; assert demopkg.VALUE == 2'], env=env)
    assert child.returncode == 0
""" % str(tmp_path / "shadow"))
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests, pythonpath=[tmp_path / "site"])
    assert code == 0, out
    assert report["result"] == "verified"
    assert "children" not in report
    assert "neither observed nor detected" in report["scope"]


def test_required_child_shadowing_fails_even_if_return_ignored(tmp_path):
    H.install_to(tmp_path / "shadow", init=H.INIT_B)
    code, report, out = scenario(tmp_path, "import demopkg\nassert demopkg.VALUE == 2\n",
                                 child_path=tmp_path / "shadow", ignore_result=True)
    assert code == 13, out
    assert report["tests"]["passed"]
    assert report["result"] == "failed"
    assert report["children"][0]["result"] == "failed"


def test_required_child_never_launched_is_incomplete(tmp_path):
    code, report, out = scenario(tmp_path, "")
    assert code == 14, out
    assert report["tests"]["passed"]
    assert report["children"][0]["result"] == "incomplete"


@pytest.mark.parametrize("source", ["pass\n", "import os; os._exit(0)\n",
                                     "import demopkg; raise SystemExit(7)\n",
                                     "import demopkg; raise RuntimeError('boom')\n"])
def test_missing_import_or_abrupt_or_failed_execution_cannot_verify(tmp_path, source):
    code, report, out = scenario(tmp_path, source, ignore_result=True)
    assert code == 14, out
    assert report["tests"]["passed"]
    assert report["children"][0]["result"] == "incomplete"


def test_duplicate_required_label_cannot_be_hidden_by_test(tmp_path):
    extra = """
    try:
        run_observed_python('worker', %r)
    except RuntimeError:
        pass
""" % str(tmp_path / "job" / "worker.py")
    code, report, out = scenario(tmp_path, "import demopkg\n", parent_extra=extra)
    assert code == 14, out
    assert report["tests"]["passed"]
    assert "more than once" in report["reasons"][0]


def test_missing_second_declared_child_cannot_be_hidden(tmp_path):
    code, report, out = scenario(tmp_path, "import demopkg\n", required=("worker", "other"))
    assert code == 14, out
    assert [c["result"] for c in report["children"]] == ["verified", "incomplete"]


def test_failed_parent_test_keeps_pytest_exit(tmp_path):
    code, report, out = scenario(tmp_path, "import demopkg\n", parent_extra="\n    assert False\n")
    assert code == 1, out
    assert not report["tests"]["passed"]


def test_child_timeout_cannot_be_hidden_by_test(tmp_path):
    # Helper throws, pytest fails, and required-child evidence remains incomplete.
    code, report, out = scenario(tmp_path, "import demopkg,time; time.sleep(30)\n", timeout=0.1)
    assert code == 1, out
    assert report["children"][0]["result"] == "incomplete"


@pytest.mark.parametrize("field,value", [("nonce", "old-run"), ("pid", -1),
                                         ("packages", ["other"]), ("schema", 99),
                                         ("modules", []), ("result", "invented")])
def test_receipt_identity_or_verdict_corruption_fails_closed(tmp_path, field, value):
    extra = """
    import json
    from pathlib import Path
    context = json.loads(Path(os.environ['ARTIFACT_RECEIPTS_CHILD_CONTEXT']).read_text())
    receipt_path = Path(context['directory']) / 'worker.receipt.json'
    receipt = json.loads(receipt_path.read_text())
    receipt[%r] = %r
    receipt_path.write_text(json.dumps(receipt))
""" % (field, value)
    code, report, out = scenario(tmp_path, "import demopkg\n", parent_extra=extra)
    assert code == 14, out
    assert report["tests"]["passed"]
    assert report["children"][0]["result"] == "incomplete"


@pytest.mark.parametrize("mutation", [
    "receipt['wheel']['sha256'] = '0' * 64",
    "receipt['modules'][0]['verdict'] = 'invented'",
    "receipt['modules'][0]['observed_sha256'] = '0' * 64",
    "receipt['modules'][0]['matched_member'] = 'demopkg/sub.py'",
    "receipt['modules'][0]['preexisting'] = 'false'",
    "receipt['modules'][0]['bytecode'] = []",
    "receipt['execution']['exit_code'] = False",
    "receipt['reasons'] = 'not a reason list'",
])
def test_structural_receipt_corruption_is_incomplete(tmp_path, mutation):
    extra = """
    import json
    from pathlib import Path
    context = json.loads(Path(os.environ['ARTIFACT_RECEIPTS_CHILD_CONTEXT']).read_text())
    receipt_path = Path(context['directory']) / 'worker.receipt.json'
    receipt = json.loads(receipt_path.read_text())
    %s
    receipt_path.write_text(json.dumps(receipt))
""" % mutation
    code, report, out = scenario(tmp_path, "import demopkg\n", parent_extra=extra)
    assert code == 14, out
    assert report['tests']['passed']
    assert report['children'][0]['result'] == 'incomplete'


def test_process_exit_disagrees_with_receipt_is_incomplete(tmp_path):
    extra = """
    import json
    from pathlib import Path
    context = json.loads(Path(os.environ['ARTIFACT_RECEIPTS_CHILD_CONTEXT']).read_text())
    status_path = Path(context['directory']) / 'worker.status.json'
    status = json.loads(status_path.read_text())
    status['returncode'] = 17
    status_path.write_text(json.dumps(status))
"""
    code, report, out = scenario(tmp_path, "import demopkg\n", parent_extra=extra)
    assert code == 14, out
    assert report['tests']['passed']


def test_script_lookup_uses_script_directory_not_parent_site(tmp_path):
    # Matching parent import does not bless a different child script-local copy.
    H.install_to(tmp_path / 'job', init=H.INIT_B)
    # scenario creates job; remove only its empty root assertion via exist_ok.
    code, report, out = scenario(tmp_path, 'import demopkg\nassert demopkg.VALUE == 2\n',
                                 ignore_result=True)
    assert code == 13, out
    assert report['children'][0]['result'] == 'failed'


def test_child_cannot_recursively_register_in_parent_session(tmp_path):
    # Explicit children cannot use the helper to claim observed grandchildren.
    code, report, out = scenario(tmp_path, """import demopkg
from artifact_receipts.children import run_observed_python
try:
    run_observed_python('worker', __file__)
except RuntimeError as exc:
    assert 'no active' in str(exc)
else:
    raise AssertionError('child inherited parent registration authority')
""")
    assert code == 0, out
    assert report['children'][0]['result'] == 'verified'


@pytest.mark.parametrize('replacement', ['null', '[]', '{broken', '"text"'])
def test_malformed_or_nonobject_receipt_is_incomplete(tmp_path, replacement):
    extra = """
    import json
    from pathlib import Path
    context = json.loads(Path(os.environ['ARTIFACT_RECEIPTS_CHILD_CONTEXT']).read_text())
    (Path(context['directory']) / 'worker.receipt.json').write_text(%r)
""" % replacement
    code, report, out = scenario(tmp_path, 'import demopkg\n', parent_extra=extra)
    assert code == 14, out
    assert report['tests']['passed']


def test_invalid_output_encoding_does_not_break_receipt_collection(tmp_path):
    code, report, out = scenario(tmp_path, 'import demopkg,os; os.write(1, bytes([255]))\n')
    assert code == 0, out
    assert report['children'][0]['result'] == 'verified'


def test_grandchild_holding_output_pipe_does_not_hang_timeout(tmp_path):
    # A deliberately out-of-scope descendant retains stdout/stderr. Killing
    # only the named child must not make communicate wait forever on it.
    code, report, out = scenario(tmp_path, """import demopkg, subprocess, sys, time
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])
time.sleep(30)
""", timeout=0.3)
    assert code == 1, out
    assert report['children'][0]['result'] == 'incomplete'


def test_helper_preserves_script_arguments_and_cwd(tmp_path):
    extra = """
    from pathlib import Path
    second = Path(%r)
    second.write_text(%r)
    other = run_observed_python('other', second, ['a b', '--flag'], cwd=%r)
    assert other.returncode == 0, other.stderr
""" % (str(tmp_path / 'job' / 'second.py'),
       'import demopkg, os, sys; assert sys.argv[1:] == ["a b", "--flag"]; assert os.getcwd() == %r' % str(tmp_path),
       str(tmp_path))
    code, report, out = scenario(tmp_path, 'import demopkg\n', required=('worker', 'other'), parent_extra=extra)
    assert code == 0, out
    assert [c['result'] for c in report['children']] == ['verified', 'verified']
