"""Controls for fail-open resolution loss and conservative wheel rejection."""
from pathlib import Path
import importlib.util
import sys

import pytest
import harness as H
from artifact_receipts.observe import ImportRecorder, observe_packages
from artifact_receipts.wheel import load_wheel, WheelError


def test_removed_namespace_resolution_is_incomplete(tmp_path):
    wheel = H.build_wheel(tmp_path / 'demopkg-1.0-py3-none-any.whl', H.standard_members())
    pkg = Path(H.install_to(tmp_path / 'site'))
    (pkg / 'ns').mkdir()
    tests = H.write_test(tmp_path / 'tests', source=(
        "import sys, demopkg, demopkg.ns\n"
        "del sys.modules['demopkg.ns']\n"
        "def test_ok(): assert demopkg.VALUE == 1\n"))
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests, pythonpath=[tmp_path / 'site'])
    assert code == 14, out
    assert report['result'] == 'incomplete'
    assert report['tests']['passed'] is True
    ns = next(m for m in report['modules'] if m['name'] == 'demopkg.ns')
    assert ns['verdict'] == 'unsupported'
    assert ns['imported_not_resident'] is True


@pytest.mark.parametrize('originless_value', [None, ''])
@pytest.mark.parametrize('resident', [False, True])
@pytest.mark.parametrize('namespace_first', [False, True])
def test_mixed_originless_and_file_resolutions_are_a_gap(tmp_path, monkeypatch, resident, namespace_first, originless_value):
    name = 'demopkg'
    origin = str(tmp_path / '__init__.py')
    Path(origin).write_bytes(H.INIT_A)
    recorder = ImportRecorder([name])
    for recorded in ([originless_value, origin] if namespace_first else [origin, originless_value]):
        recorder.record(name, recorded)
    if resident:
        import types
        mod = types.ModuleType(name)
        mod.__file__ = origin
        mod.__spec__ = importlib.util.spec_from_file_location(name, origin)
        monkeypatch.setitem(sys.modules, name, mod)
    else:
        monkeypatch.delitem(sys.modules, name, raising=False)
    wheel = load_wheel(H.build_wheel(tmp_path / 'demopkg-1.0-py3-none-any.whl', H.standard_members()))
    rows = observe_packages([name], set(), wheel, recorder)
    assert len(rows) == 1
    assert rows[0].verdict == 'observation_gap'
    assert rows[0].imported_not_resident is (not resident)


def test_safe_explicit_directory_entries_are_accepted(tmp_path):
    members = H.standard_members()
    # Directory entries carry no file digest; bind actual file bytes only.
    rows = [f'{n},sha256={H.b64_sha256(data)},{len(data)}' for n, data in members.items()]
    members['demopkg/'] = b''
    members['demopkg-1.0.dist-info/'] = b''
    wheel = H.build_wheel(tmp_path / 'demopkg-1.0-py3-none-any.whl', members, record_rows=rows)
    H.install_to(tmp_path / 'site')
    tests = H.write_test(tmp_path / 'tests')
    code, report, out = H.run_tool(tmp_path, wheel, testdir=tests, pythonpath=[tmp_path / 'site'])
    assert code == 0, out
    assert report['result'] == 'verified'


@pytest.mark.parametrize('metadata_name', ['foo.bar', 'Foo--Bar', 'FOO_._bar'])
def test_normalized_metadata_names_match(tmp_path, metadata_name):
    members = {'foo_bar/__init__.py': b'',
               'foo_bar-1.0.dist-info/METADATA': ('Metadata-Version: 2.1\nName: '+metadata_name+'\nVersion: 1.0\n').encode(),
               'foo_bar-1.0.dist-info/WHEEL': H.WHEEL_MD}
    wheel = H.build_wheel(tmp_path / 'foo_bar-1.0-py3-none-any.whl', members, dist_info='foo_bar-1.0.dist-info')
    assert load_wheel(wheel).name == 'foo_bar'


@pytest.mark.parametrize('entry', ['/bad/', 'a/../bad/', 'a//bad/', 'a\\bad/', '../', './bad/', 'a/./bad/'])
def test_unsafe_directory_entries_still_rejected(tmp_path, entry):
    members = H.standard_members()
    rows = [f'{n},sha256={H.b64_sha256(data)},{len(data)}' for n, data in members.items()]
    members[entry] = b''
    wheel = H.build_wheel(tmp_path / 'demopkg-1.0-py3-none-any.whl', members, record_rows=rows)
    with pytest.raises(WheelError, match='unsafe member paths'):
        load_wheel(wheel)


@pytest.mark.parametrize('resident', [False, True])
def test_non_file_resolution_transitions_are_not_silently_dropped(tmp_path, monkeypatch, resident):
    # Other origin strings are already retained; mixing None must not cause
    # sorting/type failures or erase unsupported evidence.
    recorder = ImportRecorder(['demopkg'])
    recorder.record('demopkg', None)
    recorder.record('demopkg', 'built-in')
    if resident:
        import types
        mod = types.ModuleType('demopkg')
        mod.__spec__ = importlib.util.spec_from_loader('demopkg', loader=None, origin='built-in')
        monkeypatch.setitem(sys.modules, 'demopkg', mod)
    else:
        monkeypatch.delitem(sys.modules, 'demopkg', raising=False)
    wheel = load_wheel(H.build_wheel(tmp_path / 'demopkg-1.0-py3-none-any.whl', H.standard_members()))
    rows = observe_packages(['demopkg'], set(), wheel, recorder)
    assert len(rows) == 1
    assert rows[0].verdict == 'observation_gap'


@pytest.mark.parametrize('name', ['different.bar', 'foo.baz'])
def test_normalization_does_not_accept_other_names(tmp_path, name):
    members = {'foo_bar/__init__.py': b'',
               'foo_bar-1.0.dist-info/METADATA': ('Metadata-Version: 2.1\nName: '+name+'\nVersion: 1.0\n').encode(),
               'foo_bar-1.0.dist-info/WHEEL': H.WHEEL_MD}
    wheel = H.build_wheel(tmp_path / 'foo_bar-1.0-py3-none-any.whl', members, dist_info='foo_bar-1.0.dist-info')
    with pytest.raises(WheelError, match='METADATA Name'):
        load_wheel(wheel)


@pytest.mark.parametrize('filename_name,dist_name', [('Foo.Bar', 'foo_bar'), ('foo_bar', 'Foo.Bar')])
def test_legacy_filename_and_dist_info_names_are_compared_normalized(tmp_path, filename_name, dist_name):
    di = dist_name + '-1.0.dist-info'
    members = {'foo_bar/__init__.py': b'',
               di+'/METADATA': b'Metadata-Version: 2.1\nName: foo-bar\nVersion: 1.0\n',
               di+'/WHEEL': H.WHEEL_MD}
    wheel = H.build_wheel(tmp_path / (filename_name+'-1.0-py3-none-any.whl'), members, dist_info=di)
    assert load_wheel(wheel).dist_info == di
