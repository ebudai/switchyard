# A launch that asks about the CLIs its tenant uses

On the preserved Zorin `test` tenant every configured role runs Claude or
Codex. Every `switchyard test` asked:

    switchyard: hermes is installed at /home/eric/.local/bin/hermes, which only
    eric can reach...
    Promote hermes host-wide? [p/s] (p):

`p` attempted a promotion and failed on a mode-0775 source. `s` launched — and
the question came back on the next invocation, and the one after that.

## What was actually wrong

Not the boundary. A tenant's configuration lives under its owner's home, and
the operator launching it cannot read that. This is deliberate and it works.

SYRD-211 drew the wrong conclusion from it. Its own docstring said so plainly:

> What cannot be asked from out here is which CLIs this tenant actually
> selected ... So the question asked is the one that can be answered -- which
> agent CLIs exist for this operator alone.

A question you can answer is not a substitute for the question that matters.
`resolvable_agent_cli_promotions()` took no tenant at all: it enumerated every
caller-only agent CLI on the host and offered each one, for any tenant, at
every start.

The missing piece was never a way to read the tenant's configuration from
outside. It was a record of the tenant's CLI selection **on the outside**.

## The record

`/etc/switchyard/projects/<slug>.json` already exists for exactly this kind of
fact: root-owned, mode 0644, one file per project, written when root registers
the tenant and read by the unprivileged launcher before it crosses the bridge.
It now carries the CLIs the tenant's roles are configured with:

```json
{
  "schema": "switchyard.project-registry.v1",
  "slug": "test",
  "name": "test",
  "config_path": "/home/test-agent/Projects/test/.switchyard/provision/test.json",
  "agent_clis": ["claude", "codex"]
}
```

A CLI name is not a secret and there is nothing here to replay — the same
argument the tenant-control grant document makes for being world-readable.

Registration is where it is written, because that is the last moment the
configuration and a root-owned file are both in reach: `_register_switchyard_project`
was already calling `load_project_config` to validate, and was throwing the
result away.

The launch then offers a promotion only for the CLIs the record names.

## Three states, not two

`registered_tenant_agent_clis()` returns a set, or `None`. The distinction is
the whole design:

* **a set** — this tenant's selection, as root recorded it. Offer only these.
* **empty set** — a tenant that configures no agent CLI. Offer nothing.
* **`None`** — nothing is recorded. Not "uses none of them", and not "uses all
  of them" either, which is precisely the reading that produced the defect.

Every tenant registered before this change is in the third state, and there is
no honest way to guess it from outside. So an unrecorded tenant is offered
nothing and prompted for nothing; the launch is left exactly as it was, and one
line says what would record it. A line, not a question: starting a tenant is
not the moment to make somebody decide about a CLI nobody can show it needs.

Nothing is lost by the silence. A role that genuinely needs a CLI is still
named by the launch itself, past the bridge, where the configuration can be
read.

## Making that line true

Nothing in the codebase updated a registry record. `_register_switchyard_project`
refuses outright when the file exists, `switchyard upgrade` never touched the
registry, and teardown only deletes. So a tenant registered yesterday would
have stayed in the `None` state for ever, and the line telling the operator how
to fix it would have been a lie.

`switchyard upgrade` now refreshes the one key, next to the report-credential
refresh SYRD-238 added for the same reason — a record that was previously
write-once needing to be brought up to date on an existing tenant. It is
narrow by construction: only `agent_clis`, only when it differs, nothing at all
when the record is missing or is not a registry document (repairing a stale
record is not that command's decision), and `--dry-run` says what it would do
and writes nothing.

## What did not change

* The offer still happens before the bridge, for the reason SYRD-211 found:
  past it the launcher runs as the owner and the operator's private copies are
  invisible.
* Declining still launches. Nothing is being created here.
* A CLI that cannot be promoted at all is still diagnosed rather than offered
  (SYRD-210), including for an unrecorded tenant — saying why something cannot
  work is not acting on it.
* `switchyard new` is untouched: it has the selection in hand already, and
  `require_agent_clis_for_new_tenant` takes it as an argument.

## Verification

`tests/tenant_launch_unused_cli_test.py`, 31 checks, covering the four cases
the ticket names — an unused caller-only Hermes, a tenant that genuinely uses
Hermes, a mixed tenant offered only its own, and a repeated launch — plus the
unrecorded tenant, the unattended path, and the write side: registration
records it, an upgrade records it for a tenant that predates it, a second
upgrade rewrites nothing, a foreign record is left alone, and a dry run writes
nothing.

Mutation, 5 mutants, all killed:

| mutant | killed by |
| --- | --- |
| drop the selection filter from the offer | a mixed tenant asked about a CLI it does not use |
| an absent record reads as "uses none" rather than "not recorded" | the record surviving a dry run unchanged |
| refresh rewrites even when the record already agrees | a second upgrade that should have written nothing |
| refresh ignores `--dry-run` | the record changing under a dry run |
| record one entry per role instead of per CLI | `["claude", "claude", "codex"]` for two Claude roles |

A sixth attempt — disabling the dry-run branch — did not match its anchor and
never applied; it is reported here as re-run and killed, not as survived.

`tests/tenant_resume_cli_promotion_test.py` (SYRD-211) keeps every case and
gains the record the launch now needs, pointed at through the environment
variable the launcher itself reads, so those calls exercise the real lookup.
Its docstring carried the superseded rule and now records what replaced it.

### Base

`73cf582`. Suites re-run against a clean worktree at that commit, per case:
`agent_cli_host_wide_test`, `team_launcher_registry_test`,
`resume_provision_continuation_test`, `team_launcher_adopt_registry_config_test`,
`upstream_report_credential_test`, `legacy_root_owned_provision_upgrade_test`
and `team_launcher_status_liveness_test` pass. Six suites fail identically at
that baseline and with this change, in the same cases:
`tenant_resume_cli_promotion_test::test_accepting_the_offer_uses_the_privileged_crossing_not_an_in_process_chown`,
`team_launcher_project_precheck_test`, `team_launcher_switchyard_commands_test`,
`tenant_control_bridge_test`, `team_launcher_onboarding_git_test` and
`team_launcher_design_test`.

### Not verified here

The live `test` tenant. Its registry record predates this change, so the first
launch after it lands should ask nothing and say how to record the selection;
`sudo switchyard upgrade test` should then record `["claude", "codex"]`, after
which a Hermes it does not use is still never offered. That sequence is the
retest's to confirm, and the tenant is preserved for it.
