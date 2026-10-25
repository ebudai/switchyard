# SYRD-683: onboarding docs refresh from an installed release

## The problem

Each onboarding doc a project gets (`docs/onboarding/*.md`) starts with a
header naming the Switchyard commit it was copied from. `switchyard upgrade`
replaces a doc only when it is still exactly what that commit shipped, so the
upgrade needs the doc's body at that commit.

When the source is an installed release (`/opt/switchyard/releases/<commit>`,
which has no `.git`), the upgrade looked for that body only in the tenant's
`--commit-git-dir`. For most tenants that is the project's own `control.git`,
which never holds a Switchyard commit. So every doc was skipped with "cannot
verify source commit", on every upgrade. On pie (`d24a15cf` → `fd06cac8`) all
five were skipped, though the old release was installed the whole time.

## Where the shipped body comes from now

`scripts/onboarding_provenance.py` reads it from trusted sources only, in this
order.

1. **The installed release of exactly that commit,**
   `<install root>/releases/<commit>`:
   - the commit in the header must be a full 40-character lowercase hash, so it
     cannot name a path;
   - the release's marker must record that commit;
   - from `/` down to the doc, every directory is opened by descriptor, one
     component at a time, without following a link. Each must belong to root
     and be writable by nobody else;
   - the doc must be a regular file that meets the same rules.

   The installed copy is then compared with the shipped body. Only the exact
   generated header is set aside, and the body is read as installation reads
   it: UTF-8 with universal newlines. So any other byte counts as an edit, a
   line-ending change included. No Git is needed.
2. **Otherwise, a source cache named in `--commit-git-dir`, but only one root
   keeps.** The cache directory gets the same proof from `/` down, and so do
   its `objects`, `refs` and `config`.
   - A tenant's `control.git` is the tenant's to rewrite, so it is never read
     as Switchyard history, even when it holds the very commits.
   - The cache is read with `git --no-replace-objects`, and must hold both the
     stamped commit and the release commit.

If neither source can be used, the doc is skipped. The message names every
source looked at and why it was not used, for example:

    switchyard: onboarding doc README.md: skipped, cannot verify source commit <c>:
    /opt/switchyard/releases/<c> does not exist; source cache
    /home/pie-agent/.local/state/switchyard/projects/pie/control.git is not root's,
    so it is not read as Switchyard history: /home/pie-agent belongs to pie-agent, not root

A refresh or an edited copy also names what it was checked against.

## What an upgrade does with each doc

| Installed copy | Outcome |
| --- | --- |
| Already this release's copy | `current at <commit>`. Nothing is read or written, so a repeated upgrade changes nothing. |
| No header, or a header naming another doc | Skipped, as before. |
| Exactly what the stamped release shipped | Refreshed to the selected release, header included. |
| Anything else: edits, a project's own README section, changed line endings, a cut or extended body, a changed header | Skipped as edited. The file is left exactly as it was. |
| A symbolic link | Skipped. It is never read or written through. |

**History and ancestry.** With history (a source checkout, or a cache root
keeps), a copy from a commit that the selected source does not contain is
still held back as "newer than this checkout". An installed release carries no
history, so on that path the docs follow the release being deployed. A rollback
to an older release therefore also rolls back untouched docs.

**Dry run.** A dry run reaches exactly the same decision as the upgrade.

**How a doc is replaced.**
- As root, by descriptor, keeping the replaced file's mode, as before.
- As the project's owner, through a sibling file renamed over the old one,
  keeping its mode. Nothing ever sees half a doc.

## Proof

`tests/onboarding_release_provenance_test.py` builds an old and a new installed
release and two tenants (pie and otto). Each tenant's `control.git` holds only
its own commits. The suite covers:

- **Refresh:** untouched copies refresh in the dry run and in the upgrade, with
  the same decisions in both. pie's edited README stays byte for byte.
- **Replacement:** a refreshed file is a new file renamed into place with the
  same mode, and nothing staged is left beside it.
- **Repeats:** a second upgrade reports every doc current and changes nothing.
- **The tenant's repository** is never asked.
- **Edits are never mistaken for the shipped copy:** CRLF line endings, a BOM,
  a prefix, a dropped final newline, appended text, a same-size change, and
  altered headers (a doubled space, capitals, another doc's name, no blank
  line, a newer commit over the old body, a short commit, a path) are each
  kept untouched.
- **An unusable old release** is named, and nothing is written. The cases: the
  release missing, its marker naming another release, its marker not JSON, the
  release, its docs or the doc a link, the doc missing, the doc a FIFO, and its
  directories writable by others. So is an unknown commit, and so is a link
  planted at a tenant's copy.
- **The real default** (root, from `/`) refuses a tree that is not root's. It
  reads a real installed release where one exists.
- **In a user namespace with two real identities,** a tenant's `control.git`
  that holds the very Switchyard commits is never read. A cache root keeps is
  used when the old release is gone:
  - a replace ref planted in it changes nothing;
  - its ancestry check still holds a newer copy back;
  - its `refs` handed to another account make it untrusted.
