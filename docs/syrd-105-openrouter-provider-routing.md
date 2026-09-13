# SYRD-105: OpenRouter provider routing, before we rely on it

Design spike. The question is whether Switchyard should let role agents run on
OpenRouter-routed models, and under what contract. Source of the risk list: Mo
Moustafa, ["So you want to use OpenRouter?"][article] (2026-09-07), as summarised
on the ticket. I did not re-measure the article's claims; what follows is about
which of them can reach this system and what we could do about them here.

[article]: https://mmoustafa.com/blog/so-you-want-to-use-openrouter/

## Recommendation

**Do not put Switchyard roles on OpenRouter-routed models yet.** If a specific
need arises, the only permitted shape is a pinned provider with a probe that
runs on the deployment host, and the preconditions below are not met today.

The deciding factor is not model quality. It is that Switchyard cannot see a
completion, so it cannot enforce any of the rules the risk list asks for.

## Where OpenRouter can already reach this system

It is not purely future-facing. The path exists and is one configuration field
away from being live.

- `hermes` is a supported role runtime, alongside `claude`, `codex` and `agy`
  (`SUPPORTED_CONFIG_CLI_NAMES`, `SUPPORTED_NEW_PROJECT_CLIS` in
  `scripts/team_launcher.py`).
- `OPENROUTER_API_KEY` is in `HERMES_PROVIDER_ENV_KEYS`, the allowlist of
  provider keys filtered out of the owner's `.hermes/.env` and seeded into each
  hermes role's own hermes home (`ROLE_CREDENTIAL_ARTIFACTS`). The key is not
  granted per role: if the owner holds one, every hermes role has it.
- A role's model is a plain configuration string passed through as `-m`
  (`DEFAULT_MODEL_ARG_BY_CLI`). `openrouter/<model>` is an ordinary value; the
  suite already uses one (`tests/team_launcher_first_run_models_test.py`).
- Reasoning effort reaches hermes as `--reasoning <effort>`
  (`EFFORT_STYLE_BY_CLI`).

What is **not** true today:

- No live role uses hermes. On this host the roles run `codex`, `claude` and
  `agy`, and no role pins a model at all.
- Switchyard never speaks a model API. There is no HTTP client for any provider
  anywhere in the repository; roles are vendor CLIs spawned in tmux panes.
- Switchyard never passes `--provider` and never configures hermes' fallback
  chain, although the CLI has both (`hermes --provider`, `hermes fallback`,
  which is "tried in order when the primary model fails with rate-limit,
  overload, or connection errors").

That last point matters more than it looks. Each role gets its own hermes home
(`hermes_home_for_role`), so a provider pin or a fallback chain would be
per-role state that Switchyard does not render, version or check. Two roles
nominally on the same model could route differently, and nothing here would
notice.

## Why the standard mitigations do not fit at this layer

The article's mitigations are adapter-level: validate that a tool call is
structured, treat an HTTP 200 with no content as a failure, record the resolved
provider per completion, retry on specific causes. Switchyard is not the
adapter. It launches a CLI and watches a terminal. Between the model and
Switchyard sits the whole vendor CLI, which does its own parsing, retrying and
history management.

Three consequences follow, and they are the substance of this spike:

1. **We cannot enforce a per-completion rule.** Nothing in Switchyard can
   inspect a completion, so "treat empty content as failure" cannot be
   implemented here. It has to be true inside the CLI, or it is not true.
2. **Failures arrive as silence, not as errors.** An empty completion or a tool
   call that came back as prose does not produce a bad diff for another role to
   catch. It produces a pane that stops changing, which is what an idle agent
   looks like — so the idle/nudge path treats a provider fault as a worker that
   needs prodding. This is reasoned from how the pane-idle path reads a pane,
   not measured; measuring it is part of the probe below if we ever adopt.
3. **We have no per-completion telemetry and nowhere to put it.** The ticket
   asks for resolved provider, model checkpoint, usage, finish reason,
   tool-call presence, retry cause and latency. Switchyard records none of
   these and cannot derive them from a pane. Any such telemetry would have to be
   emitted by the CLI and collected through a surface that does not exist.

## The risks, scored for this system

| Risk from the ticket | Reachable here | What it would look like |
| --- | --- | --- |
| Same model, different providers | Yes, once a hermes role exists | Two roles drift in quality with identical config; nothing reports which provider served either |
| Vision advertised, silently degraded | Reachable, not exercised by the launcher | Switchyard passes no image to any CLI; whether an agent opens one itself is its own business, so there is nothing here to gate |
| `reasoning.effort` accepted and ignored | Yes | `--reasoning high` is passed and may mean nothing; a role's depth setting becomes decorative with no signal |
| Quantization/provider metadata as a quality proxy | Yes | Not actionable here: Switchyard has no per-completion metadata at all |
| Tool calls arriving as text | Yes | The most damaging: a role that "says" it edited a file and did not. Caught only by review, and only if review happens |
| HTTP 200 with no content | Yes | Looks like an idle worker; the nudge path fires instead of a failure |
| Provider-specific reasoning-history rules | Yes | Resume is a first-class Switchyard feature (`--resume <session>`); a provider that rejects replayed traces breaks resumption, not just one turn |
| Production rate-limit behaviour | Yes | Shared by all roles on one key; one busy role can throttle the others |
| Pinned providers still regress or withdraw | Yes | A pinned provider that disappears takes the role offline; there is no health gate to notice |

The board's authority model is unaffected: role identity is a registered
process, not a model (SYRD-69), so routing cannot grant or move authority.
Nothing in the risk list touches publication or integration authority.

## The contract we would require

Before any Switchyard role runs on an OpenRouter-routed model, all of these must
hold. They are written as things that can be checked, not preferences.

1. **The provider is pinned, and the pin is Switchyard's.** The role's provider
   comes from project configuration and is passed explicitly, the way the model
   already is. A provider chosen inside a role's own hermes home is not a pin.
2. **The resolved provider is observable.** The CLI must report which provider
   served a turn, and the probe must be able to read it. Without this, a pin is
   an assertion nobody verifies.
3. **A structured tool call is proved on the deployment host**, not a sentinel
   string. Today's check (`validate_role_models`) asks the model to echo
   `model-ok` and requires it in stdout; it already treats exit 0 without the
   sentinel as a failure, which is the right instinct, but "it can talk" is not
   "it can call a tool".
4. **Resume survives a replayed history**, because Switchyard resumes sessions
   by id as a matter of course.
5. **Effort is either honoured or declared unsupported.** A setting that is
   silently ignored should not be presented as configuration.
6. **Rate limits are understood as shared.** One key serves every hermes role;
   the probe must exercise concurrency, not a single call.
7. **There is a fallback policy Switchyard renders**, or fallback is off. An
   unmanaged chain in a role's own config is worse than none, because it changes
   behaviour invisibly.

Items 1, 2, 3, 6 and 7 are unmet today. That is the reason for the
recommendation, and each is a specific thing to fix rather than a misgiving.

## If we ever adopt: the probe

Not a benchmark. A gate that runs where the work runs — on the deployment host,
as the role account, through the same CLI and flags a real pane uses, because
the article's rate-limit and routing behaviour differ from a laptop.

It must, per role and per pinned provider: make a call that requires a
**structured tool call** and fail if the tool call arrives as prose; fail on an
empty completion rather than counting a 200; record and assert the **resolved
provider**; run the same prompt at two effort settings and record whether
anything changed; resume a session carrying a reasoning trace and fail if the
provider rejects the replay; and run several roles' probes at once, since they
share a key. Any failure blocks the launch, the way model validation already
does.

The natural home is the existing first-run preflight (`validate_role_models`,
`_run_owner_cli_probe`), which already runs as the owner, per role, before
panes start.

## Follow-ups

Filed only for the recommendation above, both small and both guardrails rather
than adoption work:

- **SYRD-110**: refuse to launch a role whose model routes through a
  provider-routing gateway without an explicit provider pin, instead of starting
  it and hoping.
- **SYRD-111**: extend the first-run model probe to require a structured tool
  call, for every CLI. The sentinel check passes on a model that cannot call
  tools at all, which is a gap independent of OpenRouter.

Not filed: provider routing, health gates, fallback chains, per-completion
telemetry. Those belong to an adoption decision that this note recommends
against for now.
