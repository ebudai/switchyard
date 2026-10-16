# SYRD-593: the gate's printed repair runs while the project is stopped

## The deadlock

After the 2026-10-15 reboot, Otto would not start. Its designer was configured
for `gemini-3.7-flash-high`, which the owner's `agy` no longer listed, so the
launch gate (`stop_before_launch_for_unknown_models`) refused every role and
printed:

```text
switchyard: not starting designer: agy on otto-agent does not offer 'gemini-3.7-flash-high' ...
switchyard:   repair it with: switchyard set-role-runtime otto designer --cli agy --model gemini-4-argon-medium
```

`set-role-runtime` refused anyone but `TICKET_BOARD_CALLER_ROLE=director`
(`role_runtime._require_director`), and the Director was one of the roles the
gate had just declined to start. The operator's plain command crossed to root
through sudo and was refused there; pkexec was refused the same way.

## The repair

`scripts/stopped_project_model_repair.py` adds one narrow authority to
`switch_role_runtime`, and changes nothing for the Director:

| | |
|---|---|
| who | root, by effective uid: the operator's sudo or pkexec. The command already crosses there for anyone not the owner. `TICKET_BOARD_CALLER_ROLE` is not consulted; root that sets it to `director` takes the Director's path, as before. |
| when | neither the Director nor the role has a session on the project account's tmux server (asked as that account). Any tmux answer other than "can't find session" or "no server running" refuses. |
| what | the model only, to one on the owner's own catalog, or `''` for the runtime's default. The runtime is not changed, and neither are the effort, the stored arguments or `--force`. A catalog that cannot be read refuses. |
| board | not written. A model is not part of the declared workflow, and the write would need the Director. |
| files | the configuration and its tenant copies, through the same projection, chowned back to the owner. The switch journal, and any directory created to hold it, are the owner's too. |

Every refusal names what was refused and says that nothing was changed. The
owner's own account, without the Director, is still refused. The refusal now
also says that root may make the model repair.

## Not taken

- **Starting the Director before the gate.** That starts a role on a launch
  the gate has refused, and a Director whose own model is the unavailable one
  would run on whatever its provider substitutes.
- **Settling the model at a terminal during an ordinary launch.** It does not
  help a launch with no terminal (the desktop launcher), and the printed
  command would still be refused.
- **A new tenant-control verb.** The bridge takes no arguments from its caller
  by design. Root through sudo is the route the printed command already takes.

## Evidence

`tests/stopped_project_model_repair_test.py` runs as real identities in a user
namespace: a desktop operator, the tenant's owner and root. It follows the
gate's printed line from a refused launch, through the operator's crossing to
sudo and root's repair, to a launch that passes the gate. The same repair
against main (`c9fd9c63`) is refused as root, which is the outage.
