"""Named release references shared across modules.

`DEFAULT_TENANT_RELEASE_DEPLOY_REF` is the ref a tenant release deploys when
none is named. Several functions take it as a default argument, in
`scripts/team_launcher.py` and `scripts/role_identity_cutover.py`, so it lives
in this leaf that both import, and each default binds the same object.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-308); `team_launcher`
still exports it.
"""

from __future__ import annotations


DEFAULT_TENANT_RELEASE_DEPLOY_REF = "origin/main"
