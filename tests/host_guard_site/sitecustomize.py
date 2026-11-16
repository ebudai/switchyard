"""Loaded by every Python child of a suite guarded by host_state_guard (SYRD-808): the guard, then whatever
sitecustomize the environment had further along the path."""
import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
try:
    import host_state_guard

    host_state_guard.install()
finally:
    sys.path.remove(os.path.dirname(_here))
for _entry in sys.path:
    if _entry and os.path.abspath(_entry) != _here:
        _candidate = os.path.join(_entry, "sitecustomize.py")
        if os.path.isfile(_candidate):
            _spec = importlib.util.spec_from_file_location("_next_sitecustomize", _candidate)
            _spec.loader.exec_module(importlib.util.module_from_spec(_spec))
            break
