"""CLI: artifact-receipts run --wheel W --package P [--package Q] \
        [--json-report PATH] [--human-report PATH] -- [pytest args]

Runs pytest in THIS interpreter via pytest.main, so the caller chooses the
environment by choosing which interpreter runs this command. Nothing is
installed and sys.path is not modified.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import sys

from . import report as report_mod
from .plugin import ReceiptsPlugin
from .wheel import WheelError, load_wheel


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        argv = argv[1:]
    if "--" in argv:
        i = argv.index("--")
        ours, pyt = argv[:i], argv[i + 1:]
    else:
        ours, pyt = argv, []

    parser = argparse.ArgumentParser(prog="artifact-receipts run")
    parser.add_argument("--wheel", required=True, help="local wheel file")
    parser.add_argument("--package", action="append", required=True,
                        help="import package to bind to the wheel (repeatable)")
    parser.add_argument("--json-report", help="write machine-readable report")
    parser.add_argument("--human-report", help="write short human report")
    parser.add_argument("--require-child", action="append", default=[],
                        help="require one explicitly observed named Python child (repeatable)")
    parser.add_argument("--observe-native", action="store_true",
                        help="bind standard extension-module files to exact wheel members; not dependencies or loaded memory")
    args = parser.parse_args(ours)
    if args.observe_native and args.require_child:
        parser.error("--observe-native with --require-child is not supported")

    try:
        import pytest
    except ImportError:
        print("artifact-receipts: pytest is not importable in this "
              "interpreter (%s)" % sys.executable, file=sys.stderr)
        return report_mod.EXIT_INCOMPLETE

    try:
        wheel_info = load_wheel(args.wheel)
    except WheelError as e:
        # Invalid wheel: explicit incomplete, nothing trusted from it.
        print("artifact-receipts: INCOMPLETE", file=sys.stderr)
        print("  reason: invalid wheel: %s" % e, file=sys.stderr)
        if args.json_report:
            stub = {
                "tool": "artifact_receipts",
                "result": "incomplete",
                "reasons": ["invalid wheel: %s" % e],
                "wheel": {"path": args.wheel},
            }
            import json as _json
            try:
                with open(args.json_report, "w", encoding="utf-8") as f:
                    _json.dump(stub, f, indent=2, sort_keys=True)
                    f.write("\n")
            except OSError as we:
                print("  json report write failed: %s" % we, file=sys.stderr)
        return report_mod.EXIT_INCOMPLETE

    plugin = ReceiptsPlugin(args.package, wheel_info, observe_native=args.observe_native)
    child_records = None
    if args.require_child:
        from .children import ChildSession, CONTEXT_ENV
        with tempfile.TemporaryDirectory(prefix="artifact-receipts-children-") as directory:
            try:
                child_session = ChildSession(directory, args.require_child, wheel_info, args.package)
            except ValueError as exc:
                parser.error(str(exc))
            prior_context = os.environ.get(CONTEXT_ENV)
            os.environ[CONTEXT_ENV] = str(child_session.context_path)
            try:
                tests_exit = pytest.main(pyt, plugins=[plugin])
            finally:
                if prior_context is None:
                    os.environ.pop(CONTEXT_ENV, None)
                else:
                    os.environ[CONTEXT_ENV] = prior_context
            child_records = child_session.collect()
    else:
        tests_exit = pytest.main(pyt, plugins=[plugin])

    report = report_mod.combine(
        args.package, wheel_info, plugin.observations, plugin.mode_problems,
        tests_exit, import_event_count=plugin.recorder.event_count,
        observe_native=args.observe_native,
    )
    if child_records is not None:
        from .children import CHILD_SCOPE
        report["children"] = child_records
        report["child_scope"] = CHILD_SCOPE
        report["scope"] += " Explicit required children are reported separately; see child_scope."
        for child in child_records:
            if child["result"] == report_mod.FAILED:
                report["result"] = report_mod.FAILED
            elif child["result"] != report_mod.VERIFIED and report["result"] == report_mod.VERIFIED:
                report["result"] = report_mod.INCOMPLETE
            report["reasons"].extend("child %s: %s" % (child["label"], reason)
                                     for reason in child["reasons"])
    write_errors = report_mod.write_reports(
        report, json_path=args.json_report, human_path=args.human_report)
    for e in write_errors:
        print("artifact-receipts: %s" % e, file=sys.stderr)
    print(report_mod.human_text(report), end="")

    if int(tests_exit) != 0:
        # Test failures remain failures regardless of provenance.
        return int(tests_exit)
    if report["result"] == report_mod.VERIFIED:
        return report_mod.EXIT_VERIFIED
    if report["result"] == report_mod.FAILED:
        return report_mod.EXIT_FAILED
    return report_mod.EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
