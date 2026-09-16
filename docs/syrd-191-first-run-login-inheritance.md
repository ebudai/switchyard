# A provider login the running panes cannot have

`switchyard <project>` runs the first-run setup phase and then launches. The
phase asks the owner to authenticate each CLI its roles use -- one login per
provider, covering every role configured for it -- and the launch then presents
the project.

A role that is already running is not started again; it is presented as it is.
That is right for an ordinary start and wrong immediately after a login: the
running process read its credentials when it started, and it started before
there were any. Nothing about a file written afterwards reaches it.

Live on the testing tenant during SYRD-146 UAT, both logins succeeded as the
tenant owner:

    /home/testing-agent/.claude/.credentials.json  0600  2026-09-16 16:23:27
    /home/testing-agent/.codex/auth.json           0600  2026-09-16 16:23:40

and the five panes the User was then shown had been running since the previous
day:

    pid 2715930  designer (claude)  started 2026-09-15 20:12:34

Each had run its CLI once, at that moment, with no credentials on the host, and
had been sitting on that provider's sign-in or onboarding screen ever since.
Two successful logins looked like five failed ones.

So the first-run report now carries which providers **this run** authenticated,
and the roles configured for each of them. A role in that set is not "already
running" for the purposes of this launch: its session is ended and started
again, which is the ordinary path a role that was not running takes, and the new
process reads the credentials that now exist. The roles come from the declared
role/provider data the login step is built from, never from a list of names.

The signal is deliberately the login this run performed, not the credential
file's mtime. Codex rewrites `auth.json` whenever it refreshes a token, and a
rule of "the runtime started before the credentials changed" would restart every
live pane a few hours into a working day. A refresh produces no login step, so
it restarts nothing.

A session that cannot be ended is kept and reported, naming what it will keep
showing, rather than counted as restarted.

## What this does not cover

Per-role credential seeding is unchanged and was already correct: a tenant whose
roles have their own Unix accounts copies each provider's state into each role
account through `switchyard seed-role-credentials`, driven by the same declared
table (`ROLE_CREDENTIAL_ARTIFACTS`: `.claude/.credentials.json`,
`.codex/auth.json`, agy's token, hermes' env file). On a shared-account tenant
like testing, every role runs as the owner and there is nothing to copy.

Claude's *onboarding* state -- the theme prompt, and per-directory trust -- is a
separate prompt class that an account login does not establish. The live tenant
carries valid credentials with `projects: {}` and no recorded theme, and the
first-run manifest deliberately collects folder trust only for detached roles,
on the reasoning that a visible pane is somewhere the dialog can be answered.
Whether Switchyard should collect it for visible roles too -- asking the owner
once, before any pane exists, instead of leaving one dialog per pane -- is a
change to that decision and belongs to whoever owns it, not to this fix.

## The provider's own first run, before any pane

A provider login is not the whole of a provider's first run. Claude keeps two
separate things in the owner's home: the credentials, and whether the account
has been through its own setup -- the theme and welcome flow -- recorded in
`.claude.json` beside a `projects` map of the directories it trusts. The live
tenant held valid credentials with `projects: {}` and no recorded theme, so
every pane opened setup or a trust dialog instead of a prompt.

The first-run phase therefore collects, in the foreground, before any role is
launched or presented:

1. one **login** per provider, covering every role configured for it;
2. one **provider setup** step per provider whose account-wide first run is
   unfinished, covering every role that uses it -- asked once, not once per
   role;
3. one **folder trust** action per distinct worktree that is not yet trusted,
   for every configured role rather than only detached ones, naming all the
   roles it covers.

Each is driven by declared role/provider/worktree data. Already-complete state
is skipped, and skipped again on the next run. Switchyard asks the CLI to run
its own setup and then reads the account state back; it never writes that state
itself and never answers a security prompt on the owner's behalf. A step that
ran and did not complete is reported, naming the roles whose panes will open it,
rather than left to be discovered there. The manifest counts and names every one
of these interactive steps before the first one runs.

## What completing Claude's first run actually costs

Measured on this host rather than assumed, with a scratch HOME holding a valid
credential and a `.claude.json` in the live tenant's exact shape -- an
`oauthAccount`, an empty `projects` map, no onboarding marker:

- `claude auth status --json` reports `loggedIn: true`;
- an interactive `claude` opens its welcome flow: first the theme chooser, then
  an **OAuth sign-in**, which it asks for anyway;
- the same account with `hasCompletedOnboarding` recorded opens neither -- it
  goes straight to the per-directory trust prompt, and after that to a normal
  prompt.

So an account in that state cannot be brought to a ready prompt without one
sign-in, by any path Switchyard controls. The marker lives in the provider's own
file, and writing it would be manufacturing the state the vendor uses to decide
whether to ask -- which this code will not do. `~/.claude/settings.json` does
not substitute: a theme recorded there leaves the welcome flow in place.

What that means for the manifest is simply that it must not promise otherwise.
The provider setup step says, before it takes the terminal, that the flow will
ask for a sign-in even though credentials exist, that it is asked once for the
account rather than once per role, and that `/exit` hands the terminal back.
Each folder-trust step says the same about the prompt it will show. The
monochrome terminal the first run appears in is part of the same state: the
theme is what that flow sets, so it is unset until it has been completed once.

## Deploying this, and repairing the testing tenant

The rollout is the standard journaled path and stays Director/User-controlled:

1. `pkexec .../switchyard-record-rollout testing --label "SYRD-191 pre-state"
   --target-commit <sha> -- <read-only report>` -- the five role sessions, their
   pane pids and start times, and the owner's provider state.
2. Install the audited release with `install-switchyard --apply` pinned to that
   commit, through Polkit and the recorder, as SYRD-184 did.
3. Rerun `switchyard testing` **as the tenant owner**. The foreground phase
   offers no login step -- the credentials from 2026-09-16 16:23 are still
   valid -- and then runs Claude's own first run once for the account, which
   asks for a sign-in regardless, as measured above; then one trust prompt per
   distinct worktree. The manifest says all of this before any of it starts.
4. The same run restarts exactly the five stale role sessions, because their
   runtimes started before those credentials existed. Nothing else is touched:
   no deploy, no units, no board restart.
5. Prove it: each pane's process started after the run, and each reaches a ready
   prompt with no sign-in or onboarding on screen.

Step 3 is the only interactive part and it asks the owner once per provider and
once per worktree -- never once per pane, and never per role account.
