"""Native file binding: compiled imports plus exact-path and refusal controls."""
import importlib.machinery as M
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest
import harness as H
from artifact_receipts.observe import _classify_origin, native_member_paths
from artifact_receipts.wheel import load_wheel


@pytest.fixture(scope='module')
def binaries(tmp_path_factory):
    root = tmp_path_factory.mktemp('native-build')
    paths = []
    for marker in (1, 2):
        c = root / ('build%d.c' % marker)
        c.write_text('''#include <Python.h>
static PyObject *answer(PyObject *s, PyObject *a) {return PyLong_FromLong(42);}
static PyObject *marker(PyObject *s, PyObject *a) {return PyLong_FromLong(MARKER);}
static PyMethodDef methods[] = {{"answer", answer, METH_NOARGS, NULL},
{"marker", marker, METH_NOARGS, NULL}, {NULL, NULL, 0, NULL}};
static struct PyModuleDef mod = {PyModuleDef_HEAD_INIT, "demopkg", NULL, -1, methods};
PyMODINIT_FUNC PyInit_demopkg(void) {return PyModule_Create(&mod);}
'''.replace('MARKER', str(marker)))
        out = root / ('build%d' % marker)
        subprocess.run(['cc', '-shared', '-fPIC', '-I' + sysconfig.get_paths()['include'],
                        str(c), '-o', str(out)], check=True, capture_output=True)
        paths.append(out.read_bytes())
    assert paths[0] != paths[1]
    return paths


def run_case(tmp, binaries, *, package=False, mismatch=False, opt=True,
             removed=False, preload=False, fail=False):
    suffix = M.EXTENSION_SUFFIXES[0]
    member = 'demopkg/__init__' + suffix if package else 'demopkg' + suffix
    members = H.standard_members()
    members.pop('demopkg/__init__.py'); members.pop('demopkg/sub.py')
    members[member] = binaries[0]
    tag = 'cp%d%d-cp%d%d-%s' % (*sys.version_info[:2], *sys.version_info[:2], sysconfig.get_platform().replace('-', '_').replace('.', '_'))
    members['demopkg-1.0.dist-info/WHEEL'] = ('Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: ' + tag + '\n').encode()
    wheel = H.build_wheel(tmp / ('demopkg-1.0-' + tag + '.whl'), members)
    site = tmp / 'site'; site.mkdir()
    path = site / member; path.parent.mkdir(exist_ok=True)
    path.write_bytes(binaries[int(mismatch)])
    code = 'import demopkg, sys\ndef test_answer():\n assert demopkg.answer() == 42\n'
    if removed:
        code += ' sys.modules.pop("demopkg")\n'
    if fail:
        code += ' assert False\n'
    tests = H.write_test(tmp / 'tests', source=code)
    cmd = [sys.executable, '-m', 'artifact_receipts', 'run']
    if preload:
        cmd = [sys.executable, '-c', 'import demopkg; from artifact_receipts.cli import main; raise SystemExit(main())', 'run']
    cmd += ['--wheel', wheel, '--package', 'demopkg', '--json-report', str(tmp/'report.json')]
    if opt: cmd += ['--observe-native']
    cmd += ['--', '-q', tests]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([H.REPO_ROOT, str(site)]),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    proc = subprocess.run(cmd, cwd=tmp, env=env, text=True, capture_output=True)
    return proc.returncode, json.loads((tmp/'report.json').read_text()), proc.stdout+proc.stderr


@pytest.mark.parametrize('package', [False, True])
@pytest.mark.parametrize('mismatch', [False, True])
def test_real_compiled_module_and_package(tmp_path, binaries, package, mismatch):
    code, report, output = run_case(tmp_path, binaries, package=package, mismatch=mismatch)
    assert code == (13 if mismatch else 0), output
    assert report['tests']['passed']
    assert report['result'] == ('failed' if mismatch else 'verified')
    assert report['native_file_binding'] is True
    assert 'not loaded memory' in report['scope']
    assert 'shared-library dependencies' in report['scope']
    assert report['modules'][0]['bytecode'] is None


@pytest.mark.parametrize('package', [False, True])
def test_native_default_still_incomplete(tmp_path, binaries, package):
    code, report, output = run_case(tmp_path, binaries, package=package, opt=False)
    assert code == 14, output
    assert report['result'] == 'incomplete' and report['tests']['passed']
    assert report['native_file_binding'] is False


def test_removed_native_uses_recorded_loader(tmp_path, binaries):
    code, report, output = run_case(tmp_path, binaries, removed=True)
    assert code == 0, output
    assert report['modules'][0]['imported_not_resident']
    assert report['modules'][0]['loader'] == 'ExtensionFileLoader'


def test_preloaded_native_still_refused(tmp_path, binaries):
    code, report, output = run_case(tmp_path, binaries, preload=True)
    assert code == 14, output
    assert report['modules'][0]['preexisting']


def test_pytest_failure_keeps_exit(tmp_path, binaries):
    code, report, output = run_case(tmp_path, binaries, fail=True)
    assert code == 1, output
    assert report['tests']['exit_code'] == 1


@pytest.mark.parametrize('suffix', M.EXTENSION_SUFFIXES)
@pytest.mark.parametrize('package', [False, True])
def test_exact_suffix_paths(suffix, package):
    name = 'outer.inner'
    filename = '__init__' + suffix if package else 'inner' + suffix
    expected = 'outer/inner/' + filename if package else 'outer/inner' + suffix
    assert native_member_paths(name, '/tmp/' + filename) == [expected]


@pytest.mark.parametrize('filename', ['inner.extra.so', 'inner.cpython-000-unknown.so', 'other.so', 'inner.py', 'inner.SO'])
def test_bad_native_filename_not_guessed(filename):
    assert native_member_paths('outer.inner', '/tmp/' + filename) == []


@pytest.mark.parametrize('other_layout', [False, True])
@pytest.mark.parametrize('loader_ok', [False, True])
def test_binary_same_bytes_need_exact_layout_and_loader(tmp_path, other_layout, loader_ok):
    suffix=M.EXTENSION_SUFFIXES[0]; data=b'unit-test-only-not-executable'
    path=tmp_path/('demopkg'+suffix);path.write_bytes(data)
    members=H.standard_members();members.pop('demopkg/__init__.py');members.pop('demopkg/sub.py')
    member='demopkg/__init__'+suffix if other_layout else 'demopkg'+suffix
    members[member]=data
    wheel=load_wheel(H.build_wheel(tmp_path/'demopkg-1.0-py3-none-any.whl', members))
    obs=_classify_origin('demopkg', str(path), 'ExtensionFileLoader', wheel, {},
                         observe_native=True, standard_native_loader=loader_ok)
    assert obs.verdict == ('unsupported' if not loader_ok else 'mismatch' if other_layout else 'match')


def test_native_children_combination_refused_before_tests(tmp_path):
    proc=subprocess.run([sys.executable,'-m','artifact_receipts','run','--wheel','missing.whl',
                         '--package','x','--observe-native','--require-child','a'],
                        capture_output=True,text=True)
    assert proc.returncode==2
    assert 'with --require-child is not supported' in proc.stderr


def test_removed_origin_without_standard_loader_record_is_not_native(tmp_path, monkeypatch):
    from artifact_receipts.observe import ImportRecorder, observe_packages
    suffix=M.EXTENSION_SUFFIXES[0]
    path=tmp_path/('demopkg'+suffix);path.write_bytes(b'unit-test-only')
    members=H.standard_members();members['demopkg'+suffix]=path.read_bytes()
    wheel=load_wheel(H.build_wheel(tmp_path/'demopkg-1.0-py3-none-any.whl',members))
    recorder=ImportRecorder(['demopkg']);recorder.record('demopkg',str(path))
    monkeypatch.delitem(sys.modules,'demopkg',raising=False)
    obs=observe_packages(['demopkg'],set(),wheel,recorder,observe_native=True)
    assert len(obs)==1 and obs[0].verdict=='unsupported'


def test_loader_class_name_alone_does_not_authorize_native(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from artifact_receipts.observe import ImportRecorder, observe_packages
    suffix=M.EXTENSION_SUFFIXES[0]
    path=tmp_path/('demopkg'+suffix);path.write_bytes(b'unit-test-only')
    members=H.standard_members();members['demopkg'+suffix]=path.read_bytes()
    wheel=load_wheel(H.build_wheel(tmp_path/'demopkg-1.0-py3-none-any.whl',members))
    loader=type('ExtensionFileLoader',(),{})()
    spec=SimpleNamespace(origin=str(path),loader=loader)
    monkeypatch.setitem(sys.modules,'demopkg',SimpleNamespace(__spec__=spec,__file__=str(path)))
    recorder=ImportRecorder(['demopkg']);recorder.record('demopkg',str(path),loader)
    obs=observe_packages(['demopkg'],set(),wheel,recorder,observe_native=True)
    assert len(obs)==1 and obs[0].verdict=='unsupported'


def test_resident_native_requires_matching_recorded_standard_loader(tmp_path, monkeypatch):
    """A terminal loader replacement must not upgrade custom resolution evidence."""
    from types import SimpleNamespace
    from artifact_receipts.observe import ImportRecorder, observe_packages
    suffix=M.EXTENSION_SUFFIXES[0]
    path=tmp_path/('demopkg'+suffix);path.write_bytes(b'unit-test-only')
    members=H.standard_members();members['demopkg'+suffix]=path.read_bytes()
    wheel=load_wheel(H.build_wheel(tmp_path/'demopkg-1.0-py3-none-any.whl',members))
    terminal_loader=M.ExtensionFileLoader('demopkg',str(path))
    spec=SimpleNamespace(origin=str(path),loader=terminal_loader)
    monkeypatch.setitem(sys.modules,'demopkg',SimpleNamespace(__spec__=spec,__file__=str(path)))
    recorder=ImportRecorder(['demopkg'])
    recorder.record('demopkg',str(path),object())
    obs=observe_packages(['demopkg'],set(),wheel,recorder,observe_native=True)
    assert len(obs)==1 and obs[0].verdict=='unsupported'
