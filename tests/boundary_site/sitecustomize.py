"""Carries the test-run boundary into every Python child of an isolated suite (SYRD-627).

`live_boundary.isolate()` puts this directory on PYTHONPATH, so every Python
process the suite starts -- a namespace child under `unshare` included --
imports it at startup and installs the same guard. Outside an isolated run the
variable it reads is unset and it does nothing.
"""

import os

if os.environ.get("SWITCHYARD_TEST_LIVE_ROOTS"):
    import sys

    _tests = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _tests not in sys.path:
        sys.path.append(_tests)
    import live_boundary

    live_boundary.install_guard_from_env()
