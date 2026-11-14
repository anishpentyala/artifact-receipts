"""Explicit named Python child jobs. Not an observer of arbitrary descendants.

The run nonce prevents accidental receipt reuse, not forgery by tested code.
Tests and receipt files run with the same user's authority. This is cooperative
build evidence, not a sandbox or security attestation.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

CONTEXT_ENV = "ARTIFACT_RECEIPTS_CHILD_CONTEXT"
CHILD_SCOPE = (
    "Only explicitly required named Python children launched by "
    "run_observed_python are included. Other children and grandchildren are "
    "neither observed nor detected. Each child binds imported source files "
    "on disk to its own expected wheel paths, not executed bytes. Receipts "
    "are cooperative evidence, not resistant to forgery by tested code."
)


def _write_json(path, data):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        text = stream.read(8 * 1024 * 1024 + 1)
    if len(text) > 8 * 1024 * 1024:
        raise ValueError("child evidence exceeds 8 MiB character limit")
    return json.loads(text)


class ChildSession:
    def __init__(self, directory, labels, wheel_info, packages):
        if len(labels) != len(set(labels)):
            raise ValueError("required child labels must be unique")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", x) for x in labels):
            raise ValueError("child labels need 1-64 ASCII letters, digits, _ or -")
        self.directory = Path(directory)
        self.labels = list(labels)
        self.nonce = uuid.uuid4().hex
        self.wheel_sha256 = wheel_info.wheel_sha256
        self.packages = list(packages)
        self.wheel_info = wheel_info
        self.context_path = self.directory / "context.json"
        self.context = {
            "schema": 1, "nonce": self.nonce,
            "wheel": os.path.abspath(wheel_info.path),
            "wheel_sha256": self.wheel_sha256,
            "packages": self.packages, "labels": self.labels,
            "directory": str(self.directory),
        }
        _write_json(self.context_path, self.context)

    def collect(self):
        """Read all required terminal states. Missing evidence never verifies."""
        children = []
        for label in self.labels:
            record = {"label": label, "result": "incomplete", "reasons": []}
            try:
                if (self.directory / (label + ".duplicate.json")).exists():
                    raise ValueError("required label launched more than once")
                if (self.directory / (label + ".started")).read_text() != self.nonce:
                    raise ValueError("child start marker belongs to another run")
                status = _read_json(self.directory / (label + ".status.json"))
                receipt = _read_json(self.directory / (label + ".receipt.json"))
                if not isinstance(status, dict) or not isinstance(receipt, dict):
                    raise ValueError("receipt and status must be objects")
                if status.get("schema") != 1 or receipt.get("schema") != 1:
                    raise ValueError("unsupported receipt schema")
                for obj in (status, receipt):
                    if obj.get("nonce") != self.nonce or obj.get("label") != label:
                        raise ValueError("receipt belongs to another run or label")
                if status.get("state") != "finished":
                    raise ValueError("child launch did not finish normally")
                if type(status.get("pid")) is not int or status["pid"] <= 0:
                    raise ValueError("missing child PID")
                if receipt.get("pid") != status["pid"]:
                    raise ValueError("receipt PID differs from launched child")
                if receipt.get("wheel", {}).get("sha256") != self.wheel_sha256:
                    raise ValueError("child wheel identity differs from parent")
                if receipt.get("packages") != self.packages:
                    raise ValueError("child selected packages differ from parent")
                result = receipt.get("result")
                expected = {"verified": 0, "failed": 13, "incomplete": 14}
                if result not in expected:
                    raise ValueError("unknown child verdict")
                execution = receipt.get("execution", {})
                if type(execution.get("exit_code")) is not int:
                    raise ValueError("missing script exit status")
                script_exit = execution["exit_code"]
                if not 0 <= script_exit <= 255:
                    raise ValueError("invalid child script exit status")
                for field in ("reasons", "warnings"):
                    if not isinstance(receipt.get(field), list) or not all(
                            isinstance(item, str) for item in receipt[field]):
                        raise ValueError("invalid child %s" % field)
                if type(status.get("returncode")) is not int:
                    raise ValueError("missing observed process return status")
                code = status["returncode"]
                if code != (script_exit if script_exit else expected[result]):
                    raise ValueError("process exit and receipt verdict disagree")
                from .observe import ModuleObservation, expected_member_paths
                from .report import combine
                modules = receipt.get("modules")
                if not isinstance(modules, list):
                    raise ValueError("missing child observations")
                observations = [ModuleObservation(**module) for module in modules]
                for observation in observations:
                    if not isinstance(observation.name, str) or observation.verdict not in {
                            "match", "mismatch", "unsupported", "unreadable", "observation_gap"}:
                        raise ValueError("invalid child module name or verdict")
                    if type(observation.preexisting) is not bool or type(observation.imported_not_resident) is not bool:
                        raise ValueError("invalid child observation flags")
                    if observation.bytecode is not None:
                        if not isinstance(observation.bytecode, dict):
                            raise ValueError("invalid child bytecode evidence")
                        consistency = observation.bytecode.get("consistent")
                        if consistency is not None and type(consistency) is not bool:
                            raise ValueError("invalid bytecode consistency flag")
                    if observation.verdict == "match":
                        member = observation.matched_member
                        if member not in expected_member_paths(observation.name, observation.origin):
                            raise ValueError("matched member does not belong to this module")
                        if self.wheel_info.members.get(member) != observation.observed_sha256:
                            raise ValueError("matched child digest differs from wheel member")
                checked = combine(self.packages, self.wheel_info, observations, [], script_exit)
                if checked["result"] != result:
                    raise ValueError("child verdict disagrees with its observations")
                record.update(receipt=receipt, returncode=code)
                if result == "failed":
                    record["result"] = "failed"
                    record["reasons"].extend(receipt.get("reasons", []))
                if script_exit != 0:
                    record["reasons"].append("child script exited with %s" % script_exit)
                elif result == "verified":
                    record["result"] = "verified"
                else:
                    record["result"] = result
                    record["reasons"].extend(receipt.get("reasons", []))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                record["reasons"].append("missing or invalid child evidence: %s" % exc)
            children.append(record)
        return children


def run_observed_python(label, script, args=(), *, python=None, cwd=None,
                        env=None, timeout=60):
    """Launch one declared script in a fresh, explicitly observed Python process.

    No shell, no install, no sys.path injection. The chosen interpreter must
    already have artifact_receipts installed. The return value is the actual
    CompletedProcess (stdout/stderr captured); ignoring it cannot hide a failed
    or missing child receipt from the parent. Descendants are outside scope.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number of seconds")
    context_file = os.environ.get(CONTEXT_ENV)
    if not context_file:
        raise RuntimeError("no active artifact-receipts child session")
    context = json.loads(Path(context_file).read_text(encoding="utf-8"))
    if label not in context["labels"]:
        raise ValueError("undeclared child label: %s" % label)
    directory = Path(context["directory"])
    # A second invocation cannot overwrite a receipt, even if tests catch this.
    marker = directory / (label + ".started")
    try:
        with marker.open("x", encoding="utf-8") as f:
            f.write(context["nonce"])
    except FileExistsError:
        _write_json(directory / (label + ".duplicate.json"), {"label": label})
        raise RuntimeError("child label launched more than once: %s" % label)
    child_env = dict(os.environ if env is None else env)
    child_env.pop(CONTEXT_ENV, None)
    command = [python or sys.executable, "-m", "artifact_receipts.child_runner",
               context_file, label, os.path.abspath(script), *list(args)]
    status = {"schema": 1, "nonce": context["nonce"], "label": label,
              "state": "starting"}
    path = directory / (label + ".status.json")
    _write_json(path, status)
    with subprocess.Popen(command, cwd=cwd, env=child_env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, errors="replace") as proc:
        status.update(pid=proc.pid, state="running")
        _write_json(path, status)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                # Grandchildren are out of scope and can retain the pipes. Do
                # not hang forever or pretend to terminate their process tree.
                proc.stdout.close()
                proc.stderr.close()
                proc.wait(timeout=1)
            status.update(state="timeout", returncode=proc.returncode)
            _write_json(path, status)
            raise
        status.update(state="finished", returncode=proc.returncode)
        _write_json(path, status)
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
