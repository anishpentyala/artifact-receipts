"""Combine test outcome + observations into a fail-closed overall result and
emit machine-readable and human reports.

Terminology (used identically in code, reports, and README):
  verified   - the byte-binding evidence is complete: every selected package
               was imported during this run, every observed source file is
               byte-identical to a wheel member, the audit hook and the
               terminal scan agree, no module was preloaded, no unsupported
               kind or mode was involved, and no bytecode cache contradicts
               its source file. It asserts binding of on-disk bytes to the
               artifact. It NEVER asserts which bytes were executed at
               import time - see the scope string.
  failed     - at least one observed module's bytes are not in the wheel.
  incomplete - evidence insufficient: nothing imported, preloaded module,
               unsupported kind/mode, observation gap, conflicting bytecode
               evidence, invalid wheel, or report-write failure.

Exit codes: 0 verified; 13 failed; 14 incomplete. 13/14 are deliberately
outside pytest's own 0-5 range so consumers can never confuse a provenance
result with a pytest internal error or usage error. A failing pytest run
keeps pytest's own exit code no matter what provenance says.
"""

from __future__ import annotations

import json
import os
import tempfile
import sys

from . import __version__
from .observe import MATCH, MISMATCH, OBSERVATION_GAP, UNSUPPORTED, UNREADABLE

VERIFIED, FAILED, INCOMPLETE = "verified", "failed", "incomplete"

EXIT_VERIFIED = 0
EXIT_FAILED = 13
EXIT_INCOMPLETE = 14

SCOPE = (
    "observed module source files are byte-identical to the member at the "
    "module's own archive-relative path in the given wheel, cross-checked "
    "between import-system resolution records and the terminal module "
    "table. Scope is the pytest process only: imports in forked, spawned "
    "or subprocess children are neither observed nor detected. 'verified' "
    "does not assert which bytes were executed at import time (files can "
    "change between import and inspection; default pyc validity is "
    "timestamp+size; unchecked hash-based pycs are trusted without a "
    "source check), does not attest build provenance, and does not cover "
    "extension modules, namespace packages, sourceless imports, or xdist "
    "workers."
)


def combine(packages, wheel_info, observations, mode_problems,
            tests_exit_code, import_event_count=None, observe_native=False):
    reasons = []
    warnings = list(wheel_info.warnings)
    result = VERIFIED

    if mode_problems:
        result = INCOMPLETE
        reasons.extend(mode_problems)

    # Standing honesty warning: child-process imports are outside the
    # observable scope of every run; this is stated, never "detected".
    warnings.append(
        "scope is the pytest process only: imports of selected packages in "
        "forked/spawned/subprocess children are neither observed nor "
        "detected"
    )

    for p in packages:
        obs = [o for o in observations
               if o.name == p or o.name.startswith(p + ".")]
        if not obs:
            result = INCOMPLETE
            reasons.append(
                "no module of selected package %r was imported during the "
                "run" % p
            )

    for o in observations:
        label = "%s (%s)" % (o.name, o.origin)
        if o.verdict == MISMATCH:
            result = FAILED
            reasons.append("provenance mismatch: %s - %s" % (label, o.reason))
        elif o.verdict == UNSUPPORTED:
            if result == VERIFIED:
                result = INCOMPLETE
            reasons.append("unsupported module: %s - %s" % (label, o.reason))
        elif o.verdict == UNREADABLE:
            if result == VERIFIED:
                result = INCOMPLETE
            reasons.append("unreadable origin: %s - %s" % (label, o.reason))
        elif o.verdict == OBSERVATION_GAP:
            if result == VERIFIED:
                result = INCOMPLETE
            reasons.append("observation gap: %s - %s" % (label, o.reason))
        elif o.preexisting:
            # Loaded before this run: import could have been satisfied from
            # sys.modules without any loader executing. Byte binding still
            # reported, but it cannot earn a verified result.
            if result == VERIFIED:
                result = INCOMPLETE
            reasons.append(
                "preloaded before the run (sys.modules hit, no observed "
                "loader execution): %s" % label
            )
        elif o.bytecode and o.bytecode.get("consistent") is False:
            if result == VERIFIED:
                result = INCOMPLETE
            reasons.append(
                "bytecode cache contradicts the current source bytes: %s"
                % label
            )
        if o.imported_not_resident:
            warnings.append(
                "%s was imported during the run but removed from sys.modules "
                "before it ended; binding uses the origin recorded at import "
                "time, read after the run" % o.name
            )
        if o.bytecode and o.bytecode.get("note"):
            warnings.append("%s: %s" % (o.name, o.bytecode["note"]))

    return {
        "tool": "artifact_receipts",
        "tool_version": __version__,
        "result": result,
        "reasons": reasons,
        "warnings": warnings,
        "tests": {
            "exit_code": int(tests_exit_code),
            "passed": int(tests_exit_code) == 0,
        },
        "observation": {
            "channel": "meta_path finder records + terminal sys.modules scan",
            "import_events_recorded": import_event_count,
        },
        "scope": (SCOPE.replace("observed module source files", "observed module source/native files")
                  .replace("extension modules, namespace packages", "nonstandard native loaders, namespace packages")
                  + " Native opt-in binds only the extension file on disk, not loaded memory, "
                    "shared-library dependencies, ABI/platform compatibility, or code safety."
                  if observe_native else SCOPE),
        "native_file_binding": bool(observe_native),
        "wheel": {
            "path": wheel_info.path,
            "filename": wheel_info.filename,
            "name": wheel_info.name,
            "version": wheel_info.version,
            "sha256": wheel_info.wheel_sha256,
            "dist_info": wheel_info.dist_info,
        },
        "interpreter": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "packages": list(packages),
        "modules": [
            {
                "name": o.name,
                "origin": o.origin,
                "loader": o.loader,
                "verdict": o.verdict,
                "reason": o.reason,
                "observed_sha256": o.observed_sha256,
                "artifact_members": o.artifact_members,
                "expected_members": o.expected_members,
                "matched_member": o.matched_member,
                "preexisting": o.preexisting,
                "imported_not_resident": o.imported_not_resident,
                "bytecode": o.bytecode,
            }
            for o in observations
        ],
    }


def human_text(report):
    lines = []
    r = report["result"].upper()
    lines.append("artifact-receipts: %s" % r)
    lines.append("  wheel: %s (sha256 %s...)" % (
        report["wheel"]["filename"], report["wheel"]["sha256"][:16]))
    lines.append("  interpreter: %s (%s)" % (
        report["interpreter"]["executable"],
        report["interpreter"]["version"]))
    lines.append("  tests: %s (exit %d)" % (
        "passed" if report["tests"]["passed"] else "FAILED",
        report["tests"]["exit_code"]))
    for mo in report["modules"]:
        member = mo["matched_member"] or "-"
        suffix = " (removed mid-run)" if mo["imported_not_resident"] else ""
        lines.append("  %-40s %-16s %s%s" % (
            mo["name"], mo["verdict"], member, suffix))
    for child in report.get("children", []):
        lines.append("  child %-32s %s" % (child["label"], child["result"]))
    if report.get("child_scope"):
        lines.append("  child scope: %s" % report["child_scope"])
    for reason in report["reasons"]:
        lines.append("  reason: %s" % reason)
    for w in report["warnings"]:
        lines.append("  warning: %s" % w)
    return "\n".join(lines) + "\n"


def _write_one(report, path, kind):
    # Replace each artifact only after a complete write/close. This is not an
    # atomic transaction across multiple paths, nor a power-loss guarantee.
    directory = os.path.dirname(os.path.abspath(path))
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".artifact-receipts-", delete=False) as f:
            staged = f.name
            if kind == "json":
                json.dump(report, f, indent=2, sort_keys=True)
                f.write("\n")
            else:
                f.write(human_text(report))
        os.replace(staged, path)
        staged = None
    finally:
        if staged is not None:
            try:
                os.unlink(staged)
            except OSError:
                pass


def write_reports(report, json_path=None, human_path=None):
    """Write reports, repairing surviving artifacts on detected write failure.

    Recovery is best-effort: consumers must also require this run's exit 0.
    Abrupt termination or failure of both rewrite and removal can leave stale
    output. No combination of separate output files is a commit record.
    """
    targets = [(kind, path) for kind, path in (("json", json_path), ("human", human_path))
               if path]
    errors = []
    for kind, path in targets:
        try:
            _write_one(report, path, kind)
        except OSError as e:
            errors.append("%s report write failed: %s" % (kind, e))
    if not errors:
        return errors
    if report["result"] == VERIFIED:
        report["result"] = INCOMPLETE
    report["reasons"].extend(errors)
    # A previous successful output must not still say VERIFIED merely because
    # another requested output failed. Also try to invalidate stale destinations
    # from earlier runs when the first write failed before replacement.
    for kind, path in targets:
        try:
            _write_one(report, path, kind)
        except OSError as e:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as removal:
                errors.append("%s report recovery/removal failed: %s; %s" %
                              (kind, e, removal))
    return errors
