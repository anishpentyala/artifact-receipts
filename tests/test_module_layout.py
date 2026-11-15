"""Same bytes under the other module layout are not the imported source path."""

import pytest

import harness as H


@pytest.mark.parametrize("wheel_package", [False, True])
def test_same_bytes_other_layout_is_not_verified(tmp_path, wheel_package):
    members = H.standard_members()
    members.pop("demopkg/sub.py")
    if not wheel_package:
        members["demopkg.py"] = members.pop("demopkg/__init__.py")
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", members)
    site = tmp_path / "site"
    site.mkdir()
    if wheel_package:
        (site / "demopkg.py").write_bytes(H.INIT_A)
    else:
        package = site / "demopkg"
        package.mkdir()
        (package / "__init__.py").write_bytes(H.INIT_A)
    tests = H.write_test(tmp_path / "tests", source="import demopkg\ndef test_ok():\n assert demopkg.VALUE == 1\n")
    code, report, output = H.run_tool(tmp_path, wheel, testdir=tests, pythonpath=[site])
    assert code == 13, output
    assert report["result"] == "failed"
    assert report["modules"][0]["verdict"] == "mismatch"


@pytest.mark.parametrize("wheel_package", [False, True])
def test_matching_layout_same_bytes_stays_verified(tmp_path, wheel_package):
    members = H.standard_members()
    members.pop("demopkg/sub.py")
    if not wheel_package:
        members["demopkg.py"] = members.pop("demopkg/__init__.py")
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", members)
    site = tmp_path / "site"
    site.mkdir()
    if wheel_package:
        package = site / "demopkg"
        package.mkdir()
        (package / "__init__.py").write_bytes(H.INIT_A)
    else:
        (site / "demopkg.py").write_bytes(H.INIT_A)
    tests = H.write_test(tmp_path / "tests", source="import demopkg\ndef test_ok():\n assert demopkg.VALUE == 1\n")
    code, report, output = H.run_tool(tmp_path, wheel, testdir=tests, pythonpath=[site])
    assert code == 0, output
    assert report["result"] == "verified"


def test_declared_child_module_cannot_match_package_member(tmp_path):
    from test_children import scenario
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "demopkg.py").write_bytes(H.INIT_A)
    code, report, output = scenario(tmp_path, "import demopkg\nassert demopkg.VALUE == 1\n",
                                   child_path=shadow, ignore_result=True)
    assert code == 13, output
    assert report["tests"]["passed"]
    assert report["children"][0]["result"] == "failed"


def test_nonstandard_custom_loader_filename_is_explicitly_unsupported(tmp_path):
    from artifact_receipts.observe import _classify_origin
    from artifact_receipts.wheel import load_wheel
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", H.standard_members())
    path = tmp_path / "arbitrary.py"
    path.write_bytes(H.INIT_A)
    obs = _classify_origin("demopkg", str(path), "SourceFileLoader", load_wheel(wheel), {})
    assert obs.verdict == "unsupported"
    assert obs.expected_members == []


def test_missing_origin_cannot_imply_either_layout():
    from artifact_receipts.observe import expected_member_paths
    assert expected_member_paths("demopkg") == []
    assert expected_member_paths("demopkg", None) == []
