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
