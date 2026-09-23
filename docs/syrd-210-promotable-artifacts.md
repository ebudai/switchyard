# A launcher is not a promotable artifact

SYRD-210 gave a host-wide CLI promotion so a tenant owner need not install its
own copy. Live fresh-host UAT then disproved the acceptance, on `sbs`:

> Selecting `p` for Hermes copied `/home/santiago/.local/bin/hermes`, but
> verification as the future owner `sbs-agent` exited 126 because the launcher
> still execs `/home/santiago/.hermes/hermes-agent/venv/bin/python`, which the
> tenant cannot traverse.

A per-user install is often a launcher: a few lines whose runtime is a
virtualenv inside the operator's own home. Copying it host-wide copies the
pointer and not the runtime, and every tenant then execs a path under an account
it cannot enter — exit 126, at somebody's first pane.

## Two defects, not one

**It was offered as the one-keystroke default.** The promotion flow found the
caller-local executable and offered it as `[p]`, which is the right idea for a
self-contained binary and wrong for a launcher. Its first two lines already said
it could not serve a tenant; an operator paid a sudo prompt and a failed
provisioning to find that out.

**The copy was installed before it was verified.** `promote_agent_cli_host_wide`
staged the file, `os.replace`d it onto the destination, and *then* ran it in a
tenant context. So a failed verification had already overwritten whatever was
there — the failure and the damage were the same step. Reproduced against the
filesystem: with a working host-wide copy in place and an unpromotable launcher
offered, the existing copy did not survive the attempt.

## Read the bits, not the access

The reachability question is "could an account that owns nothing here use this",
and the two accounts that ask are the two that cannot see the problem: the
operator, who owns the home in question, and root, who bypasses the check
entirely. `switchyard new` runs as root, which is exactly why this was invisible
until a real tenant tried it.

So `_reachable_by_a_stranger` walks the path and reads the permission bits —
every directory needs `o+x`, the file needs `o+r` — rather than trying an
access. A probe of the old behaviour promoted the live launcher *successfully*
on this host, because the account running the check could traverse its own
`0700` home.

Both launcher shapes are covered: the shebang interpreter, and the more common
`#!/bin/sh` wrapper whose next line `exec`s a private interpreter — there the
shebang is `/bin/sh` and says nothing, and only scanning the body finds it.
`/usr/bin/env X` is left alone: what that resolves to depends on the PATH it is
run with, which is the verification's question and not this one.

**Not detected here:** a compiled executable linked against shared libraries in
a private home. Scanning a binary for paths is guesswork, and `ldd` runs code.
What catches those is the verification, which now happens before anything is
installed.

## What changed

- `agent_cli_unreachable_dependencies` inspects a candidate and names each file a
  tenant could not reach, and why.
- `resolve_agent_cli_source` refuses such an artifact. Both promotion paths
  resolve the source there, and `promote_agent_cli_through_sudo` resolves
  locally first — so the refusal happens **before sudo is asked for**, with the
  supported alternatives intact: name another local executable, switch the
  affected roles to a CLI that is already host-wide, or abort before anything is
  created.
- Neither offer presents a launcher as promotable. The new-tenant flow drops
  `[p]`, says why, and defaults to `[l]`. The resumed-tenant flow has no
  alternative CLI to offer, so it says why and leaves the host and the running
  tenant alone rather than asking an unanswerable question.
- The promotion verifies the **staged** file and replaces the destination only
  if it passes, so a failed verification leaves any existing host-wide copy
  exactly as it was, with no staging file behind.

Nothing about the supply-chain boundary moves: no vendor installer is fetched or
executed, and the operator still owns which version every tenant runs.

## Verification

`tests/agent_cli_host_wide_test.py`, now 29 tests. The new ones:

- reachability is read from the bits, asserting first that *this* account can
  reach the interpreter — otherwise the case proves nothing — and then that a
  stranger cannot;
- the `#!/bin/sh` wrapper shape, where the shebang is reachable and the exec'd
  runtime is not;
- the new-tenant offer does not present it, names the unreachable path, and
  still offers name-a-path, switch-CLI and abort;
- the resumed-tenant offer asks nothing and continues unchanged;
- it is refused before sudo, asserting nothing was run at all;
- a failed verification leaves the existing host-wide copy byte-identical and no
  staging file;
- a control: a self-contained executable is still promoted, and is verified
  before it is installed rather than after.

Mutation: 7 mutants, no survivors. Two were worth the run rather than the
confirmation — the body-path scan survived until the wrapper case existed, and
the resumed-tenant gate survived until that offer was covered.

One mutant had to be rewritten: the first version left an unbalanced
parenthesis, so it was "killed" by a syntax error rather than by behaviour,
which proves nothing.

## Live acceptance

Unchanged and not done from here: the fresh-host run belongs to whoever holds
the VM.
