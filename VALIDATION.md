# Validation and support boundaries

Candidate preparation, November 14, 2026. This is one original tool with optional
required-child and native-file binding modes, not several independent projects.
No public repository, hosted CI, registry publication or third-party adoption is
claimed by these local results.

## Checked environment

- Actual CPython 3.10.12, Linux x86-64, pytest 8.3.5 and 9.2.0, each in an
  installed-wheel environment outside the source checkout.
- Each run passes all 114 existing tests with no skips and one expected warning
  from the deliberate duplicate-ZIP-member fixture. Native tests compile actual
  small C modules and packages with `cc`; they are not mocked import successes.
- Each offline demo returns verified exit 0 for matching source files and exit
  13 for a shadowing checkout while its functional pytest test still passes.
  Existing shell controls independently reject unexpected mismatch exits 0 and
  14 and accept 13.
- All nine runtime Python files remain byte-identical to the previously retained
  implementation. Release preparation changes documentation, MIT licensing,
  build metadata and installed qualification/CI support, not observation logic.
- The qualification script verifies the exact runtime file list and every
  source/wheel/installed Python file's bytes, records interpreter/pytest versions,
  origin and wheel SHA-256, then runs a tests/demo-only copy in a fresh directory.
  It rejects an import from the source tree and a wheel with differing runtime
  bytes before running tests. A passing test run without that byte check is not
  the installed-artifact claim.
- Wheel and source distribution build successfully. The included GitHub Actions
  workflow repeats the two checked pytest versions on Python 3.10. Its presence
  is not evidence that hosted CI has run or passed.

The declared `Python >=3.10` and `pytest >=8` metadata are dependency floors,
not a guarantee for every later version. Other interpreters, versions, operating
systems, free-threading, arbitrary import hooks, hostile code, distributed workers
and external-package rehearsals are not qualified by this matrix. Native tests
require a compiler and development headers; no Windows or macOS native claim.

## What the controls establish

Positive and negative cases cover correct files, checkout shadowing, changed
installed files, exact module/package paths, wheel identity/RECORD conflicts,
preloaded or unobserved modules, removed/replaced/multi-origin modules, bytecode
contradictions, explicitly required child receipts, report-write failures and
opt-in standard-loader native files. Full details are in README.md and tests.

The result binds **on-disk files** to wheel member bytes. It does not establish
executed Python bytecode, loaded native memory, shared-library dependencies,
semantic correctness, coverage, provenance attestations or a security boundary.
An unchecked/stale bytecode cache or a file replaced after native loading can
decouple execution from the matching file on disk. Cooperative child receipts
can be forged by tested code with the same user permissions. Unregistered
children/grandchildren are neither observed nor detected. Captured child output
is not memory-capped, and timeout does not kill an entire descendant process tree.

Reports may reveal module names, local paths, interpreter location, script paths
and arguments. Review reports before sharing them. Require current exit 0 plus
the current complete `verified` report; a stale report alone is not success.
