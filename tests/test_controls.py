"""Negative controls from the design proposal. Each scenario runs the tool in
a clean subprocess and asserts on exit code and the machine-readable report.
"""

import os
import subprocess
import sys
import zipfile

import harness as H


def test_correct_install_verified(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    site = H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 0, out
    assert report["result"] == "verified"
    assert report["tests"]["passed"] is True
    mods = {m["name"]: m for m in report["modules"]}
    assert set(mods) == {"demopkg", "demopkg.sub"}
    assert mods["demopkg"]["verdict"] == "match"
    assert "demopkg/__init__.py" in mods["demopkg"]["artifact_members"]
    assert mods["demopkg.sub"]["verdict"] == "match"


def test_source_checkout_shadowing_fails(tmp_path):
    # The classic cibuildwheel-1534 shape: a source checkout ahead of
    # site-packages on sys.path. pytest passes; provenance must fail.
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    H.install_to(tmp_path / "checkout", init=H.INIT_B, sub=H.SUB_B)
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(
        tmp_path, wheel, testdir=tests,
        pythonpath=[tmp_path / "checkout", tmp_path / "site"])
    assert code == 13, out
    assert report["result"] == "failed"
    assert report["tests"]["passed"] is True
    assert any(m["verdict"] == "mismatch" for m in report["modules"])


def test_same_path_same_version_changed_bytes_fails(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    # Installed under the same name/version path, but bytes differ.
    site = H.install_to(tmp_path / "site", init=H.INIT_B, sub=H.SUB_B)
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 13, out
    assert report["result"] == "failed"


def test_submodule_shadows_while_root_matches(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    pkg = H.install_to(tmp_path / "site")
    # Correct root __init__, tampered submodule.
    with open(os.path.join(str(pkg), "sub.py"), "wb") as f:
        f.write(H.SUB_B)
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 13, out
    mods = {m["name"]: m for m in report["modules"]}
    assert mods["demopkg"]["verdict"] == "match"
    assert mods["demopkg.sub"]["verdict"] == "mismatch"


def test_nothing_imported_is_incomplete(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests", source=H.TEST_NOIMPORT)
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 14, out
    assert report["result"] == "incomplete"
    assert any("no module" in r for r in report["reasons"])


def test_namespace_package_is_incomplete(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    # Namespace layout: no __init__.py.
    nsroot = tmp_path / "ns" / "demopkg"
    nsroot.mkdir(parents=True)
    (nsroot / "sub.py").write_bytes(H.SUB_A)
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "ns"])
    assert code == 14, out
    assert report["result"] == "incomplete"
    assert any(m["verdict"] == "unsupported" for m in report["modules"])


def test_failing_pytest_stays_failing_when_provenance_matches(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests", source=H.TEST_FAIL)
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code != 0 and code not in (13, 14), out  # pytest's own exit code
    assert report["tests"]["passed"] is False


def test_failing_pytest_stays_failing_even_with_mismatch(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site", init=H.INIT_B, sub=H.SUB_B)
    tests = H.write_test(tmp_path / "tests", source=H.TEST_FAIL)
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 1, out  # pytest failure, not the provenance code


def test_duplicate_members_wheel_incomplete(tmp_path):
    wheel = tmp_path / "demopkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(str(wheel), "w") as z:
        z.writestr("demopkg/__init__.py", H.INIT_A)
        z.writestr("demopkg/__init__.py", H.INIT_B)  # duplicate
        z.writestr("demopkg-1.0.dist-info/RECORD", b"")
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 14, out
    assert "duplicate" in out


def test_path_traversal_wheel_incomplete(tmp_path):
    members = H.standard_members()
    members["../escape.py"] = b"evil\n"
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", members)
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 14, out
    assert "unsafe member paths" in out


def test_malformed_record_incomplete(tmp_path):
    members = H.standard_members()
    wheel = H.build_wheel(
        tmp_path / "demopkg-1.0-py3-none-any.whl", members,
        record_rows=["demopkg/__init__.py,sha256=wrongdigest,28"])
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 14, out
    assert "digest mismatch" in out


def test_report_write_failure_never_verified(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    bad = tmp_path / "no-such-dir" / "report.json"
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"],
                                   json_report=str(bad))
    assert code == 14, out
    assert "json report write failed" in out


def test_report_write_failure_does_not_erase_test_failure(tmp_path):
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests", source=H.TEST_FAIL)
    bad = tmp_path / "no-such-dir" / "report.json"
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"],
                                   json_report=str(bad))
    assert code == 1, out


def test_preloaded_module_cannot_verify(tmp_path):
    # import system 5.3.1: a sys.modules entry satisfies import without a
    # loader running. A module loaded before pytest started must not earn a
    # verified result even when its file bytes match.
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"],
                                   preload="demopkg")
    assert code == 14, out
    assert report["result"] == "incomplete"
    assert any("preloaded" in r for r in report["reasons"])
    # Byte binding is still reported honestly.
    mods = {m["name"]: m for m in report["modules"]}
    assert mods["demopkg"]["verdict"] == "match"
    assert mods["demopkg"]["preexisting"] is True


def test_stale_timestamp_pyc_tampered_file_still_caught(tmp_path):
    # Size- and mtime-preserving tamper defeats the pyc timestamp check, but
    # the tool hashes the current source file, so the mismatch is caught.
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    site = H.install_to(tmp_path / "site")
    subprocess.run(
        [sys.executable, "-c", "import demopkg"],
        env={**os.environ, "PYTHONPATH": str(tmp_path / "site")}, check=True)
    H.make_stale_pyc(str(site), H.INIT_B)
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 13, out
    assert report["result"] == "failed"


def test_stale_pyc_executed_while_file_matches_documented_boundary(tmp_path):
    # The honest boundary: if the on-disk file matches the wheel but the
    # interpreter silently executed stale pyc bytes (size/mtime preserved),
    # post-hoc file hashing cannot see it. The tool must NOT claim executed-
    # byte proof; the report's scope text and bytecode evidence carry this.
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members(init=H.INIT_B, sub=H.SUB_B))
    site = tmp_path / "site"
    H.install_to(site)  # writes INIT_A / SUB_A
    env = {**os.environ, "PYTHONPATH": str(site)}
    subprocess.run([sys.executable, "-c", "import demopkg, demopkg.sub"],
                   env=env, check=True)  # pycs now hold A-bytes
    pkg = os.path.join(str(site), "demopkg")
    # Swap file contents to B (matching the wheel), preserving size/mtime.
    H.make_stale_pyc(pkg, H.INIT_B)
    subp = os.path.join(pkg, "sub.py")
    st = os.stat(subp)
    with open(subp, "wb") as f:
        f.write(H.SUB_B)
    os.utime(subp, (st.st_atime, st.st_mtime))
    # Prove the interpreter runs the STALE bytes (value() == 1, file says 2).
    proc = subprocess.run(
        [sys.executable, "-c",
         "import demopkg.sub; print(demopkg.sub.value())"],
        env=env, capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "1"
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[site])
    assert code == 0, out
    assert report["result"] == "verified"
    assert "does not assert which bytes were executed" in report["scope"]
    for m in report["modules"]:
        if m["bytecode"]:
            assert m["bytecode"]["scheme"] in (
                "timestamp-based", "hash-based (PEP 552)")


def test_unchecked_hash_pyc_flagged(tmp_path):
    # PEP 552 hash-based pyc with check_source unset: interpreter trusts it
    # without checking the source. The tool must surface this in warnings.
    import importlib.util
    import struct
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    site = H.install_to(tmp_path / "site")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "site"),
           "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache")}
    src = os.path.join(str(tmp_path / "site"), "demopkg", "__init__.py")
    data = open(src, "rb").read()
    # Resolve the prefixed cache path exactly as the interpreter will.
    proc = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, sys; "
         "print(importlib.util.cache_from_source(sys.argv[1]))", src],
        env=env, capture_output=True, text=True, check=True)
    pyc_path = proc.stdout.strip()
    os.makedirs(os.path.dirname(pyc_path), exist_ok=True)
    # Compile an unchecked hash-based pyc manually (PEP 552, check_source 0).
    code_obj = compile(data, src, "exec")
    header = (importlib.util.MAGIC_NUMBER
              + struct.pack("<I", 0b01)  # hash-based, check_source unset
              + importlib.util.source_hash(data))
    import marshal
    with open(pyc_path, "wb") as f:
        f.write(header)
        f.write(marshal.dumps(code_obj))
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"],
                                   extra_env=env and {
                                       "PYTHONPYCACHEPREFIX":
                                           str(tmp_path / "pycache")})
    assert code == 0, out
    mods = {m["name"]: m for m in report["modules"]}
    bc = mods["demopkg"]["bytecode"]
    assert bc["scheme"] == "hash-based (PEP 552)"
    assert bc["consistent"] is True
    assert any("trusted-without-checking" in w for w in report["warnings"])


TEST_IMPORT_THEN_REMOVE = (
    "import sys\nimport demopkg, demopkg.sub\n"
    "def test_ok():\n    assert demopkg.sub.value() in (1, 2)\n"
    "def test_remove():\n    del sys.modules['demopkg.sub']\n"
)

TEST_IMPORT_THEN_REPLACE = (
    "import sys, types\nimport demopkg, demopkg.sub\n"
    "def test_replace():\n"
    "    fake = types.ModuleType('demopkg.sub')\n"
    "    fake.__file__ = '/elsewhere/demopkg/sub.py'\n"
    "    sys.modules['demopkg.sub'] = fake\n"
)


def test_import_then_removed_still_observed(tmp_path):
    # A terminal sys.modules scan alone would miss this module entirely;
    # the audit hook recorded its import filename, so it is still bound.
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests", source=TEST_IMPORT_THEN_REMOVE)
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 0, out
    assert report["result"] == "verified"
    mods = {m["name"]: m for m in report["modules"]}
    assert mods["demopkg.sub"]["verdict"] == "match"
    assert mods["demopkg.sub"]["imported_not_resident"] is True
    assert any("removed from sys.modules" in w for w in report["warnings"])
    assert report["observation"]["import_events_recorded"] > 0


def test_import_then_replaced_is_observation_gap(tmp_path):
    # The resident module's origin disagrees with the filename the audit
    # hook recorded at import time: fail closed, never verified.
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests", source=TEST_IMPORT_THEN_REPLACE)
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 14, out
    assert report["result"] == "incomplete"
    mods = {m["name"]: m for m in report["modules"]}
    assert mods["demopkg.sub"]["verdict"] == "observation_gap"
    assert any("observation gap" in r for r in report["reasons"])


TEST_MULTI_ORIGIN = (
    "import sys, os\n"
    "ck = os.environ['CHECKOUT']\n"
    "sys.path.insert(0, ck)\n"
    "import demopkg, demopkg.sub\n"
    "assert demopkg.sub.value() == 2\n"
    "del sys.modules['demopkg.sub']; del sys.modules['demopkg']\n"
    "sys.path.remove(ck)\n"
    "import demopkg, demopkg.sub\n"
    "def test_ok():\n    assert demopkg.sub.value() == 1\n"
)


def test_multi_origin_while_resident_is_gap(tmp_path):
    # Tampered copy imported and executed, removed, then re-imported from
    # the matching copy. The resident origin agrees with ONE recorded origin
    # but not EVERY one; that must not verify.
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    H.install_to(tmp_path / "checkout", init=H.INIT_B, sub=H.SUB_B)
    tests = H.write_test(tmp_path / "tests", source=TEST_MULTI_ORIGIN)
    code, report, out = H.run_tool(
        tmp_path, wheel, testdir=tests, pythonpath=[tmp_path / "site"],
        extra_env={"CHECKOUT": str(tmp_path / "checkout")})
    assert code == 14, out
    assert report["result"] == "incomplete"
    assert any("multiple distinct origins" in r for r in report["reasons"])


def test_bytes_matching_only_non_module_member_fails(tmp_path):
    # The wheel contains no demopkg/__init__.py, but a non-module member has
    # identical bytes. Binding requires the member at the module's own
    # archive-relative path.
    members = {
        "demopkg/sub.py": H.SUB_A,
        "demopkg-1.0.dist-info/LICENSE": H.INIT_A,
        "demopkg-1.0.dist-info/METADATA": H.METADATA,
        "demopkg-1.0.dist-info/WHEEL": H.WHEEL_MD,
    }
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", members)
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 13, out
    mods = {m["name"]: m for m in report["modules"]}
    assert mods["demopkg"]["verdict"] == "mismatch"
    assert "expected path" in mods["demopkg"]["reason"]
    assert mods["demopkg.sub"]["verdict"] == "match"
    assert mods["demopkg.sub"]["matched_member"] == "demopkg/sub.py"


def test_distinfo_filename_conflict_incomplete(tmp_path):
    # Artifact identity disagreement is a WheelError.
    members = {
        "demopkg/__init__.py": H.INIT_A,
        "demopkg/sub.py": H.SUB_A,
        "othername-2.0.dist-info/METADATA":
            b"Metadata-Version: 2.1\nName: othername\nVersion: 2.0\n",
        "othername-2.0.dist-info/WHEEL": H.WHEEL_MD,
    }
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", members,
                          dist_info="othername-2.0.dist-info")
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 14, out
    assert "conflicts with filename" in out


def test_metadata_name_conflict_incomplete(tmp_path):
    members = {
        "demopkg/__init__.py": H.INIT_A,
        "demopkg/sub.py": H.SUB_A,
        "demopkg-1.0.dist-info/METADATA":
            b"Metadata-Version: 2.1\nName: othername\nVersion: 1.0\n",
        "demopkg-1.0.dist-info/WHEEL": H.WHEEL_MD,
    }
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", members)
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 14, out
    assert "METADATA Name" in out


def test_data_relocation_is_unsupported_not_failed(tmp_path):
    # A correct install of a wheel that ships the module under
    # .data/purelib relocation must be incomplete/unsupported, not
    # failed/provenance mismatch.
    members = {
        "demopkg/__init__.py": H.INIT_A,
        "demopkg-1.0.data/purelib/demopkg/sub.py": H.SUB_A,
        "demopkg-1.0.dist-info/METADATA": H.METADATA,
        "demopkg-1.0.dist-info/WHEEL": H.WHEEL_MD,
    }
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", members)
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 14, out
    assert report["result"] == "incomplete"
    mods = {m["name"]: m for m in report["modules"]}
    assert mods["demopkg"]["verdict"] == "match"
    assert mods["demopkg.sub"]["verdict"] == "unsupported"
    assert ".data relocation" in mods["demopkg.sub"]["reason"]


def test_child_process_imports_are_outside_scope_and_labeled(tmp_path):
    # Scope honesty: a selected submodule imported only in a forked child is
    # outside what the tool observes. The run stays verified for the parent
    # process and every report carries the standing scope warning saying
    # child-process imports are neither observed nor detected.
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    site = H.install_to(tmp_path / "site")
    with open(os.path.join(str(site), "sub.py"), "wb") as f:
        f.write(H.SUB_B)
    src = (
        "import os\nimport demopkg\n"
        "def test_child_import():\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        import demopkg.sub\n"
        "        os._exit(0 if demopkg.sub.value() == 2 else 1)\n"
        "    _, status = os.waitpid(pid, 0)\n"
        "    assert os.WEXITSTATUS(status) == 0\n"
    )
    tests = H.write_test(tmp_path / "tests", source=src)
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"])
    assert code == 0, out
    assert any("child" in w and "neither observed nor detected" in w
               for w in report["warnings"])
    assert "pytest process only" in report["scope"]


def test_tool_exit_codes_never_collide_with_pytest(tmp_path):
    # pytest's own exit codes live in 0-5 (e.g. 3 internal error, 4 usage
    # error). Provenance results use 13/14 so consumers can never confuse
    # them. Here pytest exits 4 (usage error) and passes through unchanged.
    from artifact_receipts import report as report_mod
    assert report_mod.EXIT_FAILED > 5
    assert report_mod.EXIT_INCOMPLETE > 5
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl",
                          H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests")
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / "site"],
                                   pytest_args=["--no-such-option"])
    assert code == 4, out  # pytest's usage error, unchanged


def test_distinfo_filename_conflict_with_matching_metadata(tmp_path):
    # Isolate directory/filename identity from the separate METADATA check.
    # Name/Version agree with the filename; only the dist-info path disagrees.
    members = {
        'demopkg/__init__.py': H.INIT_A,
        'demopkg/sub.py': H.SUB_A,
        'othername-2.0.dist-info/METADATA': H.METADATA,
        'othername-2.0.dist-info/WHEEL': H.WHEEL_MD,
    }
    wheel = H.build_wheel(tmp_path / 'demopkg-1.0-py3-none-any.whl', members,
                          dist_info='othername-2.0.dist-info')
    H.install_to(tmp_path / 'site')
    tests = H.write_test(tmp_path / 'tests')
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / 'site'])
    assert code == 14, out
    assert 'conflicts with filename' in out
    assert 'METADATA Name' not in out


def test_resident_never_resolved_and_not_preloaded_is_gap(tmp_path):
    wheel = H.build_wheel(tmp_path / 'demopkg-1.0-py3-none-any.whl',
                          H.standard_members())
    pkg = H.install_to(tmp_path / 'site')
    # During pytest collection, inject a module without a finder resolution.
    # Its source bytes match, but the observation channel must not verify it.
    tests = H.write_test(tmp_path / 'tests', source=(
        'import sys, types\n'
        "module = types.ModuleType('demopkg')\n"
        f'module.__file__ = {os.path.join(pkg, "__init__.py")!r}\n'
        "sys.modules['demopkg'] = module\n"
        'def test_ok():\n    assert True\n'
    ))
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
                                   pythonpath=[tmp_path / 'site'])
    assert code == 14, out
    mods = {m['name']: m for m in report['modules']}
    assert mods['demopkg']['verdict'] == 'observation_gap'
    assert 'never resolved' in mods['demopkg']['reason']
    assert 'not preloaded' in mods['demopkg']['reason']
