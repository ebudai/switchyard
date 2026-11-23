# SYRD-857: a disposable VM for reviewing a release as production runs it

Audit, and the Director at Final Sign-Off, install a candidate in a throwaway
KVM VM before they sign. They use it the way production does: the real
`./install`, a tenant from `switchyard new`, the real board and listener, every
affected `--dry-run`, and the listener's own record of what it delivered. The
kit is `tests/vm_kit/`.

## Using it

From a checkout that holds the candidate (as yourself, never root):

```sh
tests/vm_kit/vm_kit.py check --commit <candidate> --keep-logs <dir>
```

`check` boots a fresh VM, installs the commit, and creates the tenant `vmt`.
Then it requires three things:
- the tenant's board runs a declared workflow;
- `research-stage enable --dry-run` passes from the Director's pane;
- the listener delivers a ticket routed to `main` into `main`'s pane, as its
  log says (`Delivered queued notification ... to vmt-main:0.0`).

It copies the logs out and powers the VM off. It took about two to three
minutes in the recorded runs, plus the image download the first time.

For any other affected `--dry-run`, use a VM of your own:

```sh
tests/vm_kit/vm_kit.py boot review1
tests/vm_kit/vm_kit.py install review1 --commit <candidate>
tests/vm_kit/vm_kit.py tenant review1 vmt
tests/vm_kit/vm_kit.py run review1 --role vmt/director -- switchyard plan-gate enable --dry-run --owner main
tests/vm_kit/vm_kit.py deliveries review1 vmt
tests/vm_kit/vm_kit.py logs review1 <dir>
tests/vm_kit/vm_kit.py down review1
```

`run` also takes `--root` and `--as <account>`. `list` shows the VMs and how
long each has left. The first run downloads the Arch cloud image and checks it
against the mirror's published SHA-256 (`fetch-image` does just that).

## Every VM is bounded

- **A supervisor owns each VM.** `boot` starts it in a session of its own, and
  qemu runs as its child. qemu gets a parent-death signal, so it never outlives
  the supervisor.
- **Each VM has a lifetime,** one hour by default (`--lifetime`). A VM with
  none is refused. At the lifetime, the supervisor presses the virtual power
  button, gives the guest 90 seconds, then stops qemu.
- **Cleanup does not depend on how qemu ended.** Either way, the supervisor
  removes the overlay, the seed and the VM's keys.
- **`down` proves the VM is gone.** It powers the guest off, presses the button
  if that fails, and then stops the supervisor. Then it checks that neither
  process it started (the supervisor and qemu) still runs, each confirmed by
  the pid it recorded and its command line, and that the overlay is gone, and
  says both.
- **`check` always ends with `down`** for a VM it created: pass or fail, and
  also when qemu ends or ssh never answers during boot. A boot that fails
  before its supervisor starts (an image `qemu-img` cannot use, no tool to
  build the seed) removes the directory it made, keys and all, and its own
  error is the one reported. A cleanup that fails is reported as well, never
  instead. `down` also removes a VM directory that recorded nothing, and
  `list` shows one as incomplete. It never takes down a
  VM of the same name that was there before it: it refuses that name, and
  `boot` creates a VM's directory atomically, so two runs cannot share one.

**Effect on the host.** The kit refuses to run as root, and never uses sudo on
the host. It changes nothing on the host except its own directory
(`$XDG_CACHE_HOME/switchyard-test-vm`, or `--state-dir`). It looks at no other
process on the host, and signals only a VM's own supervisor (the supervisor
stops its own qemu), so it cannot reach another VM. Inside the VM it is root,
with no live tenant to reach.

**Only the VM gets the kit's files.** `boot` picks a free loopback port for the
guest's ssh, but another local account could bind it first. So the kit makes
the guest's ssh host key on the host, seeds it through cloud-init (the guest
generates none of its own), and pins it in a `known_hosts` in the VM's 0700
directory. ssh and scp read no ssh config (`-F /dev/null`) and run with
`StrictHostKeyChecking=yes` against that file alone. A server showing any other key is refused before anything is sent.

## Past sign-in, offline: stand-in CLIs, not a flag

`switchyard new` stops at step 4/5 until each provider says it is signed in
and each worktree is trusted. The kit installs a stand-in `codex`
(`tests/vm_kit/standins/codex`) in the VM, and its tenants run every role on
it. The stand-in:
- reports itself signed in;
- trusts a folder it is opened in, the way Codex records a "yes" to its trust
  prompt;
- stays the pane's foreground process;
- fires the hooks Switchyard installs in `~/.codex/hooks.json`, as Codex does:
  SessionStart once, then UserPromptSubmit and Stop around every line it
  reads. So the board sees the pane go busy and idle, and the listener's
  deliveries are real.

`switchyard new` is unchanged: the whole run, measured at about 30 s, needs no
terminal and no network. A design artifact (`--from`) settles every question,
and the kit also passes the declared workflow (below).

**Why not a test-tenant flag?** A flag would put a test-only branch into the
product, past the one step that proves a provider can serve a pane, and every
real tenant would carry it. A stand-in leaves the product's path untouched,
so the review exercises exactly the code a real tenant runs.

**Why a stand-in cannot be used by accident on a real tenant:**
- **It is never installed on a host.** It lives under `tests/`, and only the
  kit copies it, into a VM it booted. A release does carry `tests/`, but never
  on any PATH.
- **It refuses to run without the kit's marker.** It needs `/etc/switchyard-test-vm`
  to be a regular file, owned by root, holding exactly the kit's text. It opens
  the path once, without following a link (or waiting on a FIFO), and makes
  every check on that open file. Only the kit's first-boot seed writes that
  file. Anywhere else it exits 3, says it is not a review VM,
  and records nothing: no sign-in, no trust, no config. A suite checks that
  on the host where the suites run.
- **`role_run.py` refuses the same way,** with the same check.

## Running a command as a role

The board accepts a role's write only from that role's live pane: from a process
in the pane's own process tree. A command run with the pane's environment from
anywhere else is refused ("local board writes must come from a live launcher
pane"). So `run --role <slug>/<role>` has `role_run.py` type the command into
the pane as a `!` line, as a CLI's shell escape takes one, and the stand-in runs
it as its own child. Its output and exit status come back to the caller.

## What the Director's attempt found

The Director's ad hoc run stopped at the sign-in step, and the tenant then
answered "no workflow configuration is active for this board". The stop was
not the cause. A tenant the kit took all the way through `switchyard new`,
every pane up, answered the same thing, with `/api/workflow` at
`{"revision": 0, "document": null}`.
- **The cause:** without `--workflow-config`, `switchyard new` seeded the legacy
  default workflow (`workflow_seed: default-project`) as table rows, and
  declared no document. A design artifact may not carry one either.
- **What the kit does:** it writes an artifact naming the shipped example
  workflow's roles, every one on the stand-in, and runs `switchyard new` with no
  `--workflow-config`. The board then runs the declared workflow `new` composes
  for that plan, as every new tenant's does.
- **Whether `switchyard new` should declare a workflow by default** was a
  product question for its own ticket. SYRD-860 settled it: `switchyard new`
  now declares the workflow its plan describes, so a tenant made without
  `--workflow-config` takes the composers at once. The kit now makes its tenant
  that way, so `check` proves the default
  ([SYRD-860](syrd-860-declared-by-default.md)).

## What it does not cover

- real providers, models or sign-in flows;
- a desktop, Konsole windows or screenshots (tenants are `--headless`, with
  the tmux viewer);
- distributions other than Arch.

The release smoke (docs/syrd-854-release-smoke.md) stays the quick check of
the release layout. The VM is the check of the whole install.

## Tests

- **`tests/vm_kit_refusals_test.py`** (no KVM, runs in the sweep sandbox):
  - the stand-in and the role runner refuse without the marker, and record
    nothing; each refuses a marker that is not root's, other text, a link and
    a FIFO, and accepts the kit's text owned as required;
  - with the marker, the stand-in signs in, trusts its folder, fires the hooks
    around each line, and runs a `!` line;
  - the kit's artifact and workflow load through the product's own
    validators;
  - names and lifetimes are refused;
  - the cloud-config seeds the host key made on the host and no other, and a
    real `sshd` on loopback is believed only when it shows that key: showing
    another, ssh and scp stop before sending anything;
  - `check` ends with `down` when a fake qemu dies during boot, and refuses,
    untouched, a VM of the same name that was already there;
  - an unusable `--image` or no ISO tool: the real cause is the error, no VM
    directory is left, and a re-run under the same name works; `down` and
    `list` handle a directory that recorded nothing;
  - with a fake qemu: a supervisor powers its VM off at its lifetime, `down`
    stops a running VM and proves it gone, and a supervisor killed outright
    takes its qemu with it.
- **`tests/vm_kit_test.py`** (`sweep_requirements`: KVM and the network): the
  whole `check` on the tree's HEAD in a real VM, and nothing left behind.
