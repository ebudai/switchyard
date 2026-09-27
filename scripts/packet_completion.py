"""Whether the privileged packet has finished, read from what it installs.

- `PacketCompletion` is the answer: what the packet has left undone, if
  anything.
- `_owner_user_unit_args` drives the owner's user manager the way the packet
  itself does; `_owner_unit_is_active` and `_board_answers` are the listener and
  HTTP probes.
- `privileged_packet_completion` checks the five installed artifacts, the board
  and listener services and the board's answer, in order, naming the packet
  step behind each problem (SYRD-155).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-363). The launcher
imports this module at its top and re-exports every name, so resume-provision
and its readiness check reach the same objects, the class included. Every
launcher facility these use -- the uid lookup, the system-unit probe the
launcher shares with postgres remedies, the board opener -- and every name
defined here that another definition here reads is read from `team_launcher`
when it runs, as it was, including inside the lazy defaults. The
standard-library names are this module's own imports, the same objects. This
module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision


@dataclass(frozen=True)
class PacketCompletion:
    """What the privileged packet has left undone, if anything."""

    problems: tuple[str, ...] = ()

    @property
    def done(self) -> bool:
        return not self.problems


def _owner_user_unit_args(plan: "ProjectBoardProvision", *args: str) -> list[str]:
    """Drive the owner's user manager the way the packet itself drives it."""
    from scripts import team_launcher as launcher

    uid = launcher.uid_for_user(plan.owner_user)
    runtime_dir = f"/run/user/{uid}"
    return [
        "sudo", "-u", plan.owner_user, "env",
        f"XDG_RUNTIME_DIR={runtime_dir}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime_dir}/bus",
        *args,
    ]


def _owner_unit_is_active(
    plan: "ProjectBoardProvision", unit: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]
) -> bool:
    from scripts import team_launcher as launcher

    result = runner(
        launcher._owner_user_unit_args(plan, "systemctl", "--user", "is-active", "--quiet", unit),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return getattr(result, "returncode", 1) == 0


def _board_answers(plan: "ProjectBoardProvision", *, opener: Callable[[str], Any]) -> bool:
    try:
        with opener(f"http://127.0.0.1:{plan.port}/api/board"):
            return True
    except Exception:  # noqa: BLE001 - any failure to read is "not answering"
        return False


def privileged_packet_completion(
    plan: "ProjectBoardProvision",
    *,
    exists: Callable[[Path], bool] = lambda path: path.exists(),
    system_unit_active: Callable[[str], bool] | None = None,
    owner_unit_active: Callable[[str], bool] | None = None,
    board_answers: Callable[[], bool] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    opener: Callable[[str], Any] | None = None,
) -> PacketCompletion:
    """Whether the privileged packet has finished, read from what it installs.

    Not from a marker the packet could have written: a marker says a script
    reached its last line, and what the continuation needs to know is whether
    the things the rest of the recovery depends on are actually there. Each
    problem names the step of the packet that would have produced it, because
    the answer to an incomplete packet is to run it again and read what it
    says.
    """
    from scripts import team_launcher as launcher

    system_active = system_unit_active or (lambda unit: launcher._system_unit_is_active(unit, runner=runner))
    owner_active = owner_unit_active or (lambda unit: launcher._owner_unit_is_active(plan, unit, runner=runner))
    answers = board_answers or (lambda: launcher._board_answers(plan, opener=opener or launcher._open_board_url))

    problems: list[str] = []
    for path, description in (
        (Path("/etc/systemd/system") / plan.board_unit, f"the board unit {plan.board_unit} is not installed"),
        (Path("/etc/tmpfiles.d") / plan.tmpfiles_name, f"the tmpfiles configuration {plan.tmpfiles_name} is not installed"),
        (Path("/etc/polkit-1/rules.d") / plan.polkit_name, f"the polkit rule {plan.polkit_name} is not installed"),
        (
            Path(plan.owner_home) / ".config" / "systemd" / "user" / plan.listener_unit,
            f"the listener unit {plan.listener_unit} is not installed for {plan.owner_user}",
        ),
        (
            Path(plan.board_current) / "scripts" / "ticket-board.py",
            f"no board release is exported at {plan.board_current}",
        ),
    ):
        if not exists(path):
            problems.append(f"{description} ({path})")
    if not system_active(plan.board_unit):
        problems.append(f"the board service {plan.board_unit} is not running")
    if not owner_active(plan.listener_unit):
        problems.append(
            f"the notify listener {plan.listener_unit} is not running for {plan.owner_user}"
        )
    if not answers():
        problems.append(f"the board does not answer on port {plan.port}")
    return launcher.PacketCompletion(tuple(problems))
