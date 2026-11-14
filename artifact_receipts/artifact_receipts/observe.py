"""Observation of selected modules during a pytest run.

Two mechanisms, cross-checked:

1. Recording wrappers around the sys.meta_path finders (stdlib import
   machinery), installed before pytest starts. A find_spec result records
   that the import system actually resolved that module from that origin
   during the run - imports satisfied from a preloaded sys.modules entry
   never reach the finders, and modules imported then removed or replaced
   mid-run are still recorded here even though a terminal scan alone would
   miss or misattribute them.

2. A terminal sys.modules scan at session finish hashes the on-disk source
   file of each resident module and (when present) checks the cached
   bytecode header for consistency with the source bytes.

Honesty boundaries:
- Hashing files after the run binds bytes on disk, not bytes executed at
  import time. A consistent bytecode cache corroborates the source file but
  cannot close that gap (default pyc validity is timestamp+size; PEP 552
  hash-based pycs with check_source unset are trusted without any source
  check). The report scope text says this explicitly.
- Code executed without the import system (exec, runpy of a file path) is
  not an import and is not claimed to be observed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import importlib.machinery
import os
import struct
import sys
from dataclasses import dataclass, field


# Verdicts per observed module
MATCH = "match"                # file bytes identical to >=1 wheel member
MISMATCH = "mismatch"          # file read fine, no wheel member with same bytes
UNSUPPORTED = "unsupported"    # namespace/extension/sourceless/unknown loader
UNREADABLE = "unreadable"      # origin path vanished or unreadable
OBSERVATION_GAP = "observation_gap"  # mechanisms disagree; cannot verify


class _RecordingFinder:
    """Wraps one sys.meta_path finder and records find_spec results for the
    selected packages: a spec origin returned here means the import system
    actually resolved that module from that origin during the run (a
    sys.modules hit never reaches the finders)."""

    _is_recording_finder = True

    def __init__(self, wrapped, recorder):
        self.wrapped = wrapped
        self.recorder = recorder

    def find_spec(self, fullname, path=None, target=None):
        find = getattr(self.wrapped, "find_spec", None)
        spec = find(fullname, path, target) if find else None
        if spec is not None and self.recorder.selected(fullname):
            # origin may be None (namespace portion); record that too.
            self.recorder.record(fullname, spec.origin, spec.loader)
        return spec

    def __getattr__(self, item):
        return getattr(self.wrapped, item)


class ImportRecorder:
    """Records import-system resolutions for selected packages by wrapping
    sys.meta_path finders. Installed before pytest runs."""

    def __init__(self, packages):
        self.packages = tuple(packages)
        self.origins = {}  # name -> set of spec origins (None allowed)
        self.native_resolutions = set()
        self.other_resolutions = set()

    def selected(self, name):
        return any(name == p or name.startswith(p + ".")
                   for p in self.packages)

    def record(self, name, origin, loader=None):
        self.origins.setdefault(name, set()).add(origin)
        target = (self.native_resolutions if type(loader) is
                  importlib.machinery.ExtensionFileLoader else self.other_resolutions)
        target.add((name, origin))

    def install(self):
        if any(getattr(f, "_is_recording_finder", False)
               for f in sys.meta_path):
            return  # already installed (same process reuse)
        sys.meta_path[:] = [
            f if getattr(f, "_is_recording_finder", False)
            else _RecordingFinder(f, self)
            for f in sys.meta_path
        ]

    def resolved_filenames(self):
        """name -> set of non-None origins recorded for it."""
        return {n: {o for o in origins if o}
                for n, origins in self.origins.items()
                if any(origins)}

    def names_seen(self):
        return set(self.origins)

    @property
    def event_count(self):
        return sum(len(v) for v in self.origins.values())


def check_cached_bytecode(origin: str, source_bytes: bytes) -> dict:
    """Inspect the pyc that corresponds to origin, if one exists.

    `consistent` is True only when the pyc header provably matches the
    current source bytes (PEP 552 hash check or timestamp+size match).
    """
    evidence = {"cached_path": None, "scheme": None, "consistent": None,
                "note": None}
    try:
        cached = importlib.util.cache_from_source(origin)
    except (ValueError, NotImplementedError) as e:
        evidence["note"] = "cannot derive cache path: %s" % e
        return evidence
    if not os.path.isfile(cached):
        evidence["note"] = "no cached bytecode file present"
        return evidence
    evidence["cached_path"] = cached
    try:
        with open(cached, "rb") as f:
            header = f.read(16)
        if len(header) < 16:
            evidence["note"] = "short pyc header"
            return evidence
        magic, bit_field = struct.unpack("<II", header[:8])
        if magic != struct.unpack("<I", importlib.util.MAGIC_NUMBER)[0]:
            evidence["note"] = "pyc magic does not match this interpreter"
            return evidence
        if bit_field & 0b1:
            evidence["scheme"] = "hash-based (PEP 552)"
            evidence["checked_source_flag"] = bool(bit_field & 0b10)
            stored = header[8:16]
            actual = importlib.util.source_hash(source_bytes)
            evidence["consistent"] = stored == actual
            if stored == actual and not (bit_field & 0b10):
                evidence["note"] = (
                    "hash matches, but pyc is trusted-without-checking "
                    "(check_source unset): interpreter may have used stale "
                    "bytes if the file changed after import"
                )
        else:
            evidence["scheme"] = "timestamp-based"
            mtime, size = struct.unpack("<II", header[8:16])
            st = os.stat(origin)
            evidence["consistent"] = (
                int(st.st_mtime) == mtime and st.st_size == size
            )
    except OSError as e:
        evidence["note"] = "could not read cache: %s" % e
    return evidence


@dataclass
class ModuleObservation:
    name: str
    origin: str | None
    loader: str | None
    verdict: str
    reason: str = ""
    observed_sha256: str | None = None
    artifact_members: list = field(default_factory=list)  # any byte-identical member
    expected_members: list = field(default_factory=list)  # module-relative paths
    matched_member: str | None = None  # byte-identical AND at expected path
    preexisting: bool = False
    imported_not_resident: bool = False  # imported, then removed mid-run
    bytecode: dict | None = None


def native_member_paths(name, origin):
    """Exact current-interpreter suffix, never a guessed platform/ABI tag."""
    if not isinstance(origin, str):
        return []
    filename = os.path.basename(origin)
    base = name.replace(".", "/")
    leaf = name.rsplit(".", 1)[-1]
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        if filename == "__init__" + suffix:
            return [base + "/" + filename]
        if filename == leaf + suffix:
            return [base + suffix]
    return []


def expected_member_paths(name: str, origin: str | None = None) -> list:
    """Archive-relative source path, preserving package versus module layout.

    Without a usable origin no source layout is established. The importer may
    use arbitrary paths, so unusual filenames are unsupported rather than
    guessed from byte equality alone.
    """
    if not isinstance(origin, str):
        return []
    base = "/".join(name.split("."))
    filename = os.path.basename(origin)
    if filename == "__init__.py":
        return [base + "/__init__.py"]
    if filename == name.rsplit(".", 1)[-1] + ".py":
        return [base + ".py"]
    return []


def _classify_origin(name, origin, loader_name, wheel_info, flags,
                     observe_native=False, standard_native_loader=False) -> "ModuleObservation":
    obs = ModuleObservation(
        name=name, origin=origin, loader=loader_name,
        verdict=UNSUPPORTED, **flags)
    if origin is None:
        obs.reason = ("no file origin (namespace package or built-in); "
                      "namespace/multi-root layouts are unsupported")
        return obs
    if origin in ("built-in", "frozen"):
        obs.reason = "interpreter built-in module"
        return obs
    lower = origin.lower()
    native = lower.endswith((".so", ".pyd")) or loader_name in (
        "ExtensionFileLoader", "AppleFrameworkLoader",
    )
    if native and not (observe_native and standard_native_loader):
        obs.reason = "native binding requires opt-in and the standard ExtensionFileLoader"
        return obs
    if not native and not lower.endswith(".py"):
        obs.reason = ("sourceless or non-source module (%s); unsupported"
                      % os.path.basename(origin))
        return obs
    try:
        with open(origin, "rb") as f:
            data = f.read()
    except OSError as e:
        obs.verdict = UNREADABLE
        obs.reason = "could not read origin file: %s" % e
        return obs
    digest = hashlib.sha256(data).hexdigest()
    obs.observed_sha256 = digest
    obs.expected_members = (native_member_paths(name, origin) if native
                            else expected_member_paths(name, origin))
    obs.artifact_members = wheel_info.members_matching(digest)
    if not obs.expected_members:
        obs.verdict = UNSUPPORTED
        obs.reason = "filename does not establish a supported module/package layout"
        return obs
    matched = sorted(set(obs.artifact_members) & set(obs.expected_members))
    obs.bytecode = None if native else check_cached_bytecode(origin, data)
    if matched:
        # Identity is bytes AND location: the byte-identical member sits at
        # the module's own archive-relative path.
        obs.matched_member = matched[0]
        obs.verdict = MATCH
    elif obs.artifact_members:
        relocated = [m for m in obs.artifact_members
                     if m.split("/")[0].endswith(".data")]
        if relocated and len(relocated) == len(obs.artifact_members):
            # Correct installs of wheels that relocate this module through
            # .data/purelib (or similar) are a supported-byte match at an
            # unsupported location: incomplete, not a provenance failure.
            obs.verdict = UNSUPPORTED
            obs.reason = (
                "bytes match only relocated member(s) %s; .data relocation "
                "is unsupported in this version" % relocated
            )
        else:
            obs.verdict = MISMATCH
            obs.reason = (
                "bytes match wheel member(s) %s but the wheel has no file at "
                "this module's expected path (%s)"
                % (obs.artifact_members, " or ".join(obs.expected_members))
            )
    else:
        obs.verdict = MISMATCH
        obs.reason = "no member of the wheel has these bytes"
    return obs


def observe_packages(packages, preexisting_names, wheel_info,
                     recorder: ImportRecorder | None = None, observe_native=False) -> list:
    """Cross-check recorded import resolutions against the terminal
    sys.modules scan.

    - Resolved and still resident: one observation (terminal origin must
      agree with every origin recorded at resolution; disagreement is an
      observation gap).
    - Resolved then removed/replaced: observed from the recorded origin,
      flagged imported_not_resident. A resident replacement whose origin
      disagrees with the recorded origin is an observation gap.
    - Resident but never resolved during the run and present before it:
      preexisting (sys.modules hit; no loader execution observed).
    - Resident, never resolved, not preexisting: observation gap (the
      finder wrappers should have seen it; fail closed).
    """
    observations = []
    recorded = recorder.resolved_filenames() if recorder else {}
    seen_by_hook = recorder.names_seen() if recorder else set()

    selected_resident = sorted(
        name for name in sys.modules
        if any(name == p or name.startswith(p + ".") for p in packages)
    )
    resident_set = set(selected_resident)

    # 1. Resident modules.
    for name in selected_resident:
        mod = sys.modules[name]
        spec = getattr(mod, "__spec__", None)
        origin = getattr(mod, "__file__", None)
        if spec is not None and spec.origin:
            origin = spec.origin
        loader = spec.loader if spec else None
        loader_name = type(loader).__name__ if loader is not None else None

        if len(recorded.get(name, ())) > 1:
            observations.append(ModuleObservation(
                name=name, origin=origin, loader=loader_name,
                verdict=OBSERVATION_GAP,
                reason=("import system resolved this module from multiple "
                        "distinct origins during the run: %s"
                        % sorted(recorded[name]))))
            continue
        if name in recorded and origin and \
                os.path.abspath(origin) not in {
                    os.path.abspath(f) for f in recorded[name]}:
            obs = ModuleObservation(
                name=name, origin=origin, loader=loader_name,
                verdict=OBSERVATION_GAP,
                reason=("finder resolution recorded origin %s but the resident "
                        "module's origin is %s; the module may have been "
                        "replaced during the run"
                        % (sorted(recorded[name]), origin)))
            observations.append(obs)
            continue
        if name not in seen_by_hook and name not in preexisting_names:
            observations.append(ModuleObservation(
                name=name, origin=origin, loader=loader_name,
                verdict=OBSERVATION_GAP,
                reason=("resident module was never resolved by the import "
                        "system during the run and was not preloaded; the "
                        "observation channel cannot be trusted for it")))
            continue
        obs = _classify_origin(
            name, origin, loader_name, wheel_info,
            {"preexisting": name in preexisting_names},
            observe_native=observe_native,
            standard_native_loader=(
                type(loader) is importlib.machinery.ExtensionFileLoader
                and recorder is not None
                and (name, origin) in recorder.native_resolutions
                and (name, origin) not in recorder.other_resolutions))
        observations.append(obs)

    # 2. Resolved during the run but no longer resident.
    for name in sorted(set(recorded) - resident_set):
        files = sorted(recorded[name])
        origin = files[-1]
        if len(files) > 1:
            observations.append(ModuleObservation(
                name=name, origin=origin, loader=None,
                verdict=OBSERVATION_GAP,
                reason=("import system resolved this module from multiple "
                        "distinct origins during the run: %s" % files)))
            continue
        native_record = (recorder is not None and
                         (name, origin) in recorder.native_resolutions and
                         (name, origin) not in recorder.other_resolutions)
        obs = _classify_origin(name, origin,
                               "ExtensionFileLoader" if native_record else None, wheel_info,
                               {"imported_not_resident": True},
                               observe_native=observe_native,
                               standard_native_loader=native_record)
        if obs.verdict == UNREADABLE:
            obs.reason += (" (module was removed from sys.modules during "
                           "the run and its file is gone)")
        observations.append(obs)

    return observations
