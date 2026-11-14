# artifact-receipts (v0.1.0)

Source-installable local tool. No package-registry release or third-party adoption is claimed.

Binds the on-disk source files of packages imported during a pytest run to
the bytes of a specific local wheel. It answers one question: *when my
tests ran, were the resolved source files of this package byte-identical to
the files in this exact wheel artifact?* It binds bytes on disk to the
artifact; it does not claim which bytes were executed at import time (see
the honesty boundaries below).

This is accident detection, not attestation. Typical accident: a source
checkout on `sys.path` silently shadows the installed wheel during tests (the
shape reported in pypa/cibuildwheel#1534), or an installed file was modified
after install. Name/version checks pass in both cases; this tool does not.

This is a narrow packaging/test-isolation diagnostic, not a new provenance
algorithm or a replacement for clean test environments, artifact verification,
coverage measurement or supply-chain attestation.

## Install from source

```sh
python -m pip install .
```

Use Python 3.10 or later with pytest installed. The locally checked support
matrix is stated in VALIDATION.md; an open-ended Python/pytest dependency floor
is not proof of compatibility with every version or platform.

## Usage

```
python -m artifact_receipts run \
    --wheel dist/mypkg-1.0-py3-none-any.whl \
    --package mypkg [--package mypkg.plugins] \
    [--json-report receipts.json] [--human-report receipts.txt] \
    -- [pytest args]
```

Run it with the interpreter/environment you would run pytest with. It calls
`pytest.main` in-process: nothing is installed, `sys.path` is not modified,
and your pytest args pass through after `--`.

## What "verified" means (and does not)

`verified` means: every selected package was resolved by the import system
during the run (recorded by wrappers around the `sys.meta_path` finders),
every observed module's on-disk source file is byte-identical (SHA-256) to
the member at that module's own archive-relative path in the given wheel
(whose own SHA-256 is reported; package `__init__.py` and plain-module `.py`
layouts are distinct, even if their bytes match. Matching only some other member, e.g. a
dist-info file with identical bytes, is a mismatch, not a match), the
resolution records and the terminal `sys.modules` scan agree (a module
resolved from more than one distinct origin during the run is an observation
gap, resident or not; a module imported then removed mid-run is still bound
from its recorded origin; a replaced module whose resident origin disagrees
with the recorded origin is an observation gap), no module was preloaded
before the run, no unsupported module kind was involved, and no cached
bytecode contradicts its source file.

The wheel's identity must be unambiguous before any mapping is trusted:
filename, the single `.dist-info` directory, and METADATA `Name`/`Version`
must agree with each other. RECORD must parse; supplied digests must use
SHA-256 and match the archive member bytes. A supplied digest for a nonexistent
archive member, or an identity/digest conflict, makes the wheel invalid
(`incomplete`). Members omitted from RECORD produce a warning, not a failure;
entries without a digest do not authenticate their member. Byte binding uses
the actual ZIP member bytes, not the completeness of RECORD.

It does **not** mean:

- that those source bytes were the bytes executed at import time. With the
  default timestamp+size pyc validity, an attacker or accident that preserves
  size and mtime can make the interpreter execute stale bytecode while the
  source file matches; PEP 552 hash-based pycs with `check_source` unset are
  trusted without any source check. Post-hoc file hashing cannot see either.
  The report carries bytecode-cache evidence and flags trusted-without-
  checking pycs, and the scope string says this explicitly. `verified` is a
  statement about byte binding, never about executed bytes.
- that every import in the process was observed. Code run without the import
  system (exec, runpy of a path) leaves no resolution record.
- anything about build provenance, semantic equivalence, coverage, or
  whether the code behaved honestly.
- anything about nonstandard custom-loader source filenames, extension/native modules, namespace packages, sourceless
  imports, `pytest-xdist` workers, or modules the wheel relocates through
  `.data/purelib`: these are detected and refused (overall `incomplete`,
  never `verified`; a relocated member is never a provenance failure when
  the install is otherwise correct).
- anything outside the pytest process itself. Imports of selected packages
  in forked, spawned, or subprocess children are NOT observed and NOT
  detected: the tool cannot see them, and every report carries a standing
  warning saying so. This is a stated scope limit, not a detection claim.

## Results and exit codes

- `verified` (0): binding established and tests passed.
- `failed` (13): at least one observed module's bytes are not in the wheel
  at its own archive-relative path.
- `incomplete` (14): evidence insufficient (nothing resolved during the run,
  preloaded module, unsupported kind/mode, observation gap between resolution
  records and the terminal module table, bytecode cache contradicting its
  source file, invalid wheel, report-write failure). An invalid wheel
  (duplicate members, path traversal, malformed or digest-mismatched RECORD,
  unsafe paths, filename/dist-info/METADATA identity conflict) yields
  `incomplete` without trusting any mapping from it; members are never
  extracted.
- A failing pytest run keeps pytest's own exit code no matter what
  provenance says; a report-write failure cannot erase a test failure and
  cannot leave a run `verified`.

Exit codes 13 and 14 sit outside pytest's own 0-5 range, so a consumer
mapping exit codes can never confuse a provenance result with a pytest
internal error (3) or usage error (4).

## Controls

`tests/test_controls.py` runs the negative controls in clean subprocesses: correct install, checkout shadowing,
same-path changed bytes, submodule-only shadowing, nothing imported,
namespace package, failing pytest with matching and mismatching provenance,
duplicate zip members, path traversal, malformed RECORD, report-write
failure, preloaded module (import-system 5.3.1 sys.modules hit), module
imported then removed from sys.modules mid-run (still bound from the recorded
origin), module imported then replaced mid-run (observation gap), module
resolved from two distinct origins while resident (observation gap), module
bytes matching only a non-module wheel member (mismatch: binding requires
the module-relative path), dist-info/filename and METADATA/filename identity
conflicts (invalid wheel), `.data/purelib` relocation on a correct install
(unsupported, not failed), size/mtime-preserving tamper, stale pyc executed
while the file matches (documented boundary), and unchecked hash-based pyc.

## Demo

`demo/run_demo.sh` (offline) shows ordinary pytest passing against a
shadowing source checkout while artifact-receipts fails the run.

## Validation

`ci/qualify_installed.py SOURCE_ROOT EXACT_WHEEL` checks the currently installed
tool's location and runtime-file bytes against the source and exact wheel, then
runs a tests/demo-only copy outside the checkout. It does not install anything.
Use a clean environment and install the wheel first. The tests create tiny
synthetic wheel fixtures; native tests compile small C extensions and need `cc`
and Python development headers on Linux. There is no network access in these
controls. Current results and limits are in VALIDATION.md.

## Experimental: explicitly required Python children

The default command still covers the pytest process only. It does not discover
arbitrary subprocess, fork, spawn or xdist imports.

For tests that deliberately run a Python worker, declare each required job:

```sh
python -m artifact_receipts run --wheel dist/example-1.0-py3-none-any.whl \
  --package example --require-child worker --json-report receipt.json -- tests
```

Use the helper in the test that launches that script:

```python
import example
from artifact_receipts.children import run_observed_python


def test_worker():
    assert example is not None
    result = run_observed_python("worker", "tests/worker.py", ["--mode", "check"])
    assert result.returncode == 0, result.stderr
```

The worker is an ordinary Python script that imports the selected package. The
helper runs a fresh interpreter with import observation installed before the
script, and returns a `subprocess.CompletedProcess` with captured text stdout and
stderr. Optional keyword arguments are `python`, `cwd`, `env` and `timeout`
(default 60 seconds, finite and positive). Script arguments are passed without
a shell. `env`, when given, replaces the environment, rather than adding keys.
The interpreter must already have this tool and the selected package installed;
nothing is installed and the parent import path is not changed. The child's
script-directory lookup matches ordinary `python path/to/script.py` behavior,
including a script-local package shadowing its installed copy.

Each required label must run exactly once. Missing, repeated, unfinished,
malformed or wrong-run receipts are **incomplete**. An ignored child return code
does not hide its mismatch or missing evidence: parent aggregation still fails
closed. Each declared child must import every selected package, independently
of the parent's imports. Parent tests must still import those packages too.
Pytest failures keep their own exit code. Otherwise a mismatch in either parent
or required child returns 13; insufficient child evidence returns 14.

Receipts cross-check run, label, launched PID, wheel hash, selected packages,
process exit and module verdicts. The parent verifies matching module paths and
digests against its wheel again. Reports put each child's evidence under
`children`, separately from parent `modules`, and include a `child_scope`.

This is cooperative build evidence, **not a security boundary**. Tested code
runs as the same user and can forge receipt files. A nonce prevents accidental
reuse, not malicious forgery. Observation still binds source bytes on disk after
execution, not the exact bytes executed. Unregistered children and grandchildren
are neither observed nor detected. Children cannot recursively register jobs
in the parent session. A timeout kills only the named process, not its whole
process tree; descendants may survive and are the caller's responsibility.
Captured output has no memory cap, so this helper is not for unbounded output.
Receipt JSON is capped at 8 MiB of characters per file. Reports include script
paths and arguments; inspect them before sharing.

## Consuming reports safely

Require exit code 0 from the current invocation **and** its matching complete
report with `result: verified`. A report alone is never a completion marker.
Missing reports, nonzero exits, malformed or stale reports must not count as
verified, even if another file contains an older `verified` result. Use a fresh
output directory per run and retain the process exit status with the artifacts.

Detected report-write failures change a previously verified in-memory result to
incomplete. Output files are replaced individually after complete writes; after
a detected failure the tool attempts to rewrite requested files with that result,
or remove files it cannot repair. Recovery is best-effort. Filesystem errors can
block both rewriting and removal, and abrupt exit, signal or power loss can
interrupt recovery or prevent reports entirely. Multiple report files are not
an atomic transaction. The command's exit status is required even when a report
file exists. Byte mismatches and pytest failures keep their own failure status.


## Opt-in native file binding

`--observe-native` adds file binding for the current interpreter's standard
`importlib.machinery.ExtensionFileLoader` only. A native module's exact
filename must use a suffix in that interpreter's `EXTENSION_SUFFIXES` and
match its own wheel-relative module or package `__init__` path. Bytes matching
a different member or the other module/package layout do not pass. The report
sets `native_file_binding` and gives the narrower native scope explicitly.

This is an optional file-binding mode, not a loaded-code attestation.
Without the flag, extension modules still make the result incomplete. The flag
cannot be combined with `--require-child`; native child capture is not supported.
Custom native loaders, iOS framework relocation and `.data` relocation remain
unsupported. Preloaded modules, contradictory origins and unobserved imports
still cannot earn a verified result.

A native match binds the extension **file on disk** to the exact wheel member.
It does not bind the image loaded in memory, capture system or bundled shared
library dependencies, validate wheel tags or ABI/platform compatibility, attest
build provenance, or establish code safety. A file replaced after loading can
have different bytes from the image still mapped in memory. A test importing
an extension with a passing functional result is not evidence that all of its
native dependencies came from the wheel.

Native compilation controls establish only the tested Linux interpreter and
standard loader cases. Windows/macOS, shared-library dependency binding and
hostile-process behavior are not qualified here. No external-wheel rehearsal,
third-party adoption or upstream validation is claimed.
