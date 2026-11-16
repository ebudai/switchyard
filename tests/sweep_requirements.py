"""Where each suite can run in a cold sweep, and which are known to be load-sensitive (SYRD-815).

A sweep runs every suite in a bwrap sandbox: no network, a private /tmp and /run, a disposable HOME, nothing of the
invoking pane's environment. Twenty suites were red there on every base and candidate (SYRD-806), each for something
only the host can give, so each guarded nothing while it was red. They are declared here with the reason, and
`tests/sweep.py` runs them on the host instead -- still cold: env -i, a disposable HOME (or, for a suite that calls
`live_boundary.isolate()`, none, so it builds its own), and a temporary directory other accounts can traverse.

Declare a suite here only for a reason the sandbox cannot give, measured, never to hide a failure.
"""

from __future__ import annotations

#: Why a suite needs the host, and the suites that do. Each reason names what the sandbox lacks.
HOST_ONLY: dict[str, tuple[str, ...]] = {
    # bwrap's own user namespace refuses a nested one ("newuidmap: Could not set caps"), or the suite must be uid 0
    # of a namespace it maps itself. live_boundary_test checks each such suite isolates itself first (SYRD-819).
    "user namespaces": (
        "board_skill_staged_bundle_test",
        "board_ui_delegation_test",
        "image_handoff_two_identity_test",
        "install_switchyard_release_marker_test",
        "legacy_presentation_launch_test",
        "legacy_presentation_migration_test",
        "live_boundary_sentinel_test",
        "live_boundary_test",
        "new_caller_decided_owner_test",
        "new_decided_owner_test",
        "new_output_dir_root_alone_test",
        "new_owner_name_test",
        "new_project_link_paths_test",
        "new_uid0_owner_test",
        "onboarding_release_provenance_test",
        "owner_runtime_dir_test",
        "presentation_state_boundary_test",
        "project_state_acl_boundary_test",
        "projection_identity_test",
        "release_bytecode_boundary_test",
        "release_status_cross_account_test",
        "release_status_trampoline_test",
        "root_owner_from_record_test",
        "root_plan_identity_test",
        "root_residual_paths_test",
        "root_residuals_two_test",
        "root_role_account_test",
        "size_review_cache_ownership_test",
        "tailnet_user_authority_test",
        "tenant_control_bridge_e2e_test",
        "worker_pool_cross_account_test",
        "workflow_layout_owner_test",
        "workflow_projection_ownership_test",
    ),
    # ACLs and owners naming ids outside bwrap's map are refused with EINVAL.
    "ids outside a sandbox's map": (
        "desktop_socket_rename_test",
        "privileged_job_reconcile_test",
    ),
    # Inside bwrap a root-owned file shows the overflow uid, so a check that a path is root's cannot pass.
    "root-owned host files": (
        "board_ui_record_test",
        "team_launcher_installed_release_deploy_test",
    ),
    # --tmpfs /run hides it.
    "the system bus": (
        "polkit_dependency_test",
    ),
    # Chromium's own sandbox needs namespaces bwrap does not give it.
    "a browser": (
        "board_write_token_rotation_browser_test",
    ),
}

#: Suites that call `live_boundary.isolate()` at the top of main() rather than at module level, each for a measured
#: reason. A sweep treats them as isolating suites: no HOME, and the live pane's two store names (SYRD-820). Each may
#: import only the standard library at module level; sweep_test checks it.
ISOLATES_IN_MAIN: dict[str, str] = {
    "live_boundary_test": "its child modes run before isolate(), to show the guard reaches a namespace child through "
                          "sitecustomize alone",
}

#: Suites measured green alone and red only under a parallel sweep's load. A sweep retries each once, alone, before
#: calling it red, and says it did.
LOAD_SENSITIVE: tuple[str, ...] = (
    "tenant_deploy_identity_test",
    "ticket_board_declarative_workflow_test",
    "uat_panel_browser_test",
)


def host_reason(suite: str) -> str:
    """Why `suite` runs on the host in a sweep, or '' when the sandbox can run it."""
    stem = suite.rsplit("/", 1)[-1].removesuffix(".py").removesuffix(".sh")
    return next((reason for reason, suites in HOST_ONLY.items() if stem in suites), "")


def load_sensitive(suite: str) -> bool:
    return suite.rsplit("/", 1)[-1].removesuffix(".py").removesuffix(".sh") in LOAD_SENSITIVE


def isolates_in_main(suite: str) -> bool:
    return suite.rsplit("/", 1)[-1].removesuffix(".py").removesuffix(".sh") in ISOLATES_IN_MAIN
