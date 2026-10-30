# SYRD-709: a registry entry is authoritative over a bundled launcher config

## What happened

SYRD-705's v6 run of the PGU cutover (2026-10-30) could not register PGU's new
tenant. Every release ships `config/team-launcher/pgu.json`, the legacy config,
with `run_as_user: agent` and `repository: /home/agent/Projects/pgu`. At
`218a906d` that file got in the way twice.

1. **Registration refused the slug.** `switchyard register <pgu-agent's
   pgu.json>` answered "project slug 'pgu' is already registered to 'pgu' at
   …/config/team-launcher/pgu.json". The collision check read the merged list
   of launcher configs and registry entries, so the bundled config counted as a
   registration.
2. **Resolution preferred the bundled config.** `_switchyard_entries` read the
   config directory first and kept the first entry for each slug. With a
   registry entry for `pgu` present, `_resolve_switchyard_project("pgu")` with
   the default config directory still returned the bundled
   `config/team-launcher/pgu.json`. Every command that resolves `pgu` would
   have acted on the legacy `/home/agent` tenant: deploy-release,
   release-status, privileged actions, the upgrade pin and launches.

Both were reproduced on `218a906d`. The new test fails there with the same
refusal. A registry holding a `pgu` entry for pgu-agent's config still
resolves to `…/config/team-launcher/pgu.json`.

## The rule

**A root registry entry (`/etc/switchyard/projects/<slug>.json`) is
authoritative for its slug and its name.**

- **Merging** (`project_resolution._switchyard_entries`):
  - every usable registry entry is kept;
  - a launcher config from the config directory, the release's bundled
    `config/team-launcher` by default, is dropped when a registry entry has its
    slug or its display name;
  - it is also dropped when the registry holds an entry file for its slug that
    cannot be used.
- **Kept as before:**
  - registry entries among themselves (first by file name for a slug);
  - bundled configs among themselves, so two bundled configs sharing a name are
    still ambiguous;
  - sorting by name, then slug.

  The precedence is the same whatever order the directories list in.
- **Registration** (`switchyard_registration._registered_project_collision`)
  checks registry entries only. A bundled config of the same slug or name is
  shadowed by the registration, not collided with. A real registry-to-registry
  collision still refuses: the same slug, or another slug under a registered
  name. So does an existing entry file.
- **Fail closed** (`project_resolution._registry_claims`). `switchyard
  register` writes `<slug>.json`, so an entry file claims its own name's slug
  even when it cannot be read, and the slug it names when that is valid. It is
  a claim when:
  - it is not a regular file: a symlink (broken or not), a directory, a FIFO.
    Every `.json` entry is examined with `lstat`, so no link is followed
    (Audit, round 1: a broken `pgu.json` symlink was skipped and the bundled
    config answered);
  - it cannot be examined;
  - it is not JSON or cannot be read;
  - it has another schema;
  - it names no valid slug;
  - it names no configuration.

  The usable-entry scan (`_registry_project_entries`) applies the same
  no-follow test (`_regular_file`), so a link under any name is never read
  through. A link `alias.json` pointing at a record for `pgu` registers
  nothing: it claims `alias`, and `pgu` is answered by whatever regular entry or
  bundled config there is, never by the link's target (Audit, round 2). A slug
  the registry claims is offered by no listing, even when another of its
  entries is usable: the registry contradicts itself, so nothing is offered. Resolving a claimed slug refuses: "project 'pgu' is registered, but its
  registry entry … cannot be read (…); refusing to use any other configuration
  for it. Repair or remove that entry as root." No listing offers the bundled
  config in its place.
- **An unreadable registry.** If the registry directory exists but cannot be
  listed, nothing can be known not to be registered. No bundled config is
  offered, and a resolution says the registry cannot be read. A registry that
  does not exist claims nothing.
- **provision-runtime** never falls back to the bundled
  `config/team-launcher/<slug>.json` past either refusal above. Every other
  refusal (an unknown project, an ambiguous selector, a project mid-provision)
  falls back as it always did.

**Unchanged:**
- With no registry entry, a bundled-only project such as the legacy `pgu`
  resolves exactly as before.
- Releases still ship `config/team-launcher/pgu.json`.
- Every consumer that resolves through `_resolve_switchyard_project`,
  `_resolve_launcher_project_config` or `_switchyard_entries` inherits the rule
  with no change of its own. That covers deploy-release, release-status,
  privileged actions (they run those commands as root), launches, upgrade,
  status, the menu, worker-pool and validate-models.

**Not changed:** `scripts/ticket-board-verify-resume`, a reboot diagnostic, still
defaults `--config` to the checkout's `config/team-launcher/<project>.json`.
Pass `--config` when checking a registered tenant.

## Tests

`tests/registry_authoritative_resolution_test.py` runs the real code against the
release's own bundled `config/team-launcher/pgu.json`. It uses a registry in a
test-owned directory named the way root names it (`SWITCHYARD_REGISTRY_DIR`),
and a tenant config shaped like pgu-agent's.

- **Before registration,** `pgu` is the bundled config.
- **pgu-agent's config registers** despite it.
- **`pgu` then resolves** to the registration, by slug, in any case, and in
  listings. So does the launcher's own resolution.
- **deploy-release, entered as root,** and release-status both load the
  registered config. They are stopped at that first read.
- **Registry-to-registry collisions** by slug and by name still refuse.
- **Names:**
  - a bundled config of another slug under the registered name is shadowed;
  - two bundled configs sharing a name stay ambiguous;
  - registering under a bundled config's name succeeds and then wins.
- **Fail closed.** These are each refused, naming the entry:
  - an entry that is not JSON, has another schema, has no configuration or has
    no valid slug;
  - an entry that is a broken symlink, a symlink to a valid entry, a directory
    or a FIFO;
  - an entry this account cannot read.

  A link under another name to a valid record for `pgu` is not read through:
  `pgu` is never the link's target, the link's own name is refused, and a real
  registration of `pgu` is not blocked by it. No `pgu` is
  listed, the launcher's resolution says so, and `provision-runtime` never falls
  back. An unreadable registry refuses.
- **An unknown project's `provision-runtime`** still runs with no config.
- **Removing the entry** exposes the bundled config again.

The two boundary guards were re-measured with their own harnesses, and each
carries a SYRD-709 note:

- **`project_resolution_boundary_test`.** Six cases read a registry. Their
  answers are the baseline's except `p454`, which is now the registration's.
  Their call records are the baseline's plus the registry-claim reads.
  `_registry_claims` is listed with its launcher reads.
- **`switchyard_registration_boundary_test`.** The stand-in registrations moved
  from `_switchyard_entries` to `_registry_project_entries`. Every result,
  print and file is the baseline's. Each call record differs in that one call.
