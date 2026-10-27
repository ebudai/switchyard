# SYRD-692: Hermes roles on the local model service

SYRD-690 asks for Switchyard roles that run the local Qwen3.8 model through
Hermes. SYRD-691 provides the model service. This slice puts a Hermes role on
that service and keeps it there across the role's whole lifecycle. The role
still runs Hermes: Hermes owns provider selection, in its own `config.yaml`
(SYRD-105). Switchyard creates, keeps and shows one declared profile, and
refuses to start the role when the service does not match it.

The code is in `scripts/hermes_local_profile.py`. The tests are in
`tests/hermes_local_profile_test.py`.

## The declaration

A local role's launcher entry carries `hermes_local` next to `cli: ["hermes"]`
and its `model`:

```json
"hermes_local": {
  "name": "local-qwen",
  "model": "Qwen/Qwen3.8-27B",
  "base_url": "http://127.0.0.1:17431/v1",
  "server": "llama.cpp b11803",
  "revision": "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0",
  "artifact": "ggml-org/Qwen3.8-27B-GGUF@a316ea527fde32c12487be7574e2db5ac2ad2a7e",
  "quantization": "Q4_K_M",
  "context": 65536
}
```

**Where the values come from.** They are copied from the service's own
record, `/var/lib/switchyard/local-models/service.json` (schema
`switchyard.local-model.v1`). Nobody types them.

**How the record is read.** It is walked from `/` one component at a time
without following a link. Every directory on the way, and the file itself, must
be owned by root and writable by nobody else.

**What is refused:**
- an endpoint that is not `http://127.0.0.1:<port>/v1` or
  `http://[::1]:<port>/v1`;
- a model id that is not one exact `org/name`, or ends in a floating name such
  as `latest`;
- a revision that is not a full 40-hex commit.

**When the config loads.** The loader refuses a declaration with unknown or
missing keys, one on a role that is not Hermes, or one whose role `model` is
not the profile's.

**Why the entry.** The entry is the tenant's durable record. Launch and
stop/start read it, upgrade loads it rather than regenerating it, and a runtime
switch rewrites only the fields that belong to the runtime. So the profile
survives all of them as written. Leaving Hermes removes it
(`role_runtime.project_role_runtime`).

## Opting in and out

| Flow | How |
|---|---|
| A new project | `project.local_qwen_roles: ["main"]` in the design artifact used with `switchyard new --from`. Each named role must be a Hermes role. |
| A new role | `switchyard add-role <project> <role> --local-qwen` (with `--cli hermes`, or no `--cli`). |
| An existing role | `switchyard set-role-runtime <project> <role> --cli hermes --local-qwen`. Run it again to re-pin after the service changes. `--dry-run` shows the profile and changes nothing. |
| Leaving it | `switchyard set-role-runtime <project> <role> --cli hermes --no-local-qwen --model <hosted model>`. The hosted model must be named, so the role never runs the Qwen id on a hosted provider by accident. |
| Off Hermes | `switchyard set-role-runtime <project> <role> --cli codex` (or any other runtime). |

**What each flow prints.** It shows the profile it writes, for example
`local-qwen: Qwen/Qwen3.8-27B at http://127.0.0.1:17431/v1 (llama.cpp b11803;
revision 1d4bf0f2ff60; ggml-org/Qwen3.8-27B-GGUF@a316ea527fde Q4_K_M; context
65,536)`. It also says if the service's context is below what Hermes accepts
(below).

**How `set-role-runtime` writes the profile.** The profile and its model go
into the entry before the switch, which carries the entry's other fields
through. If the switch does not happen, the previous value is put back.

**Models.** A local role's model is checked against nothing in the owner's
hosted catalog. The service serves it, and the start check proves that. Changing
it any other way than re-pinning or leaving the profile is refused.

**Removing material.** Leaving the profile removes only Switchyard's own
material:
- the profile in the entry;
- the marked `config.yaml` in the role's home.

On a role that stays on Hermes, the next start replaces that config with the
shared link, or with the effort-only config (SYRD-546). Off Hermes, it is
removed when the switch completes. A `config.yaml` that Switchyard did not write
is never touched. Sessions, memories and the rest of the home stay.

## What the role's home holds

At every start, `prepare_hermes_home_for_role` writes the role's `config.yaml`
privately (mode 0600). The file is marked as Switchyard's, and its second line
names the profile it was written for.

It contains the owner's shared `config.yaml` with these changes:
- **Credentials:** every value whose key names a credential (`api_key`,
  `token`, `secret`, `password`, …) is removed, at any depth.
- **Other providers:** `providers`, `custom_providers` and `fallback_model` are
  removed.
- **The main model:**
  - `provider: custom`, `default: <model>`, `base_url: <endpoint>`;
  - `api_key: no-key-required`, so Hermes never reaches for `OPENAI_API_KEY`;
  - `api_mode: chat_completions` and `context_length: <context>`.
- **Fallback:** `fallback_providers: []`.
- **Delegation:** the provider, model, endpoint and key are emptied, so
  subagents inherit the main, local model.
- **Every auxiliary task**, each of Hermes 0.15.2's (vision, web extract,
  compression, skills hub, approval, MCP, title generation, triage, kanban,
  profile describer, curator, goal judge): `provider: custom` on the same
  endpoint and model, with `fallback_chain: []`. The task's other settings, such
  as timeouts, are kept.
- **Effort:** `agent.reasoning_effort`, when the role has one.

**Why every auxiliary task is explicit.** In Hermes 0.15.2 an auxiliary task
on `provider: auto` (the default) tries the main model, then on failure walks
OpenRouter, Nous and the API-key providers (`agent/auxiliary_client.py`). An
explicit `custom` task falls back only to its own `fallback_chain`, which is
empty, and then to the main model.

**Credentials in the home.** The shared `.env`, `auth.json` and `auth.lock`
carry hosted keys and OAuth state, so they are not linked into a local role's
home. Links left from a cloud configuration are removed; only the link goes,
never what it named. The other shared entries (`SOUL.md`, hooks, skills, `bin`)
are linked as before.

**Writes never follow a link.** A local role's home is made and written only
by descriptor. The walk starts at the account's own home as the password
database names it, never at a prefix of the configured session directory (a
configured link there would carry the walk anywhere; Audit). The home may also
be named by its resolved path, which is the same directory, and it must belong
to this account. Every directory below it is
opened with `O_NOFOLLOW`, made with `mkdirat` when missing, and must belong to
this account and be writable by nobody else. That includes any configured
prefix before `.local/state`, and `.local/state/<project>-ticket-board/hermes-homes`
itself (Audit). The config,
the removal of credential links and the shared-entry links all go through the
home's descriptor:
- a link anywhere on the way, the home included, is refused before anything is
  made, changed or removed through it;
- a home outside the account's own home is refused;
- a `config.yaml` link is replaced, never written through;
- a `config.yaml` of the role's own is left alone, the refusal says so, and the
  start check then refuses to start.

**Isolation.** Two roles on the one service share only the service. Each has
its own HERMES_HOME, config file, sessions and state.

## The start check

A local role's pane runs `python3 -I scripts/hermes_local_profile.py check
--role <role> --profile <json> -- hermes …` in place of `hermes …`. It runs as
the pane runs, with the role's own HERMES_HOME and environment. Every
hosted-provider key and endpoint override Hermes reads has already been unset
(`env -u`).

So every start path is checked the same way: launch, `switchyard start`,
`worker-pool start`, recovery, a fresh session per ticket, and same-parent
continuation. Hermes starts (`exec`, the same process) only when all of these
hold:
- The endpoint's `/v1/models` lists exactly the declared model, with the
  declared context (`n_ctx`).
- The context is at least Hermes's own minimum, 64,000 tokens
  (`agent/agent_init.py`, `MINIMUM_CONTEXT_LENGTH`). Hermes refuses a smaller
  one even when `model.context_length` states it.
- The service's record still pins the declared model, revisions, artifact,
  quantization, server and endpoint.
- The home's `config.yaml` is this profile's.
- No hosted key is in the environment, in the home's `.env`, or in an
  `auth.json`. Any `*_API_KEY` counts.

Anything else is refused in the pane, in plain words, with the remedy, and
"nothing falls back to a hosted model". The startup capture keeps those lines.

## Status and validation

- `switchyard status` has a **LOCAL MODEL ROLES** section. It lists each local
  role with its whole profile and what the endpoint answers now: serving it and
  with what context, not answering, or too small a context for Hermes.
- `switchyard validate-models` checks each local role's profile against the
  service, in place of the owner-account probe.
- **First run.** The first-run phase asks the owner's Hermes account for no
  sign-in on behalf of a local role. Its provider is the endpoint.
- **Credential seeding.** Seeding for legacy per-role accounts seeds no hosted
  key into a local role.

## Not covered here

- **The live canary** is SYRD-693: tool use, edit and test, board hooks,
  stop/resume, and load. This slice proves the configuration with the installed
  Hermes's own resolution, offline, not a model turn.
- **The service's context.** The service as installed on 2026-10-27 holds a
  32,768-token context. Hermes refuses anything below 64,000, so a local role
  will not start until SYRD-691 raises it. Every flow and the start check say so,
  with the remedy.
