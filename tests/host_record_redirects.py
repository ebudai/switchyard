"""Root's records the launcher suites' commands consult, made the suite's own for the whole run (SYRD-808).

Imported once, for its effect, by `team_launcher_test_helpers`, beside the sandboxed install roots and fixture
displays it already sets. Answered by this host's records, a launcher suite read mefp's live board unit and wrote the
real /etc/switchyard/provision/mefp; others read root's trusted registry, the board supervisor's
records, the host's agy credential setting, the tenant-control grants and the shared release venv. Each module's own
name for those -- the ones read at call time -- points at an empty tree of the run's, where a case writes what it
needs. Paths spelled inline (`Path("/etc/systemd/system") / unit`) are not covered: the suites that must not reach the
host also run under `host_state_guard`, which names any such read.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

#: The run's own stand-in for the host, beside the install roots `team_launcher_test_helpers` redirects.
TEST_ROOT = Path(tempfile.gettempdir()) / f"switchyard-test-{os.getpid()}"

# What `team_launcher_test_helpers` sets before it imports this, for a suite that imports this alone (SYRD-818):
# the shared install, the project registry, and the installed wrapper whose target a status reads. The helpers'
# own values stand where they are set. (The tenant-control tree is the helpers' to move: a suite of rendered
# operator commands asserts its production path.)
for _name, _value in (("SWITCHYARD_SHARED_INSTALL_ROOT", TEST_ROOT / "opt" / "switchyard"),
                      ("SWITCHYARD_PROJECT_REGISTRY_DIR", TEST_ROOT / "registry"),
                      ("SWITCHYARD_INSTALL_PATH", TEST_ROOT / "bin" / "switchyard")):
    os.environ.setdefault(_name, str(_value))

import scripts.team_launcher as team_launcher  # noqa: E402

# The host's installed system units: a role change reads the board's installed unit to tell root's exact-release
# unit from a legacy one, and for an exact one rewrites root's staged records. Answered by this host's tenant of the
# same name, a suite wrote the real /etc/switchyard/provision/mefp (SYRD-808). The names those reads go through
# point at an empty directory of the suite's.
TEST_SYSTEM_UNIT_DIR = TEST_ROOT / "systemd-system"
TEST_SYSTEM_UNIT_DIR.mkdir(parents=True, exist_ok=True)
from scripts.ticket_board import board_identity as _board_identity  # noqa: E402
from scripts.ticket_board import release_units as _release_units  # noqa: E402
from scripts.ticket_board import rollout_journal as _rollout_journal  # noqa: E402

_release_units.SYSTEM_UNIT_DIR = _rollout_journal.SYSTEM_UNIT_DIR = _board_identity.UNIT_DIR = TEST_SYSTEM_UNIT_DIR
# And the rest of root's host records these suites' commands consult (SYRD-808), each by its module's own name: the
# trusted registry an owner is judged by, the board supervisor's records, the host's agy
# credential setting, the tenant-control tree's grants (read by these constants, not SWITCHYARD_TENANT_CONTROL_ROOT),
# the shared release venv and root's rollout journal. None of them is this host's; each is empty unless a case writes one.
import dataclasses as _dataclasses  # noqa: E402

from scripts import agy_credential as _agy_credential  # noqa: E402
from scripts import board_services as _board_services  # noqa: E402
from scripts import board_supervisor_spec as _board_supervisor_spec  # noqa: E402
from scripts import owner_writes as _owner_writes  # noqa: E402
from scripts import tenant_control_helper as _tenant_control_helper  # noqa: E402
from scripts.ticket_board import project_provision as _project_provision  # noqa: E402

TEST_HOST_RECORDS = TEST_ROOT / "host-records"
for _directory in ("trusted-registry", "board-supervisor/config", "board-supervisor/state",
                   "board-supervisor/run"):
    (TEST_HOST_RECORDS / _directory).mkdir(parents=True, exist_ok=True)
_owner_writes.TRUSTED_REGISTRY = (TEST_HOST_RECORDS / "trusted-registry", str(TEST_HOST_RECORDS / "trusted-registry"))
_supervisor_paths = _dataclasses.replace(
    _board_supervisor_spec.Paths(), config_root=TEST_HOST_RECORDS / "board-supervisor/config",
    state_root=TEST_HOST_RECORDS / "board-supervisor/state", runtime_root=TEST_HOST_RECORDS / "board-supervisor/run",
    unit_dir=TEST_SYSTEM_UNIT_DIR, drop_in_dirs=(TEST_SYSTEM_UNIT_DIR,))
_board_services.board_supervisor_paths = lambda: _supervisor_paths
_agy_credential.DEFAULT_AGY_CREDENTIAL_SETTING_PATH = TEST_HOST_RECORDS / "agy-credential.json"
if os.environ.get("SWITCHYARD_TENANT_CONTROL_ROOT"):
    team_launcher.TENANT_CONTROL_ROOT = _tenant_control_helper.TENANT_CONTROL_ROOT = Path(
        os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"])
    _project_provision.TENANT_CONTROL_ROOT = os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"]
else:
    # Left at its production path, a role's git template is read there: answered as none, as this host's absent
    # tree answered, without reading the host (SYRD-818).
    from scripts import project_config_json as _project_config_json

    _shipped_git_template_env = _project_config_json.role_git_template_env
    team_launcher.role_git_template_env = _project_config_json.role_git_template_env = lambda project: (
        _shipped_git_template_env(project) if os.environ.get("SWITCHYARD_TENANT_CONTROL_ROOT") else {})
os.environ.setdefault("SWITCHYARD_SHARED_PYTHON", str(TEST_ROOT / "opt" / "switchyard" / "venv/bin/python"))
# And root's rollout journal, which a command run as root records each privileged step in.
os.environ.setdefault(_rollout_journal.JOURNAL_ROOT_ENV, str(TEST_HOST_RECORDS / "rollout"))

# Root's conversation adoptions (/var/lib/switchyard): read as root's record or not at all, so a tree of the run's
# reads as none, as this host's absent one did (SYRD-818).
from scripts import conversation_adoption_record as _adoption_record  # noqa: E402

_adoption_record.ROOT = TEST_HOST_RECORDS / "conversation-adoptions"

# And every launch's report looks, among all of the host's processes, for a window root opened for the project:
# here it looks among none, unless a case points PROC_ROOT at a fixture of its own or names a proc root (SYRD-818).
from scripts import presentation_windows as _presentation_windows  # noqa: E402

TEST_PROC_ROOT = TEST_ROOT / "proc"
TEST_PROC_ROOT.mkdir(parents=True, exist_ok=True)
_HOST_PROC = team_launcher.PROC_ROOT
_shipped_unsafe_windows = _presentation_windows.unsafe_root_presentation_windows


def _unsafe_windows_in_fixture(*args, proc_root=None, **kwargs):
    if proc_root is None:
        proc_root = team_launcher.PROC_ROOT if team_launcher.PROC_ROOT != _HOST_PROC else TEST_PROC_ROOT
    return _shipped_unsafe_windows(*args, proc_root=proc_root, **kwargs)


team_launcher.unsafe_root_presentation_windows = _presentation_windows.unsafe_root_presentation_windows = \
    _unsafe_windows_in_fixture
