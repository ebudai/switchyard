"""The last line of an upgrade dry run: ready to apply, or not, and that nothing changed (SYRD-685).

A dry run of `switchyard upgrade` prints the source it chose, every change it would make, the deploy commands and
each phase's status, and then stopped, with nothing that said whether the applying command was safe to run. A
refusal said STOP; success said nothing. During PIE's recovery the operator could not tell the two apart.

`UpgradeDryRunVerdict` is entered once by `upgrade_project_command`, the one upgrade command boundary. Every way out
of a dry run passes through it, so exactly one verdict line is printed, last:

- **READY TO APPLY**, only when the last phase answered 0. It names the project and the release commit the
  release phase found: the one it would move to, or the one already deployed that stays.
- **NOT READY** for anything else: a phase that stopped the upgrade, whatever it answered, a nonzero finish, or a
  refusal raised from inside a phase. The blocking reasons are the ones already printed above it; a raised refusal's
  own words are printed where Python would have printed them, before the verdict, and it exits 1 as Python would.

Each line says nothing changed, and ends with a bracketed `key=value` tail for a script to read. Nothing is printed
when the upgrade applies, and the detailed output and every exit status are unchanged.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

PREFIX = "switchyard: dry-run verdict:"


class UpgradeDryRunVerdict:
    """One upgrade's dry-run verdict: told what the release phase found, and said once, on the way out."""

    def __init__(self, project: str, *, dry_run: bool, print_func: Callable[[str], None] = print) -> None:
        self.project, self.dry_run, self.print_func = project, dry_run, print_func
        self.deploy_ref = ""
        self.status: Any = None
        self.withheld = False
        self.step = ""
        self.done = False

    def __repr__(self) -> str:
        return f"UpgradeDryRunVerdict({self.project!r}, dry_run={self.dry_run!r})"

    # -- what the phases settle ------------------------------------------------------------------------------------

    def only(self, step: str) -> None:
        """The one narrow step run instead of the upgrade (`--only`), named in place of a release."""
        self.step = step

    def pinned(self, deploy_ref: str | None) -> None:
        """The ref U1 pinned, named when the release phase never resolves one."""
        self.deploy_ref = str(deploy_ref or "")

    def release(self, *, status: Any, withheld: bool) -> None:
        """What U6's release phase found: the tenant's release status, or that the deploy was withheld."""
        self.status, self.withheld = status, withheld

    # -- saying it ---------------------------------------------------------------------------------------------------

    def stopped(self, answer: Any) -> Any:
        """A phase stopped the upgrade before its end. Whatever it answered, this is not ready."""
        self._say(False, answer)
        return answer

    def finished(self, answer: Any) -> Any:
        """The upgrade's own answer, after every phase."""
        self._say(answer == 0 and not isinstance(answer, bool), answer)
        return answer

    def __enter__(self) -> "UpgradeDryRunVerdict":
        return self

    def __exit__(self, kind: Any, error: BaseException | None, _trace: Any) -> bool:
        if error is not None and not self.done and self.dry_run:
            code = error.code if isinstance(error, SystemExit) else None
            if isinstance(code, str):
                words = " ".join(code.split()).removeprefix("switchyard: ").removesuffix(" Nothing was changed.")
                words = words.rstrip(".")
                # Python would print a worded refusal after this line, as it exits. So it is printed here, where it
                # would have gone, and before the verdict; the exit status is the one Python gives it (Audit of
                # 4f3817a9).
                sys.stdout.flush()
                print(code, file=sys.stderr, flush=True)
                self._say(False, 1, why=f"it stopped: {words[:400]}")
                raise SystemExit(1) from None  # printing nothing more; Python flushes the verdict as it exits
            elif isinstance(error, SystemExit):
                self._say(False, code if code is not None else 0, why="it stopped; the reasons are above")
            else:
                self._say(False, None, why=f"it failed: {type(error).__name__}: {' '.join(str(error).split())[:400]}",
                          raised=True)
        return False

    def _say(self, ready: bool, answer: Any, *, why: str = "", raised: bool = False) -> None:
        if not self.dry_run or self.done:
            return
        self.done = True
        exit_status = answer if isinstance(answer, int) and not isinstance(answer, bool) else None
        tail = f"exit={'raised' if raised else exit_status if exit_status is not None else 'none'}"
        if not ready:
            reason = why or "the blocking reasons are printed above"
            self.print_func(f"{PREFIX} NOT READY: {self.project}: {reason}. Nothing changed. "
                            f"[verdict=not-ready project={self.project} {tail}]")
            return
        commit, where = self._release()
        self.print_func(f"{PREFIX} READY TO APPLY: {self.project}, {where}. Nothing changed; run the same command "
                        f"without --dry-run to apply it. [verdict=ready project={self.project} commit={commit or '-'} "
                        f"{tail}]")

    def _release(self) -> tuple[str, str]:
        """(the exact commit, how the release stands) from what the release phase found."""
        if self.step:
            return "", f"its {self.step} step only, and no release"
        status = self.status
        target = str(getattr(status, "target_sha", "") or "")
        current = str(getattr(status, "current_sha", "") or "")
        if self.withheld:
            return "", ("its release deploy withheld until its roles are on their own accounts, as said above"
                        + (f"; pinned to {self.deploy_ref}" if self.deploy_ref else ""))
        if target and getattr(status, "unchanged", False):
            return target, f"release {target}, already deployed: the board release does not move"
        if target:
            return target, f"release {target}, moving the board from {current or 'no deployed release'}"
        return "", (f"pinned to {self.deploy_ref}, which the release phase did not resolve to a commit"
                    if self.deploy_ref else "with no release commit the release phase could name")
