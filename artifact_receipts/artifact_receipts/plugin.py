"""pytest plugin: installs the import audit recorder, snapshots sys.modules,
detects unsupported execution modes, observes selected modules at finish.
"""

from __future__ import annotations

import sys

from . import observe


class ReceiptsPlugin:
    def __init__(self, packages, wheel_info, observe_native=False):
        self.packages = list(packages)
        self.observe_native = observe_native
        self.wheel_info = wheel_info
        self.recorder = observe.ImportRecorder(self.packages)
        self.preexisting_names = set()
        self.mode_problems = []   # unsupported execution modes -> incomplete
        self.observations = []

    def pytest_configure(self, config):
        # Anything selected already in sys.modules was NOT loaded by this
        # pytest run (import system 5.3.1: sys.modules satisfies import
        # without running a loader) and cannot earn a verified result.
        self.preexisting_names = {
            name for name in sys.modules
            if any(name == p or name.startswith(p + ".") for p in self.packages)
        }
        # The recorder only sees import resolutions from this point on;
        # that is why preexisting modules are downgraded rather than trusted.
        self.recorder.install()
        # xdist runs tests in worker processes; this controller's finder
        # wrappers and sys.modules do not see those imports. Refuse xdist.
        try:
            n = config.getoption("numprocesses")
        except (ValueError, AttributeError):
            n = None
        if n:
            self.mode_problems.append(
                "pytest-xdist active (-n %s): imports happen in worker "
                "processes and are not observable here" % n
            )

    def pytest_sessionfinish(self, session, exitstatus):
        self.observations = observe.observe_packages(
            self.packages, self.preexisting_names, self.wheel_info,
            recorder=self.recorder, observe_native=self.observe_native,
        )
