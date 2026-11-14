"""Report content must not contradict an ordinary detected delivery failure."""
import copy
import json

import pytest

from artifact_receipts import report as R


def sample(result="verified"):
    return {"result": result, "reasons": [], "warnings": [], "modules": [],
            "wheel": {"filename": "example.whl", "sha256": "a" * 64},
            "interpreter": {"executable": "python", "version": "3"},
            "tests": {"passed": True, "exit_code": 0}}


@pytest.mark.parametrize("failed_format", ["json", "human"])
def test_partial_delivery_marks_surviving_report_incomplete(tmp_path, failed_format):
    report = sample()
    paths = {"json": tmp_path / "receipt.json", "human": tmp_path / "receipt.txt"}
    paths[failed_format] = tmp_path / "missing" / "receipt"
    errors = R.write_reports(report, json_path=str(paths["json"]), human_path=str(paths["human"]))
    assert errors
    good = "human" if failed_format == "json" else "json"
    text = paths[good].read_text()
    if good == "json":
        assert json.loads(text)["result"] == "incomplete"
        assert json.loads(text)["reasons"] == errors
    else:
        assert "artifact-receipts: INCOMPLETE" in text
        assert "write failed" in text
    assert report["result"] == "incomplete"


def test_successful_delivery_stays_verified(tmp_path):
    report = sample()
    original = copy.deepcopy(report)
    json_path, human_path = tmp_path / "receipt.json", tmp_path / "receipt.txt"
    assert R.write_reports(report, str(json_path), str(human_path)) == []
    assert report == original
    assert json.loads(json_path.read_text())["result"] == "verified"
    assert "artifact-receipts: VERIFIED" in human_path.read_text()


def test_failed_byte_binding_not_promoted_by_delivery_failure(tmp_path):
    report = sample("failed")
    errors = R.write_reports(report, str(tmp_path / "receipt.json"),
                             str(tmp_path / "missing" / "receipt.txt"))
    assert errors
    assert report["result"] == "failed"
    assert json.loads((tmp_path / "receipt.json").read_text())["result"] == "failed"


def test_unrepairable_written_report_is_removed(tmp_path, monkeypatch):
    report = sample()
    target = tmp_path / "receipt.json"
    original = R._write_one
    calls = []

    def write_once(report, path, kind):
        calls.append(path)
        if len(calls) != 1:
            raise OSError("synthetic failed recovery")
        original(report, path, kind)

    monkeypatch.setattr(R, "_write_one", write_once)
    errors = R.write_reports(report, str(target), str(tmp_path / "missing" / "receipt.txt"))
    assert errors
    assert not target.exists()
    assert report["result"] == "incomplete"


def test_stale_report_from_previous_run_is_removed_if_rewrite_fails(tmp_path, monkeypatch):
    target = tmp_path / "receipt.json"
    target.write_text('{"result": "verified"}')

    def fail_write(*args):
        raise OSError("synthetic filesystem failure")

    monkeypatch.setattr(R, "_write_one", fail_write)
    assert R.write_reports(sample(), str(target))
    assert not target.exists()


def test_unrecoverable_filesystem_failure_is_explicit(tmp_path, monkeypatch):
    target = tmp_path / "receipt.json"
    target.write_text('{"result": "verified"}')

    def fail(*args):
        raise PermissionError("synthetic denied")

    monkeypatch.setattr(R, "_write_one", fail)
    monkeypatch.setattr(R.os, "unlink", fail)
    report = sample()
    errors = R.write_reports(report, str(target))
    assert any("recovery/removal failed" in error for error in errors)
    assert report["result"] == "incomplete"
    # This limit is why a surviving report without this run's exit 0 is unsafe.
    assert json.loads(target.read_text())["result"] == "verified"


def test_failed_staged_replace_preserves_destination_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "receipt.json"
    target.write_text("old receipt")

    def fail(*args):
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(R.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic rename failure"):
        R._write_one(sample(), str(target), "json")
    assert target.read_text() == "old receipt"
    assert list(tmp_path.glob(".artifact-receipts-*")) == []


@pytest.mark.parametrize("failed_format", ["json", "human"])
@pytest.mark.parametrize("tests_fail", [False, True])
def test_cli_partial_delivery_preserves_failure_and_repairs_survivor(
        tmp_path, failed_format, tests_fail):
    import harness as H
    wheel = H.build_wheel(tmp_path / "demopkg-1.0-py3-none-any.whl", H.standard_members())
    H.install_to(tmp_path / "site")
    tests = H.write_test(tmp_path / "tests", source=H.TEST_FAIL if tests_fail else H.TEST_PASS)
    paths = {"json": tmp_path / "receipt.json", "human": tmp_path / "receipt.txt"}
    paths[failed_format] = tmp_path / "missing" / "receipt"
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests,
        pythonpath=[tmp_path / "site"], json_report=str(paths["json"]),
        human_report_path=paths["human"])
    assert code == (1 if tests_fail else 14), out
    good = "human" if failed_format == "json" else "json"
    text = paths[good].read_text()
    if good == "json":
        assert json.loads(text)["result"] == "incomplete"
    else:
        assert "artifact-receipts: INCOMPLETE" in text
