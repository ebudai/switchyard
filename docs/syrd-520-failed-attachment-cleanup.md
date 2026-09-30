# SYRD-520: a failed attachment operation removes what it created, and only that

## What happened

To attach a frame, the board first copies it into the asset directory and
then writes the ticket. Main found during SYRD-518 that when the write was
refused, the database rolled back but the copy stayed. SYRD-519's
reproduction showed the asset directory growing from one file to two.

## Reproduced on main aabb9ae

The board was built the production way: companion roles, `schema.sql`, the
real `ticket-board-migrate`, and `rbac.sql`. It was driven through the real
`TicketBoardApp`, with temporary frame and asset directories.

| operation | result | asset directory |
|---|---|---|
| update, refused by a later step (comment without author) | refused | +1 copy left |
| update, refused by `edit_fields`' role check | refused | +1 copy left |
| update whose second path is missing | refused | +1 copy (the first) left |
| create, refused after its attachments (`add_comment` role check) | refused | +1 copy left |
| crop, refused by a declared board's role check | refused | +1 crop left |
| copy that fails after writing | refused | +1 file left |
| copy whose clock-picked name is already taken | refused | **the existing file was overwritten** with the copy, and left |
| upload attached, whether refused or kept | — | used in place: never copied, consumed or removed |
| success | ok | +1 copy, referenced |

Two points about uploads and names:
- **Uploads are never consumed through the app.** It resolves the asset
  directory, so an upload, which lives in it, is used in place.
  `copy_attachment`'s "consume an `upload_*` source" branch is unreachable
  from the app.
- **A name collision destroyed another file.** A copy's name was
  `<ticket>-<time_ns>.png`, saved with `Image.save(path)`, which overwrites.

## The change

**`new_asset_files.NewAssetFiles`: the files one operation creates.** This
is a new module. `attachment_store` stays the file store that SYRD-518 pinned,
with the same functions and imports. `copy_attachment`, `write_crop` and the
two `materialize_*` functions only gain an optional `new_files` parameter;
without it they behave exactly as before.
- **Exclusive creation.** Each file is created with `O_CREAT|O_EXCL|O_NOFOLLOW`
  through a descriptor on the asset directory it is written to, read when the
  file is created. It is recorded by that directory, its name, and its device
  and inode. A copy or crop whose name is taken picks another name; it never
  overwrites.
- **Removal on failure.** It removes a recorded file only while that name is
  still that same regular file. A file replaced since is left in place and
  reported.
- **Deferred consumption.** Consuming a copied upload waits until the
  operation is kept.
- **File mode is unchanged:** `0o666` under the umask, as a plain write.

**`new_asset_files.discarded_on_failure()`.** It wraps each operation that
writes attachment files:
- **`_pg_update_ticket` and `create_ticket_record`.**
  `TicketBoardApp._attachment_transaction(conn)` stands in for
  `conn.transaction()`, so the guard encloses the commit.
- **`crop_attachment`.** The guard encloses the crop's write and its
  connection block. psycopg commits in `Connection.__exit__` and then closes
  without raising (`pgconn.finish()`). The crop keeps its event order:
  write, then connect, set the role, append, read back, and exit.
- **If anything raises before the transaction commits**, including the commit
  itself, exactly the operation's own files are removed.
- **After a commit, nothing removes them**, even if closing the connection
  then fails.
- **If a removal fails**, the caller still gets the original error. The files
  left are added to that error as a note and logged as a warning; they are
  never silently dropped, and never reported in place of the error.

**Not touched:**
- **Pre-existing and reused assets**, uploads, and files written by a
  concurrent operation. Only this operation's recorded inodes are candidates.
- **Role and review gates, database atomicity, and filesystem confinement.**
  Names are plain names under the asset directory, opened without following
  symlinks.
- **Deleting an older attachment after a successful replacement.** That is a
  separate lifecycle policy, and it is not done here.

## Neighbours

The first version put the file record and guard in `attachment_store`. It
also wrapped the crop in `conn.transaction()`, and it read `self.asset_dir`
even when nothing was attached. That broke three neighbouring suites that
pass on main:
- `ticket_board_attachment_store_test`, which pins the store's exact
  functions and imports;
- `ticket_board_image_asset_policy_test`, whose fake connection has no
  `transaction()` and which pins the crop's event order;
- `ticket_board_input_policy_test`, whose app has no `asset_dir`.

I narrowed the change to fit rather than edit their fixtures: a separate
module, a parameter-only change to the store, and the directory read only
when a file is created. All three pass unchanged.

## Observed, not changed

On a **legacy** board, `append_ticket_attachment` accepted a crop from the
unknown role `stranger`. A declared board refuses it ("invalid configured
caller role"). This ticket does not alter role gates.

## Evidence

- **`tests/failed_attachment_cleanup_test.py`.** It covers:
  - every refusal above;
  - success and reuse, including an upload;
  - a taken name;
  - a concurrent operation's successful update during another's failure;
  - a commit that fails, via a deferred constraint trigger raising at COMMIT;
  - an error after the commit;
  - a removal that fails;
  - a file replaced since creation;
  - a refused crop and a kept one;
  - deferred consumption of an upload that was copied.

  The code and SQL from aabb9ae run in a child process to reproduce the
  orphans and the overwrite. It passes both under `env -i` and in a role
  pane.
- **Mutation, with `tests/bounded_run.py mutate`: 13 of 13 killed.** Each
  kill was confirmed from its assertion line. The mutants cover:
  - no removal;
  - keeping the files before the commit;
  - no identity check;
  - non-exclusive creation;
  - a silent failed removal;
  - discarding on success;
  - immediate consumption;
  - create, update or crop unguarded;
  - a swallowed error;
  - no log;
  - no name retry.
- **Sweep against aabb9ae.** 41 suites were run under `env -i`: every suite
  naming attachments, screenshots, crops, uploads, `attachment_store`, or the
  create and update paths, minus the launcher, presentation and upgrade
  suites that drive tmux or root paths. Browser suites ran through a wrapper
  that points Playwright's `launch()` at the host's cached headless shell,
  the same way on both trees.
  - 36 pass on both trees, and 5 fail on both at the same final line.
  - `ticket_board_write_api_test` stops before its upload, attach and crop
    cases. With only its step that already fails skipped, it prints `ok` on
    both trees.
