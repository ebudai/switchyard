"""Root's trusted releases and an attesting board, for suites that run the identity cutover (SYRD-700).

The cutover renders the board's units for the release it deploys, from root's trusted copy of it, and attests the
board once it serves under them. These suites stage their releases under the test install root, from fixture
repositories that do not carry the renderer, and have no running board for `board_identity.effective_unit` to
read. The release-age refusal itself is catalogued_deploy_release_test's.
"""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace

#: What root renders the installed board and canary from, shaped like provisioned units.
STAGED_SHAPE = {
    "board.service": ("User=boardsvc\nExecStart=/usr/bin/python3 /srv/{project}-ticketboard-live/current/scripts/"
                      "ticket-board.py --unix-socket /run/{project}-ticket-board/ticket-board.sock\n"
                      "RuntimeDirectory={project}-ticket-board\nRuntimeDirectoryMode=0750\n"
                      "Environment=TICKET_BOARD_PROJECT={project}\n"
                      "Environment=TICKET_BOARD_DATABASE_URL=postgresql:///{project}\n"),
    "canary.service": "User=boardsvc\nRuntimeDirectory={project}-ticket-board-canary\n",
}


def staged_unit(unit: str, project: str) -> str:
    """A generated unit as the artifacts phase leaves it."""
    return f"[Service]\n# generated {unit}\n" + STAGED_SHAPE.get(unit.rsplit("-", 1)[-1], "").format(project=project)


@contextlib.contextmanager
def exact_release_host():
    from team_launcher_test_helpers import TEST_SWITCHYARD_SHARED_INSTALL_ROOT

    from scripts import tenant_release_deploy
    from scripts.ticket_board import board_identity, privileged_operations

    saved = (privileged_operations.SHARED_RELEASES, privileged_operations.TRUSTED_OWNER_UID,
             board_identity.effective_unit, tenant_release_deploy.exact_release_runtime)
    privileged_operations.SHARED_RELEASES = str(TEST_SWITCHYARD_SHARED_INSTALL_ROOT / "releases")
    privileged_operations.TRUSTED_OWNER_UID = os.getuid()
    board_identity.effective_unit = lambda project, **_kwargs: SimpleNamespace(release_commit="")
    tenant_release_deploy.exact_release_runtime = lambda release: True
    try:
        yield
    finally:
        (privileged_operations.SHARED_RELEASES, privileged_operations.TRUSTED_OWNER_UID,
         board_identity.effective_unit, tenant_release_deploy.exact_release_runtime) = saved
