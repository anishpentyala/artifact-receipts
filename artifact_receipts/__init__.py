"""artifact_receipts: bind pytest-observed imports to a local wheel's bytes.

Verifies that the on-disk source files of selected packages, as resolved by
the import system during the pytest run, are byte-identical to members of a
specific local wheel.

Scope: accidental-environment error detection (e.g. a source checkout
shadowing an installed wheel). Not a security attestation, not a
hostile-code sandbox, and not proof of which bytes were executed at import
time (see README's honesty boundaries).
"""

__version__ = "0.1.0"
