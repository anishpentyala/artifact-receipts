"""Internal fresh-process script observer, invoked only by children helper."""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys
import traceback

from . import observe
from .children import CHILD_SCOPE, _write_json
from .report import combine
from .wheel import load_wheel


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    context_path, label, script, *script_args = argv
    context = json.loads(Path(context_path).read_text(encoding="utf-8"))
    if label not in context["labels"]:
        raise ValueError("undeclared child label")
    info = load_wheel(context["wheel"])
    if info.wheel_sha256 != context["wheel_sha256"]:
        raise ValueError("wheel changed before child launch")
    packages = context["packages"]
    recorder = observe.ImportRecorder(packages)
    preexisting = {n for n in sys.modules if recorder.selected(n)}
    recorder.install()
    script_exit = 0
    try:
        # Match ordinary `python script.py` lookup and argument behavior. This
        # affects this child's script path, never its parent's path.
        sys.argv = [script, *script_args]
        sys.path[0] = os.path.dirname(script)
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        if exc.code is None:
            script_exit = 0
        elif isinstance(exc.code, int) and 0 <= exc.code <= 255:
            script_exit = int(exc.code)
        else:
            print(str(exc.code), file=sys.stderr)
            script_exit = 1
    except BaseException:
        traceback.print_exc()
        script_exit = 1
    observations = observe.observe_packages(packages, preexisting, info, recorder)
    receipt = combine(packages, info, observations, [], script_exit,
                      import_event_count=recorder.event_count)
    receipt.pop("tests")
    receipt["execution"] = {"script": script, "args": script_args,
                            "exit_code": script_exit}
    receipt["warnings"] = [w for w in receipt["warnings"]
                           if not w.startswith("scope is the pytest process")]
    receipt["scope"] = CHILD_SCOPE
    receipt.update(schema=1, nonce=context["nonce"], label=label, pid=os.getpid())
    _write_json(Path(context["directory"]) / (label + ".receipt.json"), receipt)
    return script_exit or {"verified": 0, "failed": 13, "incomplete": 14}[receipt["result"]]


if __name__ == "__main__":
    raise SystemExit(main())
