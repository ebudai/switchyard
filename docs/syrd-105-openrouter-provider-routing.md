# SYRD-105: OpenRouter provider routing, before we rely on it

Design spike. The question is whether Switchyard should let role agents run on
OpenRouter-routed models, and under what contract. Source of the risk list: Mo
Moustafa, ["So you want to use OpenRouter?"][article] (2026-09-07), as summarised
on the ticket. I did not re-measure the article's claims; what follows is about
which of them can reach this system and what we could do about them here.

[article]: https://mmoustafa.com/blog/so-you-want-to-use-openrouter/

## Where the responsibility sits

Set by the User after reading the first draft of this note, and it decides most
of what follows: **choosing and pinning a provider is the runtime's business,
not Switchyard's.** It belongs in the selected runtime's own provider
configuration. Switchyard neither passes a provider nor enforces one, and does
not refuse to start a role because a pin is missing.

The most Switchyard may do is **record a declarative model-to-provider mapping
for visibility** — a statement of which provider a role is expected to be served
by, readable beside the model it already declares, carrying no enforcement. A
reader can then see what was intended and compare it with what the runtime
actually did.

## Recommendation

**Do not put Switchyard roles on OpenRouter-routed models yet**, and do not build
provider machinery here when adoption does come.

The deciding factor is not model quality, and it is the same fact that makes the
boundary above the right one: Switchyard cannot see a completion. It launches
vendor CLIs and watches terminals, so none of the rules the risk list asks for
can be enforced at this layer whoever wants them. They belong where the
completions are.

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

That last point matters more than it looks, and it survives the boundary rather
than arguing with it. Each role gets its own hermes home (`hermes_home_for_role`),
so a pin or a fallback chain is per-role runtime state. Configuring it is the
runtime's business; what follows from the per-role split is that two roles
nominally on the same model can be configured differently, and nobody reading
this system can currently tell. That is the gap a declarative mapping is for.

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
| Pinned providers still regress or withdraw | Yes | A provider that disappears takes the role offline. Noticing is the runtime's job; from here it looks like a worker that stopped |

The board's authority model is unaffected: role identity is a registered
process, not a model (SYRD-69), so routing cannot grant or move authority.
Nothing in the risk list touches publication or integration authority.

## What the runtime has to be doing before a role runs on it

These are conditions on the runtime and its provider configuration, not
requirements Switchyard imposes or checks. Whoever configures the runtime owns
them; they are written down here so the decision is made with open eyes rather
than rediscovered from a stalled pane.

1. **The provider is pinned where pins belong** — in the runtime's own provider
   configuration. `hermes` has both the pin (`--provider`, `hermes model`) and an
   ordered fallback chain (`hermes fallback`), so the mechanism exists; using it
   is the operator's call. Note that hermes homes are per role here, so a pin set
   in one role's home says nothing about another's.
2. **The resolved provider is knowable.** If the runtime cannot say which
   provider served a turn, a pin is an assertion nobody can check, and the
   mapping below records an intention nobody can compare against reality.
3. **The model can actually call a tool.** Every Switchyard role is a
   tool-calling agent. This one is Switchyard's business, because Switchyard
   already probes models at first run and the probe is too weak: today's check
   (`validate_role_models`) asks the model to echo `model-ok` and requires it in
   stdout. It already treats exit 0 without the sentinel as a failure, which is
   the right instinct, but "it can talk" is not "it can call a tool". That gap
   is SYRD-111 and is independent of OpenRouter.
4. **Resume survives a replayed history**, because Switchyard resumes sessions
   by id as a matter of course. A provider that rejects replayed reasoning traces
   breaks resumption, not just one turn.
5. **Effort is honoured or known to be ignored.** `--reasoning <effort>` is
   passed for hermes; if a provider accepts it and does nothing, the role's depth
   setting is decorative and nobody is told.
6. **Rate limits are understood as shared.** One key serves every hermes role, so
   concurrency is the realistic case and a single call proves little.

Only item 3 is Switchyard's to fix, and it is filed. The rest are the runtime's,
and none of them are met by default.

## What Switchyard should probe, and what it should not

Switchyard already gates a launch on whether each role's model answers at all,
and that gate should ask the question that matters for an agent: can this model
**call a tool**, proved on the deployment host, as the role account, through the
same CLI a real pane uses. A tool call that arrives as prose, or not at all, is a
failure, and an exit 0 carrying no usable content stays a failure. That is a
capability question about the configured runtime, it is answerable from where
Switchyard already stands, and it is the whole of SYRD-111.

What Switchyard should **not** grow is a provider health gate: probing providers,
scoring them, pinning around them or failing over between them. That is the
routing responsibility the boundary above places outside this system, and
building a blind version of it here — blind because no completion is visible —
would be worse than not having one.

If a mapping is recorded for visibility, the honest thing to compare it against
is whatever the runtime reports, not something Switchyard infers from a pane.

## Follow-ups

One open, one cancelled by the boundary above:

- **SYRD-111**, open: extend the first-run model probe to require a structured
  tool call, for every CLI. The sentinel check passes on a model that cannot call
  tools at all, which is a gap independent of OpenRouter and independent of who
  owns provider selection.
- **SYRD-110**, cancelled: it would have refused to launch a role whose
  gateway-routed model carried no provider pin. That is exactly the enforcement
  the boundary above places outside Switchyard, so it was cancelled rather than
  deferred. This note originally proposed it; the User's decision replaced it.

Not filed, and deliberately: provider routing, health gates, fallback chains and
per-completion telemetry. Those are the runtime's or they are nobody's, and
building them here would mean building them blind.

The one thing this note leaves open for Switchyard is the declarative
model-to-provider mapping. It is worth having when a role is actually routed
through a gateway, and worth nothing before that, so it should be filed when the
first such role is proposed rather than built against a hypothetical.
