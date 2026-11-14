"""Check installed runtime bytes, then run tests and demo outside the source tree."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile


def main():
    source, wheel = (Path(x).resolve(strict=True) for x in sys.argv[1:])
    spec = importlib.util.find_spec("artifact_receipts")
    if spec is None or spec.origin is None:
        raise RuntimeError("artifact_receipts is not installed")
    origin = Path(spec.origin).resolve(strict=True).parent
    if origin == source / "artifact_receipts" or source in origin.parents:
        raise RuntimeError("tool resolves inside the source tree")
    rows = []
    with ZipFile(wheel) as archive:
        runtime = sorted((source / "artifact_receipts").glob("*.py"))
        expected = {"artifact_receipts/" + p.name for p in runtime}
        packaged = {p for p in archive.namelist() if p.startswith("artifact_receipts/") and p.endswith(".py")}
        if expected != packaged or not expected:
            raise RuntimeError("runtime file list does not match wheel")
        for path in runtime:
            data = path.read_bytes()
            if archive.read("artifact_receipts/" + path.name) != data:
                raise RuntimeError("source/wheel bytes differ: " + path.name)
            if (origin / path.name).read_bytes() != data:
                raise RuntimeError("installed bytes differ: " + path.name)
            rows.append({"file": path.name, "sha256": hashlib.sha256(data).hexdigest()})
    import pytest

    print(json.dumps({"python": sys.version, "pytest": pytest.__version__, "origin": str(origin),
                      "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                      "runtime_files": rows}, indent=2), flush=True)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    with tempfile.TemporaryDirectory(prefix="artifact-installed-check-") as directory:
        root = Path(directory)
        for name in ("tests", "demo"):
            shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=root, env=env, check=True)
        subprocess.run(["bash", "demo/run_demo.sh"], cwd=root, env=env, check=True)


if __name__ == "__main__":
    main()
