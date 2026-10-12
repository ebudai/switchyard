# SYRD-434: the fresh-packet check tells retiring a publisher from installing one

## What was there

`role_control_sudoers_install_test::test_fresh_provisioning_grants_a_shared_account_project_nothing`
checks the `operator-commands.sh` packet for a project provisioned today. Two of
its assertions required the packet never to mention `switchyard-publish-ref` or
`switchyard-integrate-main`.

Since SYRD-123 the packet names both, in two lines that take them away:

```
sudo rm -f '/usr/local/lib/switchyard/otto/switchyard-publish-ref'
sudo rm -f '/usr/local/lib/switchyard/otto/switchyard-integrate-main'
```

They come from `RETIRED_STAGED_EXECUTABLES`, in
`provision_role_tooling.py`. An upgrade can take a program away only by naming
it, so a tenant that an older release gave a root-owned publisher loses it.

The check was wrong in two ways:

- **It failed on every packet.** Main fails at
  `assert "switchyard-publish-ref" not in commands`. `run_module_tests` stops at
  the first failure, so the module's later tests never ran on main either. They
  all pass now.
- **It would pass the dangerous packet.** If the generator retired nothing, the
  names would disappear from the packet and the check would pass, while every
  upgraded tenant kept its publisher on disk.

## The change

This is a test-only change. No generator or production file changes.

`_retired_publisher_lines(script, project)` reads every packet line that names
either publisher. A line counts as a removal only if the shell reads it as
exactly `sudo rm -f <the project's staged path to that publisher>`: four words,
nothing chained after them, and no comment. Every other line naming a publisher
is returned as "the rest". That covers an install, a grant, an invocation, a
comment, a chained or continued line, another project's path, and `rm -rf`.

The fresh-packet check now asserts three things:

- there is no `NOPASSWD: /usr/local/lib/switchyard` grant (unchanged);
- the rest is empty;
- the removals are exactly one for each publisher, in order.

The "no role control interface" and no-install-line assertions are unchanged.

`test_the_retired_publisher_reader_admits_only_a_removal` feeds the reader one
of each kind of line, so a reader that calls everything a removal cannot pass.

## Tests

**`tests/role_control_retired_cleanup_test.py`** runs the fresh-packet check as
it was on main, from a git archive, and as it is now. Each run happens in its
own child process against its own tree's generator. The checks see the packet
as shipped, and the same packet with one fault planted:

| packet | main's check | this check |
|---|---|---|
| shipped | fails on `"switchyard-publish-ref" not in commands` | passes |
| `dropped`: nothing retired | **passes** | fails: `assert removals == [` |
| `staged`: publisher installed instead | fails | fails: `assert rest == [], rest` |
| `restaged`: `rm -f … && install …` | fails | fails: `assert rest == [], rest` |
| `invoked`: packet runs the integrator | fails | fails: `assert rest == [], rest` |
| `granted`: NOPASSWD rule for the publisher | fails | fails: the grant check |

Every plant is proved to have arrived in the packet its check read. Both trees
generate the same shipped packet once the tree path is normalised.

## Evidence

- **Mutation:** 11 mutants of the reader and the new assertions, all killed.
  - **Eight by `role_control_sudoers_install_test` itself:** a chained,
    comment-trailed, `rm -rf` or other-project line read as a removal; lines
    naming only one publisher skipped; whitespace splitting; non-removals
    dropped; and reading only the publisher.
  - **Three by the comparison:** no rest check (`invoked` passes), no removal
    check (`dropped` passes), and no grant check (`granted` fails at the rest
    check instead).
- **The sweep:** 59 related suites, run serially on main (0f6651c) and on this tree. The only exit-code changes are `role_control_sudoers_install_test` (1 to 0) and the new suite. 13 suites are red on both, with identical normalised output. `team_launcher_layout_upgrade_commands_test` rewrote its known fixture files `role0..5.provider-state.json` under `~/.local/state/pgu-ticket-board/pane-sessions` on both runs.
