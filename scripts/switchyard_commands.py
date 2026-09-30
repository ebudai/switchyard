"""The `switchyard` verb table: which verbs exist, which need root, the help, and the root check.

`SWITCHYARD_COMMANDS` names every verb `switchyard` recognizes;
`SWITCHYARD_UNPRIVILEGED_COMMANDS` the ones that must never escalate, and
`SWITCHYARD_PRIVILEGED_COMMANDS` -- computed from the two when this module loads
-- the rest. `switchyard_help_text` is the help `switchyard --help` prints, and
`switchyard_invocation_requires_root` the answer the installed wrapper asks
for before it decides whether to cross to root.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-432), in their original
order. The launcher imports this module and re-exports all five names;
`switchyard_main` still calls the launcher's names, and
`scripts/command_crossing.py` still reads the unprivileged set there. The two
functions read the verb tables and the tenant-control check through the
launcher when they run, so a suite that rebinds one there still intercepts it.
The privileged set is computed here, when this module loads, from the same two
objects the launcher re-exports. This module imports `team_launcher` only
inside the functions, when they run.
"""

from __future__ import annotations

from typing import Sequence


SWITCHYARD_COMMANDS = (
    "board-skill",
    "new",
    "register",
    "upgrade",
    # Applies the reviewed repository boundary to a tenant that is already
    # running, and nothing else in the packet (SYRD-175).
    "repair-boundary",
    # Records, shows or withdraws this host's standing desktop approval. The
    # record existed and had one writer, behind an interactive prompt; this is
    # the front door onto it (SYRD-174).
    "approve-desktop",
    # Records an existing tenant's declared workflow as root's own, once, with
    # an operator authorizing it through Polkit and the whole decision in the
    # rollout journal (SYRD-166).
    "adopt-workflow",
    # Installs root's declared workflow onto a tenant whose board is running
    # none. A legacy tenant keeps stages and roles as table rows with no
    # workflow document, so /api/workflow answers null and its Director keeps
    # receiving provisioning-scaffold onboarding (SYRD-240).
    "migrate-workflow",
    # Rebinds a declared workflow's pane roles to the tenant's own panes, when
    # the declaration names runtimes or targets its panes do not register with
    # and so hides them, the Director's authority included (SYRD-262).
    "rebind-workflow-panes",
    # Finishes a project whose `switchyard new` stopped before it was
    # registered, so the installation that already exists can be completed
    # instead of started again: root's artifacts first (SYRD-147), then the
    # registration and role startup the interrupted process never reached
    # (SYRD-155).
    "resume-provision",
    "finish-upgrade",
    "cutover-roles",
    # Reports the publication-key cutover, and on request checks the one thing
    # no push can establish. Its only possible write is the root-owned,
    # non-secret evidence file (SYRD-116).
    "publication-status",
    # Reads the root-owned record of a privileged provisioning or upgrade run.
    # A role reads it directly rather than the User pasting output (SYRD-128).
    "rollout-log",
    # Recovers a disconnected Director display slot from the desktop session
    # that owns the screen. It runs through the tenant-control bridge, which
    # authenticates the operator from SUDO_UID against the tenant's root-owned
    # grant -- the ordinary recovery is gated to the Director's own pane, and
    # that pane is what has gone away (SYRD-239).
    "recover-display",
    # The one front door onto a bounded privileged operation: a catalogued
    # action name and typed values, pre-flown against the installed policy and
    # then asked for through pkexec. It replaces handing a sudo command to the
    # User through chat (SYRD-112).
    "privileged-action",
    # Repoints /opt/switchyard/current at an already-built release, by commit
    # alone: the bounded version of the bare `ln -sfn` the operator packet used
    # to carry. Records the previous target before it moves anything, verifies
    # what landed, and puts the previous target back if it does not (SYRD-112).
    "install-shared-release",
    # Compares the shared release, the tenant's deployed board, the live build
    # and both upgrade journals, and -- only as root, and only after re-proving
    # the deployment from the running board -- closes the release phase. Its
    # only write is root's own journal entry (SYRD-117).
    "release-status",
    # Deploys a prepared tenant's board to its pinned release and closes the
    # release phase by re-proof; the catalogued `deploy-release` (SYRD-531).
    "deploy-release",
    "add-role",
    # Reports what a declared pool of interchangeable workers would change, and
    # what would stop it. Reads only (SYRD-37).
    "worker-pool",
    # Records which of the owner's existing keys a tenant publishes with, and
    # rewrites the managed ssh_config block. Both are root's writes (SYRD-100).
    "set-owner-identity",
    "present",
    "attach",
    "replace-window",
    "set-vcs-close-role",
    "set-role-runtime",
    "agy-credential",
    "seed-role-credentials",
    "role-prompt",
    "onboarding-readiness",
    "stop",
    # The other half of `stop`: bring a suspended tenant back in dependency
    # order, and stop at the boundary that fails rather than claiming a start
    # over a board that never came up (SYRD-193).
    "start",
    "teardown",
    "status",
    "validate-models",
)
# `present` and `board-skill` act on the caller's own runtime -- display slots
# and the caller's CLI skill trees -- so neither needs to escalate. `role-prompt`
# writes the tenant's own configuration through the board's workflow API as the
# invoking user, which is the point: the director sets a role's remit without root
# and without hand-editing a generated artifact.
SWITCHYARD_UNPRIVILEGED_COMMANDS = frozenset(
    # `finish-upgrade` is the director's own phase and refuses to run as root by
    # design; classifying it privileged made the wrapper escalate it into the
    # refusal, leaving the director no way to run it at all (SYRD-49).
    #
    # `attach` must never escalate either, and for a sharper reason: the whole
    # point of it is to hand an operator a terminal on a worker without a
    # privileged parent shell behind it. A wrapper that ran it through sudo
    # would put exactly that shell there, and every key the operator pressed
    # would have it as an ancestor (SYRD-76).
    #
    # `worker-pool` for the same reason as `role-prompt` and `attach` together.
    # Everything it writes goes through the board's own workflow API as the
    # invoking role, and everything else it does is a tmux session on the
    # project account's own server. Escalating it would put a privileged parent
    # shell behind `worker-pool <project> attach <worker>`, which is precisely
    # the thing SYRD-76 exists to keep out from behind an operator's terminal
    # (SYRD-37).
    #
    # `privileged-action` is the sharpest case of all. It escalates itself,
    # through pkexec, for one catalogued action at a time -- and the root-owned
    # helper then proves the CALLER is the control role's registered pane by
    # walking its own ancestry up to a tmux parent. A wrapper that ran this
    # under sudo would put a privileged shell in the middle of that walk and
    # change the identity being proved, so the boundary would be asked about
    # the wrong process. It runs unprivileged, exactly as typed (SYRD-112).
    #
    # `status` reads and prints; it changes nothing. Escalating it asked an
    # operator to cross a privileged mutation boundary to look at their own
    # host, and after a legacy cutover -- when the tenant's configuration moved
    # into an account the desktop operator is not -- that ask became a password
    # prompt for a read. The root-owned registry, release and journal records
    # this reports are world-readable by design; anything it cannot read is
    # named as unavailable instead (SYRD-241).
    {
        "present", "attach", "board-skill", "role-prompt", "set-role-runtime",
        "finish-upgrade", "worker-pool", "privileged-action", "status",
    }
)
SWITCHYARD_PRIVILEGED_COMMANDS = frozenset(
    command for command in SWITCHYARD_COMMANDS if command not in SWITCHYARD_UNPRIVILEGED_COMMANDS
)


def switchyard_help_text() -> str:
    from scripts import team_launcher as launcher

    commands = ", ".join(launcher.SWITCHYARD_COMMANDS)
    return f"""Usage:
  switchyard
  switchyard <project name or slug>
  switchyard <command> [options]

Commands:
  board-skill      install or verify the portable board skill for every agent CLI
  new              create and provision a new project
  register         register an existing project config
  upgrade          update generated project artifacts and report release drift
  repair-boundary  apply the reviewed repository boundary to a registered tenant
  approve-desktop  record, show or withdraw this host's standing desktop approval
  adopt-workflow   record an existing project's declared workflow as root's own copy
  finish-upgrade   run the director-owned phase of an upgrade from the director's session
  cutover-roles    legacy compatibility command (new runtimes use the project account)
  add-role         add an implementer or auditor role, worktree, pane, and board registration
  worker-pool      plan, apply and run a project's declared pool of interchangeable workers
  present          map persistent role sessions into stable display slots at runtime
  attach           attach this terminal to a role's live worker by project and role name
  replace-window   replace a root-owned presentation window without stopping any worker
  recover-display  reattach a project's disconnected Director display, as its desktop operator
  set-vcs-close-role
                   set which existing project role can mark tickets done
  set-role-runtime change an existing role's agent runtime and reconnect its panes
  agy-credential   show, set, or clear this host's agy credential source
  role-prompt      show, set, or clear a role's onboarding prompt
  onboarding-readiness
                   report whether every registered tenant has migrated director onboarding
  stop             suspend a project: window, sessions, listener and board, reversibly
  start            resume a suspended project in dependency order
  teardown         remove project board provisioning artifacts after a dry-run review
  release-status   compare the shared release, deployed board, live build and both journals
  status           list registered projects and pane liveness
  validate-models  check configured role models without starting panes

Bare project names start or attach the project. Recognized commands: {commands}.
"""


def switchyard_invocation_requires_root(argv: Sequence[str]) -> bool:
    from scripts import team_launcher as launcher

    if not argv:
        return False
    command = argv[0].casefold()
    if command in {"-h", "--help", "help", "--version", "version"}:
        return False
    if len(argv) >= 2 and argv[1] in {"-h", "--help"}:
        return False
    if command not in launcher.SWITCHYARD_PRIVILEGED_COMMANDS:
        return False
    # A lifecycle verb this caller can reach over their tenant's control bridge
    # needs no root at all. Saying otherwise here escalates at the trampoline,
    # before any of the routing below runs -- which would put a sudo prompt in
    # front of exactly the human the bridge exists to spare (SYRD-50 rollout
    # review). A bare project name is already unprivileged, which is why only
    # `stop` and `status` needed this.
    return not launcher._tenant_control_can_serve(argv)
