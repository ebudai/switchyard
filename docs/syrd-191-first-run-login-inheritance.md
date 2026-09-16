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

So each role's runtime is reconciled against a **provider state generation**.
The generation is a digest of decisions rather than of bytes: whether the
account holds that provider's credential at all, whether its own first run is
complete, and which directories it trusts. Each role records the generation its
runtime was started against, beside its resumable state, and a launch restarts
exactly the roles whose record differs from what the account carries now.

That is deliberately not keyed to a login. The live failure performed no login
-- the credential was already valid -- and was still five stale runtimes; a rule
that watched logins would have missed it exactly as the first attempt did. Nor
is it keyed to the credential file's mtime: Codex rewrites `auth.json` whenever
it refreshes a token, which changes no decision, so it produces the same
generation and restarts nobody. Completing an account's first run, or trusting a
worktree, changes it once, and the roles that could not have seen that change
are restarted once.

A role with no record is stale by definition -- nothing says what its runtime was
started against -- which is what makes an ordinary `switchyard <project>` repair
a tenant carrying sessions from before any of this existed.

The record is written after the launch, for roles that actually came up. A role
whose start failed, or whose stale session could not be ended, keeps its old
record and is named in the output, so the next ordinary launch reconciles it
instead of forgetting.

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

## Driving the launch, not just its parts

The reconciliation above returns two things -- the roles to present, and the
ones whose stale session could not be ended -- and the launch read that pair as
if it were the list of roles. Every helper had a test; the call site had none,
because the suite exercised the helpers directly and nothing drove
`launch_project`. Live, that stopped five role runtimes and then raised
`AttributeError: 'list' object has no attribute 'role'` before starting any of
them. The suite now drives `launch_project` end to end for an ordinary start,
which reproduces that exactly when the unpacking is removed.

The same run proved a second thing worth keeping: the phase rebinds its runner
when the project declares desktop access, so deciding "did a caller inject a
runner?" by reading that name afterwards answered yes for every tenant with a
pane -- and the bounded, watched foreground step would never have run live. The
question is now answered by the argument itself, `None` meaning the live path,
and the desktop adjustment is a transformation both paths apply rather than
something hidden inside one of them.

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
ask for a sign-in even though credentials exist, and that it is asked once for
the account rather than once per role.

The User answers the provider's own prompts and nothing else. Each foreground
step is bounded: the state it exists to record is watched while the CLI runs,
and the moment it appears the CLI is ended and the phase moves on. Nobody is
asked to type `/exit`, once for the account and again for every worktree --
that is a chore, not a first run.

Trust is read where the CLI really keeps it. Claude 2.1.270 records it once per
**repository**, not per directory: the live tenant's five role worktrees are
linked worktrees of one `control.git`, none of them appears in `projects`, and
every one opens at a ready prompt because that repository is the trusted entry.
Reading only the worktree path called them untrusted, scheduled a step the CLI
never prompts for, and left the watcher waiting for a key nobody was going to
write. The repository is read from the worktree's own `.git` file rather than by
running git in somebody else's tree, and a directory trusted under its own path
still counts.

A step that cannot complete ends loudly. The wait is bounded in minutes, and on
expiry it says what it was waiting for, that the CLI was ended and the run
continues, and that a prompt which never appeared means Switchyard is reading
the wrong state -- a defect here, not something for the User to answer again.

The terminal keeps its presentation across the owner boundary. `sudo` resets
the environment, and a CLI that cannot see `TERM` or `COLORTERM` draws itself in
monochrome, which is what the User was shown; those variables are now forwarded
explicitly, and nothing else is.

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
