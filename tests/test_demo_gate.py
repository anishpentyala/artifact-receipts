"""Shell demo gate controls. These test expected exit handling, not runtime provenance."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which('bash') is None or os.name == 'nt', reason='POSIX bash demo')
@pytest.mark.parametrize('shadow_exit,expected_demo_exit', [(0, 1), (14, 1), (13, 0)])
def test_demo_requires_expected_mismatch_exit(tmp_path, shadow_exit, expected_demo_exit):
    demo = Path(__file__).resolve().parents[1] / 'demo' / 'run_demo.sh'
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    fake = bindir / 'python3'
    fake.write_text(
        '#!/bin/sh\n'
        'case "$PYTHONPATH" in\n'
        f'  *checkout*) exit {shadow_exit};;\n'
        'esac\n'
        'cat >/dev/null\n'
        'exit 0\n'
    )
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
    proc = subprocess.run(['bash', str(demo)], env=env, capture_output=True, text=True)
    assert proc.returncode == expected_demo_exit, proc.stdout + proc.stderr
