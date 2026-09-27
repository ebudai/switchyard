# SYRD-272: source size inventory and launcher extraction plan

This document serves the SYRD-272 parent. Each extraction child updates it:
the before/after table, the slice log, and the plan's next entry. SYRD-286 was
the first slice, SYRD-287 the second, SYRD-288 the third, SYRD-289
the fourth, SYRD-290 the fifth, SYRD-291 the sixth, SYRD-292
slice 6a, SYRD-293 slice 6b, SYRD-294 slice 6c,
SYRD-295 slice 7a, SYRD-296 slice 7b, SYRD-297 slice 7c-1, SYRD-298
slice 7c-2a, SYRD-299 slice 7c-2b, SYRD-300 slice 7c-3, SYRD-301
slice 8a, SYRD-302 slice 8b, SYRD-303 slice 9a, SYRD-304 slice 9b, SYRD-305 slice 10a, SYRD-306 slice 10b, SYRD-308 slice 10c, SYRD-309
slice 11a, SYRD-310 slice 11b, SYRD-311 slice 11c, SYRD-312 slice 11d, SYRD-313 slice 11e, SYRD-314 slice 11f, SYRD-316 slice 12a, SYRD-317 slice 12b and SYRD-318 slice 12c.

- Baseline: public main `d5ffdd00a2b162ac9bee545f52ba0840f70fc7ed`. The
  exclusive refactor window opened at this commit.
- SYRD-286 was integrated as `4af050e436123fc1ad7d2aed0034ed771cc5bba9`, which
  is SYRD-287's baseline.
- SYRD-287 was integrated as `95c11f0ae3f8e6b873aa1e42c99a7064558fa2af`, which
  is SYRD-288's baseline.
- SYRD-288 was integrated as `fd85a84a91636b3230fbf238d47c1ae7e24fe957`, which
  is SYRD-289's baseline.
- SYRD-289 was integrated as `3e7337bef48d93363dd8e3ae018cc03cb5b79fe1`, which
  is SYRD-290's baseline.
- SYRD-290 was integrated as `9c804c7d26731af80a347a5612424c899f79e352`, which
  is SYRD-291's baseline.
- SYRD-291 was integrated as `9ceed50d1b1f9c4c95565865240164cd9cf8574c`, which
  is SYRD-292's baseline.
- SYRD-292 was integrated as `6f0ccb733c0b6df1a8627c77d021238056cad973`, which
  is SYRD-293's baseline.
- SYRD-293 was integrated as `37bbb53ce823f222e20fa6beb20a0bffa57827af`, which
  is SYRD-294's baseline.
- SYRD-294 was integrated as `501ca7697dabc0eea58641cfa3393965d9598aeb`, which
  is SYRD-295's baseline.
- SYRD-295 was integrated as `fede447580805287cbdfb8b149d1e1d7a846636d`, which
  is SYRD-296's baseline.
- SYRD-296 was integrated as `5beb317fcba8040cc29e4a00187a76c884af512a`, which
  is SYRD-297's baseline.
- SYRD-297 was integrated as `9aecc37fc08e17638dbf73c52db41b61b0604aa0`, which
  is SYRD-298's baseline.
- SYRD-298 was integrated as `14d1cc93211c78f1f3e29bcc9f72ad14e550ed3f`, which
  is SYRD-299's baseline.
- SYRD-299 was integrated as `57cac704c13ca3372b5b8f35352bb08a1cdd8e84`, which
  is SYRD-300's baseline.
- SYRD-300 was integrated as `f29f5a0eb158946c789ce227ba44f964a94deb4f`, which
  is SYRD-301's baseline.
- SYRD-301 was integrated as `1b07f8661b8d14d363e163c04d81a6502b5d9b83`, which
  is SYRD-302's baseline.
- SYRD-302 was integrated as `1e84784520dceb32b9a4070ad938382407ce0a3e`, which
  is SYRD-303's baseline.
- SYRD-303 was integrated as `fcafa6579383d330aa73e9af4d5fc9439091fa7a`, which
  is SYRD-304's baseline.
- SYRD-304 was integrated as `1e0dd965554a3a69f22a5c5c6966a5efac362fcc`, which
  is SYRD-305's baseline.
- SYRD-305 was integrated as `96953dea97dd979c92f1f6c188bdb00a2000e6f0`, which
  is SYRD-306's baseline.
- SYRD-306 was integrated as `ce43dcf544a1dd298834aef2dca70a63510f5f2b`, which
  is SYRD-308's baseline.
- SYRD-308 was integrated as `3b60d235fd6334b7a08a7d0c887eaf6194e721f0`, which
  is SYRD-309's baseline.
- SYRD-309 was integrated as `01b036bba6f130ed7f9c39d85dc4e46ecb58002a`, which
  is SYRD-310's baseline.
- SYRD-310 was integrated as `402570956fad894e39a1d2baa3327eea43cefb9a`, which
  is SYRD-311's baseline.
- SYRD-311 was integrated as `cbee510d8d7a7cb1f7a7641fc8650ebc3176a044`, which
  is SYRD-312's baseline.
- SYRD-312 was integrated as `1eaf302f377e41720caa3cc9142a94e835264c8a`, which
  is SYRD-313's baseline.
- SYRD-313 was integrated as `4296bed9eb3238437120aace76fa91d712b05f79`, which
  is SYRD-314's baseline.
- SYRD-314 was integrated as `65f094a7d5ec49cf0ea2715e32f7e5b257affaae`, which
  is SYRD-316's baseline.
- SYRD-316 was integrated as `1e7776ec46c1db7d25567bb7f3b6cfd8570b5fc9`, which
  is SYRD-317's baseline.
- SYRD-317 was integrated as `8b5a33226f97728399e4dffa0d048611a3eb824e`, which
  is SYRD-318's baseline.
- Soft limit: 1,250 lines, advisory. The pre-commit warning is unchanged.

## 1. Tracked source inventory

These are the tracked non-test files over the soft limit at the baseline,
followed by the parent's list. Each "after" column is that child's candidate.

| File | Baseline | After SYRD-286 | After SYRD-287 | After SYRD-288 | After SYRD-289 | After SYRD-290 | After SYRD-291 | After SYRD-292 | After SYRD-293 | After SYRD-294 | After SYRD-295 | After SYRD-296 | After SYRD-297 | After SYRD-298 | After SYRD-299 | After SYRD-300 | After SYRD-301 | After SYRD-302 | After SYRD-303 | After SYRD-304 | After SYRD-305 | After SYRD-306 | After SYRD-308 | After SYRD-309 | After SYRD-310 | After SYRD-311 | After SYRD-312 | After SYRD-313 | After SYRD-314 | After SYRD-316 | After SYRD-317 | After SYRD-318 | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `scripts/team_launcher.py` | 38,527 | 37,808 | 36,670 | 36,379 | 35,730 | 34,987 | 34,365 | 33,826 | 33,573 | 33,377 | 32,030 | 31,654 | 31,495 | 31,209 | 30,284 | 29,664 | 28,942 | 28,485 | 27,498 | 26,888 | 26,225 | 25,822 | 24,899 | 24,575 | 24,100 | 23,997 | 23,804 | 23,198 | 23,099 | 22,940 | 22,357 | 22,053 | Plan in §3. |
| `scripts/worker_pool_command.py` | — | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | New in SYRD-286. |
| `scripts/role_credentials.py` | — | — | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | New in SYRD-287. |
| `scripts/agy_credential.py` | — | — | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | New in SYRD-287. |
| `scripts/upstream_report.py` | — | — | — | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | New in SYRD-288. |
| `scripts/host_accounts.py` | — | — | — | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | New in SYRD-288: a dependency-free leaf. |
| `scripts/project_onboarding.py` | — | — | — | — | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | New in SYRD-289. |
| `scripts/role_command.py` | — | — | — | — | — | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | New in SYRD-290. |
| `scripts/model_validation.py` | — | — | — | — | — | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | New in SYRD-290. |
| `scripts/project_worktrees.py` | — | — | — | — | — | — | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | New in SYRD-291. |
| `scripts/launcher_checkout.py` | — | — | — | — | — | — | — | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | New in SYRD-292. |
| `scripts/owner_git.py` | — | — | — | — | — | — | — | — | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | New in SYRD-293. |
| `scripts/pane_hooks.py` | — | — | — | — | — | — | — | — | — | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | New in SYRD-294. |
| `scripts/agent_cli_discovery.py` | — | — | — | — | — | — | — | — | — | — | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | New in SYRD-295. |
| `scripts/agent_cli_promotion.py` | — | — | — | — | — | — | — | — | — | — | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | New in SYRD-295. |
| `scripts/first_run_setup.py` | — | — | — | — | — | — | — | — | — | — | — | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | New in SYRD-296. |
| `scripts/provider_auth_status.py` | — | — | — | — | — | — | — | — | — | — | — | — | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | New in SYRD-297. |
| `scripts/provider_screen.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | New in SYRD-298: a leaf. |
| `scripts/provider_session.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | New in SYRD-299: imports only `provider_screen` at its top. |
| `scripts/first_run_auth.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | New in SYRD-300: imports `provider_session` and `runtime_catalog` at its top, never the launcher. |
| `scripts/project_status.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | New in SYRD-301: imports nothing of Switchyard's at its top. |
| `scripts/board_services.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | New in SYRD-302: imports nothing of Switchyard's at its top. |
| `scripts/workflow_adoption.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | New in SYRD-303: imports nothing of Switchyard's at its top. |
| `scripts/pane_rebind.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | New in SYRD-304: imports nothing of Switchyard's at its top. |
| `scripts/role_account_migration.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | New in SYRD-305: imports nothing of Switchyard's at its top. |
| `scripts/provider_runtime_state.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | New in SYRD-306: imports only the `account_drop` leaf at its top. |
| `scripts/account_drop.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | New in SYRD-306: a stdlib-only leaf (the privilege drop). |
| `scripts/role_identity_cutover.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | New in SYRD-308: imports only the `release_refs` leaf at its top. |
| `scripts/release_refs.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | New in SYRD-308: a one-constant leaf (the default deploy ref). |
| `scripts/tmux_viewer.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | New in SYRD-309: imports nothing of Switchyard's at its top. |
| `scripts/session_records.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | New in SYRD-310: imports nothing of Switchyard's at its top. |
| `scripts/session_paths.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 177 | 177 | 177 | 177 | 177 | 177 | 177 | New in SYRD-311: imports nothing of Switchyard's at its top. |
| `scripts/role_sessions.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 252 | 252 | 252 | 252 | 252 | 252 | New in SYRD-312: imports nothing of Switchyard's at its top. |
| `scripts/provider_resume.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 360 | 360 | 360 | 360 | 360 | New in SYRD-313: imports nothing of Switchyard's at its top. |
| `scripts/role_pane_entry.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 407 | 407 | 407 | 407 | 407 | New in SYRD-313: imports only `launcher_env` and `provider_resume` at its top. |
| `scripts/launcher_env.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 35 | 35 | 35 | 35 | 35 | New in SYRD-313: a stdlib-only leaf (`_env_first`, `DEFAULT_PANE_STATE_DIR`). |
| `scripts/tmux_session_argv.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 152 | 152 | 152 | 152 | New in SYRD-314: imports nothing of Switchyard's at its top. |
| `scripts/presentation_reconnect.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 213 | 213 | 213 | New in SYRD-316: imports only `layout_modes` at its top. |
| `scripts/layout_modes.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 19 | 19 | 19 | New in SYRD-316: a stdlib-only leaf (the four `LAYOUT_MODE_*` constants). |
| `scripts/desktop_presentation.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 508 | 508 | New in SYRD-317: imports only `layout_modes` at its top. |
| `scripts/desktop_layout_writer.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 199 | 199 | New in SYRD-317: imports nothing of Switchyard's at its top; root's no-follow crossing write. |
| `scripts/gui_window_launch.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 389 | New in SYRD-318: imports nothing of Switchyard's at its top; the root→desktop crossing and the Konsole window. |
| `scripts/ticket_board/schema.sql` | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | Proposed exception: one DDL document applied whole. It is still reviewed as its own child. |
| `scripts/ticket_board/project_provision.py` | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | Parent list; needs a child. |
| `scripts/ticket_board/notify_listener.py` | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | Parent list; needs a child. |
| `scripts/presentation_controller.py` | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | Parent list; needs a child. |
| `scripts/ticket_board/app.py` | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | Parent list; needs a child. |
| `scripts/ticket_board/server.py` | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | Parent list; needs a child. |
| `scripts/ticket_board/migrations/pgu921_syrd11_declarative_workflow.sql` | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | Proposed exception: a migration is immutable history. |
| `scripts/ticket_board/frontend_script_core.py` | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | Parent list; generated front-end asset. Its boundary is the asset, not Python modules. |
| `scripts/ticket_board/write_client.py` | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | Parent list; needs a child. |
| `scripts/ticket-board-service.sh` | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | Parent list; a shell entry point. Split along its own subcommands. |
| `scripts/ticket_board/migrations/pgu528_workflow_rbac_config_authoritative.sql` | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | Proposed exception: migration. |
| `scripts/ticket_board/migrations/pgu589_depersonalize_user_role.sql` | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | Proposed exception: migration. |
| `deploy/SYRD-87-recover-syrd-runtime.sh` | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | Proposed exception: a one-off recovery packet kept as a record. |

For comparison only: 16 test files are over 1,250 lines, the largest being
`tests/ticket_board_postgres_triggers_test.py` at 4,034. They are not in this
plan.

The exceptions above are proposals. The Director reviews and records them on
SYRD-272; none is taken as granted here.

To reproduce the counts:

```sh
git ls-files | grep -v '^tests/' | grep -Ev '\.(md|json|txt|png|svg|lock)$' \
  | xargs wc -l | sort -rn | awk '$1 > 1250'
```

## 2. `scripts/team_launcher.py`: responsibilities and seams

### The public surface a split has to keep

- **Entry points.** `./switchyard`, `scripts/switchyard`, `scripts/team-launcher`
  and `scripts/switchyard-viewer-layout` all import `scripts.team_launcher`, so
  every entry point uses one module object.
- **Importers.** 114 files import the launcher: 104 tests and 10 non-test files. These
  include `presentation_controller`, `worker_pool`, `workflow_launcher`,
  `workflow_manage` and `onboarding_readiness`. Together they read about 487
  distinct attributes as `team_launcher.<name>`.
- **CLI verbs.** The verbs come from `SWITCHYARD_COMMANDS`. Each parser and
  handler is dispatched from `switchyard_main`.
- **Monkeypatch seams.** Tests patch launcher facilities on the module, for
  example:
  - `team_launcher.current_user_name` (98 sites);
  - `_owner_home_for_auth`;
  - `_cli_auth_status`;
  - `_workdir_is_trusted`;
  - `load_project_config`.

  Moved code that calls such a facility must look it up on `team_launcher` at
  call time. Otherwise the patches silently stop reaching it.
- **Git ownership lint.** `tests/team_launcher_git_ownership_lint_test.py`
  scans every module in its `GIT_LINTED_MODULES`. Since SYRD-293 that is
  `team_launcher.py`, `project_worktrees.py`, `launcher_checkout.py` and
  `owner_git.py` (the chokepoint and its call sites). The test
  `test_every_module_that_defines_a_git_builder_is_linted` fails if any
  `scripts/` module defines a `git_*_args` builder outside that set, so a slice
  that moves builders must add its module in the same commit.

### Responsibility map (heuristic)

Each top-level definition is assigned to a domain by ordered name rules. The
preceding comments and blank lines are counted with it, so the column sums to
the whole file. This is a map for planning, not an exact boundary: every child
re-derives its own closure before moving anything.

| Domain | Lines | Defs |
|---|---:|---:|
| provisioning (new/register/teardown/owner accounts) | 5,051 | 182 |
| release selection, install and upgrade | 4,787 | 120 |
| desktop, presentation windows and display bridge | 4,437 | 193 |
| project config and registry | 2,721 | 68 |
| privileged boundary, tenant control and repair | 2,717 | 80 |
| agent CLI discovery, promotion and first-run auth | 2,633 | 70 |
| general helpers (unclassified) | 2,442 | 222 |
| tmux panes and sessions | 2,076 | 94 |
| provider state, runtime registration and role identities | 1,761 | 46 |
| workflow declaration and rebind | 1,567 | 36 |
| model/effort/CLI runtime selection — **command construction and model validation moved by SYRD-290** | 1,456 | 70 |
| credentials (agy, role seeding, upstream report) — **agy and role seeding moved by SYRD-287; upstream report by SYRD-288** | 1,436 | 65 |
| repository hooks, git and worktrees — **project worktrees and control repository moved by SYRD-291** | 1,196 | 68 |
| CLI parsers and dispatch | 1,157 | 15 |
| board service, listener and status | 1,095 | 54 |
| onboarding docs, prompts and skills — **docs, director onboarding and board skill moved by SYRD-289** | 796 | 32 |
| worker pool — **moved by SYRD-286** | 737 | 19 |
| launcher checkout self-update — **moved by SYRD-292** | 462 | 15 |

### How a slice is chosen and cut

1. **Compute the closure.** Take the domain's definitions, plus every helper
   whose callers all lie inside it.
2. **Measure the coupling.** Count:
   - references from the closure to the rest of the launcher;
   - references from the rest into the closure;
   - how many tests patch its names.

   Prefer few inbound edges and no patched names.
3. **Move the code unchanged.** Move it into one named module. The launcher
   imports that module at top level, by explicit name, and nothing else. The
   module never imports `team_launcher` at top level; it reads launcher
   facilities through `from scripts import team_launcher as launcher` inside
   the functions that need them. `scripts/worker_pool.py` already uses this
   pattern.
4. **Keep old names importable.** Every moved name that was reachable as
   `team_launcher.<name>` stays so, through that explicit import list. There is
   no `import *`, no `__getattr__` and no generated globals.
5. **Prove the move is unchanged.** Since SYRD-287 the proof is by AST and
   comments, not line spans:
   - every moved definition's AST equals the baseline's once `launcher.X` is
     read as `X` and the function-level launcher import is dropped;
   - the launcher's top-level nodes equal the baseline's minus the moved ones,
     plus only the new import statements;
   - comment lines are conserved as a multiset;
   - every name the baseline bound at top level is still bound, unless it is a
     moved private name nobody outside reads.

   SYRD-286's text diff shared its line spans with the tool that did the cut.
   In SYRD-287 the same kind of span bug cut two stay-behind constants and
   still passed that diff. The independent proof caught it, and it was shown to
   fail on that fault, on a changed body and on a lost comment. Run
   retroactively on SYRD-286 (`4af050e` against `d5ffdd0`), it also holds.
6. **Default arguments are bound at import.** A moved function whose
   parameter defaults to a launcher name cannot defer that name to call time.
   Such a dependency moves to a leaf module that both the launcher and the
   moved code import, so it stays one object. The extractor refuses a launcher
   name in a default or decorator (SYRD-288).
7. **No name may be left unbound.** The proof also fails on any name used but
   bound nowhere, in the launcher or a new module. A module header built by hand
   can miss an import: SYRD-288 caught `dataclasses.replace` this way.
8. **Imported launcher names are launcher attributes too.** A name the
   launcher binds by importing it from another Switchyard module (such as
   `home_dir_for_user` since SYRD-288) is still patched on the launcher by the
   suites. Moved code that calls it reads it from the launcher at call time,
   unless it is only a default value (see rule 6). The extractor treats such
   names as launcher facilities since SYRD-289. The first cut of SYRD-289 left
   one unrewritten, and the unbound-name check (rule 7) caught it.
9. **Source-scanning guards pin text to a file.** Some suites read
   `team_launcher.py` as text. `launch_without_model_probes_test` requires
   exactly one `validate_models=True` call site there, and the git ownership
   lint scans only that file. Before moving a definition, check whether such a
   guard names it. Either leave the guarded site in the launcher (SYRD-290 left
   `switchyard_validate_models_command`), or widen the guard in the same commit
   with the reason stated.
10. **No agent CLI runs in these suites.** Claude, Codex and agy are installed
    on this host, and Claude is logged in, so a suite that finds a real
    authenticated CLI would make a paid request
    (`team_launcher_model_tool_call_probe_test` has such a case). Slices that
    touch model or CLI code run their suites with stubs for `claude`, `codex`,
    `agy`, `hermes` and `gemini` ahead of `/usr/bin` on PATH; each stub refuses
    and exits 1. The one exception is `codex_effort_config_key_test`, whose
    Codex runs only inside `unshare --net`.
11. **A second launcher module object.** `desktop_access_test`'s exported-release
    case loads `team_launcher.py` again, under another module name. Moved code
    resolves `scripts.team_launcher`, not that copy. That case has failed at the
    same assertion since before SYRD-286 (checked on `d5ffdd0`), before it
    reaches any moved code, so it gives no evidence either way. A slice that
    wants it as evidence must first make it pass on the baseline.
12. **A name defined twice binds to its last definition.** The launcher
    defines `_normalized_path` twice: at line 3091, returning a `str`, and at
    line 3999, returning a `Path`. Every caller gets the later, `Path`
    version. SYRD-293's first cut moved that later copy, and the leftover
    `str` copy then rebound the launcher's name. The node check (rule 5)
    caught it. The extractor now refuses to move any name defined more than
    once. `presentation_window_processes` is also defined twice, which matters
    for the presentation slices. Removing the dead copy would be a fix, and is
    left for its own ticket.
13. **Keep contractual patch seams.** Suites patch launcher names and expect the
   patch to reach code that is now elsewhere. A moved caller reads such a name
   from `team_launcher` when it runs, even if the definition itself moved.
   SYRD-287 does this for the owner-traversal checks.
14. **Per-case runners must import as the suite does.** A runner that also
    puts `scripts/` on `sys.path` can load a second launcher module (a bare
    `team_launcher` next to `scripts.team_launcher`). A patch then misses the
    code under test. In SYRD-299 that let a baseline case of
    `team_launcher_switchyard_resolution_test` reach a real `sudo`, on both
    trees. It was refused for want of a password, and nothing changed. Run each
    case in its own process, with `sys.path[0]` set to `tests/` as running the
    file gives, and put a refusing `sudo` stub ahead of `/usr/bin` on PATH.
15. **A shared sentinel moves with its first reader, and is re-exported.**
    `NO_RUNNER_INJECTED` is compared by identity: it is the difference
    between a watched setup window and an unwatched one (SYRD-221). The auth
    phase and `switchyard new` both use it as a default argument. SYRD-300
    moved it with the phase, and the launcher imports it at its top, so both
    defaults are bound to one object when they are defined. The boundary test
    pins that; a second copy would pass every behavioural suite that injects a
    runner.
16. **A seam patched by rebinding a constant needs time as evidence.** A suite
    that rebinds a wait (SYRD-302: `OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS`,
    60 s to 2 s) still passes if the rebinding stops reaching the code; it only
    gets slower. Time the case with the rebinding reached and bypassed, and
    keep a static check that the name is only read through the launcher.
    Never mutate the worktree while another run reads it.
17. **Moved seams live on `scripts.team_launcher`.** Seven suites put
    `scripts/` on `sys.path` and `import team_launcher` bare, a second module
    object. Before a move, the code under test read the bare copy's globals,
    so patches there landed. After a move, the code reads
    `scripts.team_launcher`, and patches on the bare copy miss. SYRD-303 hit
    this in `legacy_workflow_migration_test`, which was green on the
    baseline and red on the first candidate. That suite now imports
    `scripts.team_launcher as tl`: same cases, same patches, patching the
    module the code reads. Before moving code a bare-importing suite drives,
    check which names it patches.
18. **The call-time import must not shadow the function's own names.**
    SYRD-308's first cut inserted `from scripts import team_launcher as
    launcher` into `cutover_role_identities_command`, which has a
    `launcher` parameter (an injected session starter). The parameter was
    shadowed, and the restart called the module. The AST proof held,
    because it strips exactly that import; two suites caught it. The
    extractor now uses the alias `team_launcher` for a function that binds
    `launcher`, and refuses when neither is free. The proof normalises aliases
    per function, and its step 6 fails on any call-time import that shadows a
    parameter, local, inner name or other import. No earlier slice was
    affected (every extracted module scanned).
19. **A tmux stub must allow fixture sockets, not only `-L`/`-S`.** Refusing
    every socketless tmux call also refused suites that isolate their server
    with a fixture `TMUX_TMPDIR`. That left `viewer_builds_all_six_panes` at
    1/7 on both trees in SYRD-309's first run, hiding the evidence that
    mattered. The stub now passes a socketless call only when `TMUX_TMPDIR`
    names a non-default directory and any `$TMUX` points inside it (a `$TMUX`
    elsewhere would win), and refuses everything else. Under it the viewer
    suites run fully (7 and 298 checks). One baseline suite,
    `merge_gate_helper_test`, still makes a socketless `tmux list-panes -a`
    with no fixture; without the stub that would query the live server.
20. **A constant patched by rebinding stays where it is rebound.**
    `DEFAULT_SESSION_DIR` and `DEFAULT_PANE_STATE_DIR` are patched by
    assigning `team_launcher.<name>`, and several extracted modules read them
    through the launcher. SYRD-311 moved their resolvers but kept both
    constants, and `LIVE_PGU_STATE_DIR_NAME`, which builds the first, in the
    launcher. The resolvers read them as `launcher.<name>` at call time, as
    the baseline read its own globals. Moving a rebound constant into a module
    whose functions read it bare would split the rebinding. The boundary test
    rebinds each default and asserts that the moved resolvers see it.
21. **A reader guard checks every reference, not one.** "Reads it through
    the launcher" must mean that *every* reference to the name in a reader
    is `launcher.<name>`. SYRD-314's first guard asked for one such reference
    and no bare name, and a mutant that reached the builder through the
    module object at one of two sites survived it. The guard now collects
    every reference and requires all of them to go through the launcher.
22. **Measure on the tree you name, and count rebinding, not mentions.**
    The closure tool reads a cached call graph; SYRD-314's 12a figures were
    taken from a graph of `4296bed` and called "measured on this candidate".
    They were unchanged, but only by luck: regenerate the graph on the tree
    whose name goes next to the number. Likewise a regex patch count also
    counts names quoted in guards (12a's "6x/2x/1x" was 2 rebinding suites,
    and none for the other two); report rebinding assignments separately.
23. **A mutation kill reports its own last error line.** The runner used to
    print only four exception types, so a kill by any other one showed an
    empty reason (SYRD-316). It now prints the last line of the traceback
    whatever it is, and each kill's line is read: a mutant killed by the
    fixture crashing somewhere unrelated has not been killed by the property
    it was meant to test.
24. **A seam is any rebound name that moved code calls, whoever rebinds it.**
    Rebinding is counted in every form -- `.X =`, `setattr`, `patch.object`
    and `patch("...X")` -- and in every test, our own boundary tests
    included. In SYRD-318 three of the four seams (`_gui_launch_prefix`,
    `gui_program_path`, `_make_konsole_log_readable`) are rebound only by
    SYRD-317's boundary test; the moved callers still reach them through the
    launcher, as the baseline's did.

## 3. Sequenced plan

Each child is one coherent extraction, audited and integrated before the next
starts. The sizes are closure sizes at the baseline. Every child re-measures.

| # | Child | Approx. lines | Coupling at baseline |
|---|---|---:|---|
| 1 | **Worker pool** declaration, preflight and `worker-pool` verb (SYRD-286) | 680 | 3 inbound edges, 0 patched names |
| 2 | **Agent credentials**: agy credential source and role credential seeding (`agy-credential`, `seed-role-credentials`) (SYRD-287) | 1,180 moved | 17 inbound (mostly `switchyard_new_command`); 2 patched names found on re-measure |
| 3 | **Upstream report link and credential** (SYRD-288) | 296 moved | 2 inbound (`upgrade_project_command`); 2 patched names |
| 4 | **Onboarding docs, director onboarding and generated board skill** (SYRD-289) | 636 moved | 9 inbound; 3 patched names, all at call sites that stay in the launcher |
| 5 | **Role CLI command construction and model validation** (SYRD-290) | 212 + 694 in two modules | 5 + 11 inbound; 0 patched names; `validate-models` verb kept in the launcher (rule 9) |
| 6 | **Project worktrees and control repository** (SYRD-291); widened the git ownership lint | 742 | 6 inbound; `_control_repository_owner_home` patched (routed through the launcher) |
| 6a | **Launcher checkout self-update** (SYRD-292) | 632 | 7 inbound edges; `ensure_launcher_checkout_current` patched at launcher call sites; 12 builders linted |
| 6b | **Owner-correct git execution and project git helpers** (SYRD-293) | 323 | 10 inbound edges from the launcher, plus the three git modules through `launcher.run_owner_correct_git`; that name is patched on the launcher and kept there as the seam, including for owner_git's own helpers |
| 6c | **Board pane hooks and Codex hook trust** (SYRD-294) | 262 | Measured on SYRD-293's candidate: repository (pre-commit) hooks already live in `scripts/repository_hooks.py`, so there is little repository-hook glue left in the launcher. The tmux viewer-relayout hooks belong to presentation (§3 row 12). |
| 7 | Agent CLI discovery, promotion and first-run auth | 2,600 | several children |
| 7a | **Agent CLI discovery and host-wide promotion** (SYRD-295) | 435 + 1,099 in two modules | vendor install table and its three text formatters kept in the launcher for the no-execution guard (rule 9) |
| 7b | **First-run workdir trust and setup manifest** (SYRD-296) | 443 | `_workdir_is_trusted` patched on the launcher and kept there as the seam; the auth facilities stay in the launcher |
| 7c | First-run provider auth phase | ~1,900 | measured on SYRD-296's candidate; split as below |
| 7c-1 | **Provider auth-status probing** (SYRD-297) | 223 | four patched names, including a table patched by rebinding; every caller, including the module's own functions, reads them through the launcher |
| 7c-2a | **Pure provider screen classifiers** (SYRD-298) | 344 | a leaf: no Switchyard imports |
| 7c-2b | **Foreground first-run session runner and terminal ownership** (SYRD-299) | 1,022 (30 defs) | imports `provider_screen` directly; `_owner_command_env_args` and `_pane_identity_scrubbed_env` are read from the launcher at call time; the three shared constants moved with it (see the slice log) |
| 7c-3 | **First-run authentication phase and report** (SYRD-300) | 703 (13 defs) | 3 patched names moved and called through the launcher; the runner sentinel moved with them (rule 15); two source guards widened to read the new module; the install-table formatters and `_pane_identity_scrubbed_env` kept in the launcher |
| 8 | Board service, listener and status | 1,100 | split as below |
| 8a | **`status` and `release-status` verbs** (SYRD-301) | 800 (21 defs) | 1 patched name (`switchyard_status_command`), dispatched by `switchyard_main` through the launcher's name; no source guard named the moved code |
| 8b | **Board and listener service control** (SYRD-302) | 557 (28 defs) | the 22-def closure minus `_uid_for_user` (kept: generic and patched), plus the owner-user-manager state family; 5 patched names, one by rebinding, read through the launcher |
| 9 | Workflow declaration, adopt/migrate/rebind verbs | 1,600 | re-measure |
| 9a | **Workflow adoption, migration and the root-vouched handoff** (SYRD-303) | 1,068 (19 defs) | 1 patched name (`install_handed_off_workflow`), called by finish-upgrade through the launcher's name; `read_board_workflow_state` (patched) stays and is read through the launcher; one bare-importing suite switched to the canonical module (rule 17) |
| 9b | **Workflow pane rebind and reconciliation** (SYRD-304) | 677 (13 defs) | `read_board_workflow_state`, `role_pane_declaration`, `role_runtime_binding` and `presentation_section_for_roles` kept as launcher seams; `workflow_pane_rebind_test` needed no change (rule 17 audit) |
| 10 | Provider state, runtime registration and role identities | 1,800 | re-measure |
| 10a | **Role-account migration** (SYRD-305) | 747 (16 defs) | 0 patched names; 21 launcher facilities read at call time; lifecycle callers stay |
| 10b | **Provider-state generation and runtime-registration reads** (SYRD-306) | 448 + 57 (16 + 2 defs) | `RUNTIME_REGISTRATION_TIMEOUT_SECONDS` and `RuntimeRegistrationWait` moved with the wait (def-time default, result type); the privilege drop went to the `account_drop` leaf (rule 6); `_read_json_object` and `FIRST_RUN_SETUP_CLIS` kept as seams; the patched `await_runtime_registration` is still called by the launcher's name |
| 10c | **Role-identity cutover** (SYRD-308) | 1,012 + 15 (17 + 1 defs) | the 10 named seams stay; `DEFAULT_TENANT_RELEASE_DEPLOY_REF` went to the `release_refs` leaf (rule 6); the cutover command's own `launcher` parameter needed the rule-18 alias |
| 11 | tmux panes and sessions | 2,100 | heavily patched; later |
| 11a | **tmux viewer argv builders, layout, re-layout hooks and launch** (SYRD-309) | 404 (31 defs) | 0 patched; 7 names read through the launcher by `presentation_controller` and `switchyard-viewer-layout`; the presentation-titles and viewer guards widened to the new module |
| 11b | **Session records and seeding** (SYRD-310) | 572 (28 defs) | `report_launch_session_records` (patched 6) still called by the launcher's name; `DEFAULT_SESSION_DIR` (patched 10) kept and read through the launcher; the record timeout/poll constants and `LaunchSessionRecordStatus` moved with it (rule 6); no guard needed widening |
| 11c | **Session and pane-state paths and the initial pane idle state** (SYRD-311) | 177 (11 defs) | `DEFAULT_SESSION_DIR`, `DEFAULT_PANE_STATE_DIR` and `LIVE_PGU_STATE_DIR_NAME` kept in the launcher (rebinding-patched shared seam, rule 20); the patched `account_session_dir`, `role_session_dir` and `seed_initial_pane_idle_state` moved and are reached through the launcher |
| 11d | **Role session start and stop** (SYRD-312) | 252 (7 defs) | the patched `_start_role_sessions_without_a_window` and `stop_role_sessions` still reached through the launcher by `stop_project` and the cutover; 5 names read by `role_command`, `tmux_viewer` and `role_identity_cutover`; the hermes `getsource` guard reads the moved function through the re-export |
| 11e | **Role pane entry and resume verification** (SYRD-313), split into three modules | 407 + 360 + 35 (9 + 29 + 2 defs) | the rebound resume timings and agy root stay in the launcher (rule 20); `DEFAULT_PANE_STATE_DIR` with `_env_first` went to a leaf because the four entry points take it as a default (rule 6); the three patched entry points still called by the launcher's name |
| 11f | **tmux session argv and live pane-command matching** (SYRD-314) | 152 (8 defs) | the two patched argv builders reached through the launcher by every reader; live matching asks the launcher for the pane pid and process tree at call time |
| 12a | **Presentation reconnect, attach check and hand-back** (SYRD-316) | 213 + 19 (6 + 4 defs) | the hand-back's `layout` default moved with the layout modes to a leaf (rule 6); `reconnect_presentation` rebound on the launcher by 2 suites and reached there by the cutover; hand-back reads the launcher's pane program, titles and schema at call time |
| 12b | **Desktop presentation hand-off**, split in two (SYRD-317) | 508 + 199 (17 + 3 defs) | the desktop half and root's crossing layout writer apart; the four rebound seams called from inside reached through the launcher, across both modules; 20 launcher names read at call time |
| 12c | **Konsole window launch and GUI session environment** (SYRD-318) | 389 (20 defs) | four rebound seams (`launch_konsole_window` and three the SYRD-317 boundary test rebinds) and `_env_first` reached through the launcher at call time; the root→desktop crossing unchanged |
| 12d | Presentation layout files (`materialize_layout`, `default_layout_output_path`, `desktop_presentation_layout_path`, `desktop_layout_destination_problem`, `ensure_layout_output_owner`, `desktop_state_dir` and closure) — **suggested next** | ~183 (10 defs; measured on SYRD-318's candidate) | `materialize_layout` rebound 2x and `default_layout_output_path` 3x; read by `presentation_controller`, `role_runtime`, `desktop_presentation`, `desktop_layout_writer` and `presentation_reconnect`; the closure pulls in `pane_split_title`, `role_display_name` and `inert_pane_command`, which other modules read. The desktop-account and layout-mode detection (`presentation_gui_user`, `detected_invoking_desktop`, `resolve_layout_mode`) is a further 5 defs / 106 lines, `detected_invoking_desktop` rebound 4x. `launch_project` still needs its own split |
| 12 | Desktop, presentation windows and display bridge | 4,400 | 46 inbound, 9 patched; several children |
| 13 | Privileged boundary, tenant control and repair | 2,700 | several children |
| 14 | Release selection, install and upgrade | 4,800 | several children |
| 15 | Provisioning (`new`, `register`, `teardown`, owner accounts) | 5,000 | several children |
| 16 | Config and registry core, then the remaining CLI dispatch | — | last; what remains is the thin entry module |

After the launcher, the parent's other files each get their own children:
`project_provision.py`, `notify_listener.py`, `presentation_controller.py`,
`app.py`, `server.py`, `write_client.py` and `ticket-board-service.sh`. The
generated front-end core follows its asset boundary.

## 4. Slice log

### SYRD-286: worker pool

**Moved** into `scripts/worker_pool_command.py` (777 lines), from two regions
of `team_launcher.py` that were 35,000 lines apart:

- lines 581–953:
  - the `WORKER_POOL_*` limits;
  - `WorkerPool`, `parse_worker_pool` and `WorkerPoolFinding`;
  - `worker_pool_member_role`, `worker_pool_preflight`, `_is_pool_member_role`
    and `_board_known_roles`;
  - `format_worker_pool_preflight`.
- lines 35696–36057:
  - `switchyard_worker_pool_command`;
  - its board read and apply helpers (`_worker_pool_document`,
    `_worker_pool_readiness`, `_read_board_snapshot`,
    `_apply_worker_pool_document`, `_read_board_workflow_document`);
  - `WORKER_POOL_ACTIONS` and `_build_switchyard_worker_pool_parser`.

**Boundaries:**
- **Into the new module.** `load_project_config` calls `parse_worker_pool`, and
  `switchyard_main` calls the parser and the verb. `ProjectConfig.worker_pool`
  is annotated `WorkerPool`. `team_launcher` imports those 12 names
  explicitly: the 3 edges above, plus every public name tests and scripts read
  as `team_launcher.<name>`.
- **Out to the launcher, at call time.** The moved code reads
  `current_user_name`, `_owner_home_for_auth`, `_cli_auth_status`,
  `_owner_cli_is_installed`, `_missing_cli_install_clause`,
  `_workdir_is_trusted`, `_role_cli_name`, `_resolve_switchyard_project`,
  `load_project_config` and `MAX_VISIBLE_PANES_PER_WINDOW`. `ProjectConfig`
  and `RoleConfig` are imported only for type checking.

**Navigation measurement.** The task: find and read everything that implements
the `worker-pool` preflight and verb, for example to change one finding's
wording. The counts come from the files; no timing is claimed, and no claim is
made about tokens.

| | Baseline | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 38,527 lines) | 1 (`worker_pool_command.py`, 777 lines) |
| Where it sits in that file | two regions, lines 581–953 and 35696–36057 | contiguous, the whole file |
| `grep -ci pool` over the file a reader opens | 166 hits in 38,527 lines | 153 hits in 777 lines |
| `grep -ci pool` left in `team_launcher.py` | 166 | 32 (import list, `ProjectConfig` field, verb table, dispatch) |
| `def`/`class` names with "pool" in `team_launcher.py` | 12 | 0 |

A reader who already knows both line ranges reads about 740 lines either way.
The gain is in finding them: one named file instead of two distant ranges in a
38,527-line module. Whether that shortens tickets is for the parent's final
measurement to show.

### SYRD-287: agent credential sourcing and role seeding

The domain was re-measured on `4af050e` before editing: 46 definitions in three
regions of `team_launcher.py`, at lines 436–453, 19632–19856 and 20357–21345.
It became two modules, each below the soft limit. Upstream-report credentials
stay in the launcher for their own child.

- **`scripts/role_credentials.py`** (832 lines) holds role seeding:
  - the credential artifact catalogue (`RoleCredentialArtifact`,
    `ROLE_CREDENTIAL_ARTIFACTS`, the Hermes owner dir and provider env keys,
    `hermes_credential_target`, `select_hermes_provider_env`);
  - state and manifest (`_credential_state`, `_role_credential_target`,
    `role_credential_manifest`);
  - the copy itself (`seed_role_credential` and its no-follow openers);
  - the `seed-role-credentials` verb;
  - the owner-safe primitives it stands on: `_openat_no_follow`,
    `_openat_no_follow_keep_parent`, `_copy_fd_contents`, `_read_fd_bytes`,
    `_write_all`, and the owner-traversal checks;
  - the agy token layout constants (`AGY_CREDENTIAL_DIR_NAME`,
    `AGY_CREDENTIAL_TOKEN_NAME`), which the artifact catalogue needs at import.
- **`scripts/agy_credential.py`** (484 lines) holds the agy credential source:
  - the root-owned host setting and its read/write;
  - resolution of a project's source (host default, override, opt-out);
  - validation of the source token by descriptor;
  - the owner's own token state (`AGY_CREDENTIAL_*`);
  - `_open_owner_credential_dir` and `_seed_agy_credential_for_owner`;
  - the `agy-credential` verb.

**Boundaries.**
- **Imports.** `agy_credential` imports five names from `role_credentials`, by
  name; `role_credentials` never imports `agy_credential`. Neither imports the
  launcher at top level.
- **Into the modules.** `team_launcher` imports 31 names explicitly. These are
  the launcher's own uses (`switchyard_new_command`,
  `provider_state_generation`, `repatriate_role_runtime_state`,
  `_resume_preflight_allows_attempt`, dispatch), plus every public moved name,
  plus the private names suites read. Fifteen private helpers that nothing
  outside reads are no longer launcher attributes.
- **Out to the launcher, at call time.** `role_credentials` reads
  `uid_for_user`, `_uid_for_user`, `home_dir_for_user`, `current_user_name`,
  `role_run_as_user`, `pending_identity_for`, `session_file_name`,
  `_command_name`, `_walk_no_follow` and `_group_ids_for_user`.
  `agy_credential` reads `_uid_for_user`, `current_user_name`,
  `default_gui_user`, `_is_valid_owner_user_name`, `_prompt_bool` and
  `_write_json_atomic`.
- **The one routing change.** `_require_owner_home_traversable` and
  `_require_owner_traversable` are defined in `role_credentials`, but both
  no-follow openers call them as `launcher.<name>`.
  `team_launcher_new_project_test` patches them on the launcher. Bypassing the
  launcher turns that suite red and the new boundary test's named check red.

**Security boundaries kept.** The code is AST-identical, so ownership
assignment, modes, O_NOFOLLOW openat walks and descriptor-based copies are the
same code. The effects were also compared. For each of the 10 cases in
`team_launcher_agy_credential_seeding_test` that are red on the baseline, both
sides were compared: every provisioning command attempted, the output, and the
seeded token's contents and mode. With worktree roots and a pkcheck pid
normalised, they are identical: 6 seed a 0600 token through fd-based chowns,
and 4 correctly seed nothing.

No git builder moved, so the ownership lint's scope is unchanged.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`4af050e`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 37,808 lines) | 2 (`agy_credential.py` 484, `role_credentials.py` 832) |
| Where it sits | three regions: 436–453, 19632–19856, 20357–21345 | each module contiguous |
| `grep -ci credential` in the file(s) a reader opens | 262 in 37,808 lines | 72 in 484 (agy), 67 in 832 (role) |
| `grep -ci credential` left in `team_launcher.py` | 262 | 162 (upstream report, first-run auth, import list, `new`) |
| credential/agy/hermes `def`/`class` names in `team_launcher.py` | 37 | 13 |

### SYRD-288: upstream report link and credential

Re-measured on `95c11f0` before editing: 9 definitions in one contiguous region
of `team_launcher.py`, lines 26806–27101, about 296 lines. There are 2 inbound
edges, both from `upgrade_project_command`.

- **`scripts/upstream_report.py`** (351 lines) holds:
  - `UPSTREAM_REPORT_CREDENTIAL_NAME` and `UPSTREAM_REPORT_TOKEN_KEY`;
  - `upstream_report_credential_path`, the tenant's owner-private copy of the
    report token;
  - `_board_env_report_token`;
  - `upstream_report_board`, which resolves the upstream board from the host
    registry;
  - `record_upstream_report_link`, which persists the link in the tenant
    config;
  - `refresh_upstream_report_credential`;
  - `_credential_is_private` and `_write_owner_private_file`, the owner-only
    write through an owned directory chain.
- **`scripts/host_accounts.py`** (23 lines) holds `home_dir_for_user`, unchanged.

**Boundaries.**
- **Into the modules.** `team_launcher` imports 9 names explicitly: the 8 moved
  names callers or suites read, and `home_dir_for_user`.
  `upstream_report_credential_test` patches
  `team_launcher.record_upstream_report_link` and
  `refresh_upstream_report_credential` and then drives the launcher's
  `upgrade_project_command`. That call site stayed in the launcher, so the
  patches still reach it. `_credential_is_private` is no longer a launcher
  attribute; nothing outside read it.
- **Out to the launcher, at call time.** `uid_for_user`, `_load_json`,
  `_write_json_atomic`, `_registry_project_entries` and
  `_open_owned_directory_chain`.
- **The one boundary change.** Five moved functions take
  `home_for_user=home_dir_for_user` as a default argument. A default is
  evaluated when the `def` runs, and the suite asserts it `is
  team_launcher.home_dir_for_user`. So `home_dir_for_user`, an 8-line pure
  `pwd` lookup, moved into the leaf `host_accounts`. The launcher and
  `upstream_report` both import it from there, so it is one object, and
  patches on `team_launcher.home_dir_for_user` still reach every launcher
  caller. The defaults bind exactly as before: once, to the real function.

**Evidence.**
- The AST proof holds for both modules, including the stay-behind bound-name
  check. It fails on a planted lost comment, a changed default, and the missing
  `replace` import.
- The new `tests/upstream_report_boundary_test.py` has 8 checks, and 6 of 6
  mutations are killed.
- `upstream_report_credential_test` (54 checks), the home-patching suites and
  the previous slices' boundary tests are green and identical to the baseline.
- The pre-existing reds are identical per case.
- CLI help output is byte-identical to the baseline.
- A staged release contains and loads both modules, with the default identity
  intact.

No git builder moved.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`95c11f0`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 36,670 lines) | 1 (`upstream_report.py`, 351 lines) |
| Where it sits | lines 26806–27101 | the whole file |
| `grep -ciE 'upstream.?report'` in the file a reader opens | 78 in 36,670 lines | 39 in 351 lines |
| `grep -ciE 'upstream.?report'` left in `team_launcher.py` | 78 | 54: the `upstream_report_url` config field, CLI flags, provisioning plumbing and the import list |
| `def` names with `upstream_report` in `team_launcher.py` | 4 | 0 |

### SYRD-289: onboarding documents, director onboarding and the board skill

Re-measured on `fd85a84` before editing: 24 definitions, about 636 lines, in
five regions of `team_launcher.py`: 219–238, 7188–7258, 15575–16038,
27831–27883 and 28550–28625. They moved into one module,
**`scripts/project_onboarding.py`** (753 lines):
- **Onboarding documents:**
  - the `SWITCHYARD_*ONBOARDING*` names;
  - the director seed text and `seed_director_onboarding`;
  - writing, stamping and installing the docs;
  - source-commit provenance (`_switchyard_source_commit`, snapshot parsing,
    history and ancestry reads through `run_owner_correct_git`);
  - `upgrade_switchyard_onboarding_docs`.
- **Director onboarding:** `director_onboarding_state` and
  `migrate_declarative_director_onboarding`.
- **The generated project board skill:** `BOARD_SKILL_NAME`, the installer
  path, the install args and `ensure_generated_project_board_skill`.

**Not moved.** The `role-prompt` verb is an inline block of `switchyard_main`
that forwards to `scripts/workflow_manage.py`, where the role-prompt logic
already lives. Extracting it would rewrite `switchyard_main` rather than move
definitions. The `board-skill` verb is already `scripts/board_skill_cli.py`.
Model and runtime selection is the next slice.

**Boundaries.**
- **Into the module.** `team_launcher` imports 19 names explicitly: its
  callers' names in `new`, `upgrade`, `finish-upgrade` and launch, and every
  name the suites or `workflow_manage` read. Five private helpers nothing
  outside reads are no longer launcher attributes. The suites patch
  `_install_switchyard_onboarding_docs`, `director_onboarding_state` and
  `migrate_declarative_director_onboarding` around launcher call sites that
  stayed, and no moved function calls a patched moved name.
- **Out to the launcher, at call time.** 12 names: `run_owner_correct_git`,
  `_chown_project_file`, `_open_board_url`, `current_user_name`,
  `home_dir_for_user` (rule 8), `_load_json`, `_switchyard_dir`,
  `_proc_failure_reason`, `_read_switchyard_release_marker`,
  `shared_switchyard_release_for_path`, `_is_generated_project_layout_template`
  and `director_phase_required`. `ProjectConfig` and `DeclaredWorkflowPresence`
  are annotation-only.
- **Workflow-config names.** `DIRECTOR_ONBOARDING_MIGRATION` and
  `DIRECTOR_ROLE` come from `scripts.ticket_board.workflow_config`, imported by
  the module. The launcher's own import of them is unchanged.
- No default or decorator names a launcher name. No `git_*_args` builder moved,
  so the lint scope is unchanged.

**Evidence.**
- The AST proof holds, including the bound-name check. It fails if the
  `home_dir_for_user` call is left unrewritten.
- The new `tests/project_onboarding_boundary_test.py` has 7 checks, and 4 of 4
  mutations are killed. One of them binds the home lookup from the leaf module
  instead of the launcher.
- Green and identical to the baseline: the board skill and staged bundle,
  role onboarding prompt, role-prompt command integration, director
  verification policy, the finish-upgrade suites, legacy workflow migration,
  the release phase journal, desktop policy generation, single-owner staged
  tooling, installed-release deploy, workflow migration, the skill reminder,
  cutover, and the three earlier boundary tests.
- Pre-existing reds are identical per case or by whole log:
  `team_launcher_onboarding_git`, `desktop_access`, `claude_permission_hook`
  and `team_launcher_declarative_workflow` (`unshare --map-auto`).
- CLI help output is byte-identical to the baseline for 9 invocations.
- A staged release contains and loads the module, and its own `board-skill`
  and `role-prompt` help exit 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`fd85a84`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 36,379 lines) | 1 (`project_onboarding.py`, 753 lines) |
| Where it sits | five regions, from line 219 to line 28625 | the whole file |
| `grep -ci onboarding` in the file a reader opens | 154 in 36,379 lines | 94 in 753 lines |
| `grep -ci onboarding` left in `team_launcher.py` | 154 | 83: callers in `new`/`upgrade`/`finish-upgrade`/launch, the inline `role-prompt` dispatch, prompt limits and the import list |
| onboarding/board-skill `def`/`class` names in `team_launcher.py` | 16 | 0 |

### SYRD-290: role CLI command construction and model validation

Re-measured on `3e7337b` before editing. The ~630 estimate covered two
responsibilities, so they became two modules with a one-way import:

- **`scripts/role_command.py`** (212 lines, 13 definitions) builds a role's CLI
  command:
  - the per-CLI adapter tables: `YOLO_ARGS_BY_CLI`, `STARTUP_ARGS_BY_CLI`,
    `EFFORT_STYLE_BY_CLI`, `DEFAULT_MODEL_ARG_BY_CLI` and
    `DEFAULT_RESUME_{MODE,FLAG,SUBCOMMAND}_BY_CLI`;
  - `yolo_args_for_role`, `startup_args_for_role` and `effort_args_for_role`
    (Codex's `-c model_reasoning_effort="<level>"`);
  - `_resume_args_for_role`, `hermes_env_for_role` and `cli_command_for_role`.

  Inbound: `tmux_new_session_args` and `_role_from_json`.
- **`scripts/model_validation.py`** (694 lines, 24 definitions) checks a
  role's model:
  - the probe constants and prompt;
  - `ModelValidationFailure`, `ModelProbeAttempt` and `_ModelProbeWorkspace`;
  - the probe command, evidence and suggestion helpers, `_probe_role_model` and
    `validate_role_models`;
  - the unknown-model report, confirm and record steps and the stop gate;
  - the interactive model and effort fields.

  Inbound: first-run auth, `new`, `switchyard_main` and the role-runtime
  prompts. It imports `YOLO_ARGS_BY_CLI` from `role_command`.

**Not moved.**
- `switchyard_validate_models_command` stays in the launcher. It is the one
  call site allowed to pass `validate_models=True`, and
  `launch_without_model_probes_test` checks that in `team_launcher.py`'s text
  (rule 9). The first cut moved it, and that suite went red only on the
  candidate. The cut was redone rather than the guard edited.
- `_command_name`, which has callers across the launcher, stays as a shared
  facility.
- CLI discovery, promotion, auth and running the probes stay in the launcher.

**Boundaries.**
- **Into the modules.** `team_launcher` imports 32 names explicitly: 12 from
  `role_command` and 20 from `model_validation`. No moved name is patched by
  any suite. Five private helpers nobody outside reads are no longer launcher
  attributes.
- **Out to the launcher, at call time.**
  - `role_command`'s reads include `_command_name`, `role_runtime_binding`,
    `session_id_for_role`, `hermes_home_for_role`, `_uses_hermes`,
    `_uses_fresh_session_per_ticket`, `default_user_bin_dirs`,
    `_prepend_paths` and `_env_unset_prefix`.
  - `model_validation`'s reads include `_role_cli_name`, `_run_owner_cli_probe`,
    `_owner_catalog_args`, `_proc_failure_reason`,
    `OWNER_CLI_PROBE_TIMEOUT_SECONDS`, `load_project_config`,
    `_write_json_atomic` and `ensure_owner_file`.
- **Library imports.** `runtime_catalog`, `terminal_select` and the
  prompt-schema names are imported directly. They are shared module objects,
  and none is patched on the launcher.

**Evidence.**
- The AST proof holds for 37 definitions, including the bound-name check.
- A **192-case argv matrix** of `cli_command_for_role`, `yolo_args_for_role`,
  `startup_args_for_role`, `effort_args_for_role` and `hermes_env_for_role` is
  **byte-identical** to the baseline. It covers:
  - 4 runtimes;
  - 3 effort levels;
  - yolo on and off;
  - fresh and resumed sessions;
  - with and without extra arguments and a model.
- The new `tests/role_command_boundary_test.py` has 9 checks, and 6 of 6
  mutations are killed.
- `codex_effort_config_key_test` gives 15 checks on both sides, including
  Codex's own header, offline.
- The model-probe, first-run, catalog, selector, pane, tmux, clone-hook,
  adoption, Hermes, process-authority and role-runtime suites, and the four
  earlier boundary tests, are green and identical to the baseline.
- Pre-existing reds are identical per case: `team_launcher_pane_paths` 9/2,
  `team_launcher_project_artifacts` 10/6, `team_launcher_env_config` 24/2. The
  two env-config failures are both command-construction cases. The command
  each builds and the assertion each fails at are byte-identical on both
  sides; they assume a real home and a real owner's PATH.
- CLI help output is byte-identical to the baseline for 7 invocations.
  `validate-models --help` treating `--help` as a project is existing
  behaviour, the same on both sides.
- A staged release contains and loads both modules.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`3e7337b`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 35,730 lines) | 2 (`role_command.py` 212, `model_validation.py` 694) |
| Where it sits | regions from line 439 to 18786 | each module contiguous |
| model/effort/yolo/startup/cli-command `def`/`class` names in `team_launcher.py` | 27 | 5 (live-model inspection and the `validate-models` verb) |
| `grep -ci effort` in the launcher / the new modules | 59 / — | 44 / 15 + 6 |
| `grep -ciE 'model.?probe\|model.?validation'` in the launcher / the new modules | 50 / — | 27 / 38 |

### SYRD-291: project worktrees and the control repository

Re-measured on `9c804c7` before editing. Everything named git, worktree, hook,
repo, clone or commit forms a closure of 146 definitions and about 3,044 lines,
far more than the ~1,200 estimate. So this child took one coherent portion and
proposes 6a–6c (§3) for the rest.

**Moved** into **`scripts/project_worktrees.py`** (742 lines): 41 definitions
from line 672 and lines 3914–5212.
- **Git argv builders:** 16 of them for the control repository (clone, remote
  rename, fetch refspec, fetch ref, worktree add), role worktrees (check,
  status, reset, clean and dry run) and the shared checkout (check, checkout,
  status, clean and dry run), plus `git_fetch_worktree_ref_args`,
  `control_repository_refspec` and `mkdir_p_args`.
- **Refresh warnings:** `_config_git_owner_rules`, and the dirty-tree
  warnings and their parsers.
- **The control repository:** `CONTROL_REPOSITORY_*`,
  `control_repository_state`, `ensure_control_repository`,
  `chown_control_repository_args`, and ownership repair
  (`_control_repository_*`, `repair_control_repository_ownership`).
- **Worktrees:** `ensure_control_role_worktrees`, `ensure_project_worktrees`,
  `fetch_project_worktree_ref` and `WorktreeProvisionResult`.

**Stayed.**
- The owner-correct chokepoint (`run_owner_correct_git`, `GitOwnerRule`).
- Launcher-checkout self-update and its `git_launcher_*` builders.
- `worktree_ref`, which has 16 callers.
- The worktree helpers inside other domains.
- `_path_owner_label` and `_path_owner_ids`. They are generic path-owner
  helpers, siblings of `_path_owner_user`, and are patched in
  `team_launcher_control_repo_test`.

**Boundaries.**
- **Into the module.** `team_launcher` imports 36 names explicitly.
  `ensure_project_worktrees` (patched in 2 suites) is called only from
  launcher sites that stayed.
- **The one routing change.** `_control_repository_owner_home` moved, but four
  suites patch it on the launcher, so its two moved callers call
  `launcher._control_repository_owner_home` (rule 12).
- **Out to the launcher, at call time.** `run_owner_correct_git`,
  `GitOwnerRule`, `worktree_ref`, `current_user_name`, `role_run_as_user`,
  `_normalized_path`, `_proc_failure_reason`,
  `_control_repository_owned_roots`, `_path_owner_label` and `_path_owner_ids`.

**Git ownership lint, widened in the same commit.**
- `GIT_LINTED_MODULES` now includes `project_worktrees.py`.
- A new guard fails if any `scripts/` module defines a `git_*_args` builder
  outside that set.
- Mutation-checked. A builder run through `runner(...)` in the new module, and
  a builder's argv assigned before being run, are each reported *at
  `project_worktrees.py`*. Dropping the module from the set fails the guard.
- The launcher's two lint findings are the baseline's, compared by source
  text: the deploy-ref builders in an error message.
- In a normal run the suite stops at that baseline-red case, so the new guard
  was exercised case by case.

**Evidence.**
- The AST proof holds for 41 definitions, including the bound-name check.
- The new `tests/project_worktrees_boundary_test.py` has 8 checks, and 4 of 4
  mutations are killed.
- Green and identical to the baseline:
  - control-repository placeholder, pane commands, repository boundary
    repair, worktree inheritance and worktree cleanup;
  - repository hooks, install-all hooks and clone hooks;
  - first-run setup (492), cutover, first-run auth and Codex folder trust;
  - all five earlier boundary tests.
- Red on the baseline, compared case by case:
  - `team_launcher_control_repo` 7/3 and `team_launcher_add_role_vcs` 6/2. For
    every red case, each git command run through `run_owner_correct_git` (14,
    29, 15, 4 and 4 commands), its keyword arguments, and where the case
    fails are byte-identical to the baseline.
  - `team_launcher_pane_paths` 9/2.
- CLI help output is byte-identical to the baseline for 7 invocations.
- A staged release contains and loads the module, and the lint is clean on the
  release's own copy of it.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`9c804c7`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 34,987 lines) | 1 (`project_worktrees.py`, 742 lines) |
| Where it sits | line 672, and lines 3914–5212 interleaved with launcher-checkout code | the whole file |
| control-repository/worktree `def`/`class` names in `team_launcher.py` | 31 | 8 (callers in launch and first-run, and `worktree_ref`) |
| `git_*_args` builders in `team_launcher.py` | 30 | 14 (launcher checkout and deploy ref) |
| `grep -c control_repository` in the launcher / the new module | 104 / — | 46 / 68 |

### SYRD-292 (slice 6a): launcher checkout self-update

Re-measured on `9ceed50` before editing: 27 definitions, about 516 lines, in
four regions (lines 619–620, 777, 3948–4561 and 22144). They moved into
**`scripts/launcher_checkout.py`** (632 lines):
- `ALLOW_STALE_LAUNCHER_ENV`, `LEGACY_ALLOW_STALE_LAUNCHER_ENV` and
  `LauncherCheckoutProbe`;
- the **12 launcher-checkout `git_*_args` builders**, with
  `_parse_ahead_behind`, `_format_behind_count` and `_short_head`;
- `probe_launcher_checkout`, `probe_checkout_against_worktree_ref` and
  `launcher_checkout_status`;
- `_auto_fast_forward_launcher_checkout`, `ensure_launcher_checkout_current`
  and `deploy_launcher_checkout`;
- `warn_if_artifact_source_checkout_is_stale`;
- `_launcher_checkout_runner`, and `_owner_correct_git_runner`, the runner
  adapter over the chokepoint whose only caller is `_launcher_checkout_runner`.

**Boundaries.**
- **Into the module.** `team_launcher` imports 22 names explicitly for its
  callers: `launch_project`, `main`, `upgrade_project_command`,
  `_runtime_checkout_copy_status` and `_format_checkout_probe_status`.
  `ensure_launcher_checkout_current` is patched in
  `team_launcher_presentation_test` around launcher call sites that stayed.
  Five private helpers nothing outside reads are no longer launcher
  attributes.
- **Out to the launcher, at call time.** The chokepoint
  (`run_owner_correct_git`, `GitOwnerRule`, `_path_owner_user`), `_repo_root`
  (patched in 3 suites), `worktree_ref`, `_env_truthy_any`,
  `_parse_ls_remote_head` (shared with deploy-ref resolution),
  `_proc_failure_reason` and `shared_switchyard_release_for_path`.
- **Unchanged and still in the launcher.** Host shared-release install and
  upgrade.

**Git ownership lint.**
- `launcher_checkout.py` joined `GIT_LINTED_MODULES`, and the module has no
  findings.
- Mutation-checked:
  - a launcher-checkout builder run through `runner(...)` is reported at
    `launcher_checkout.py:172`, as a third finding beside the launcher's two;
  - dropping the module from the set fails the all-builder-module guard.
- The launcher's two documented baseline findings (the deploy-ref builders)
  are still reported, not masked.

**Evidence.**
- The AST proof holds for 27 definitions, including the bound-name check.
- The new `tests/launcher_checkout_boundary_test.py` has 6 checks. It drives
  `probe_launcher_checkout` through a patched chokepoint and `_repo_root` with
  canned git answers, and 4 of 4 mutations are killed.
- Green and identical to the baseline: cutover, release phase journal,
  read-only status, privileged artifacts, tenant deploy identity, authority
  before deploy, the SYRD-87 deploy probe target, and the six boundary tests.
- Red on the baseline, compared case by case with a fresh `HOME` per run:
  - `team_launcher_freshness` 15/1. For the red case
    (`…uses_owner_runner_when_launcher_user_differs`), the 9 git commands
    through the chokepoint and the failing line are byte-identical to the
    baseline.
  - `team_launcher_resume_detached` 8/1, `team_launcher_presentation` 11/5,
    `team_launcher_pinned_release_resume` 14/1,
    `team_launcher_trusted_migration_artifact` 6/7 and
    `team_launcher_viewer` 21/5. The viewer suite differs only in the hashes
    of its temporary fixture commits.
- CLI help output is byte-identical to the baseline for 5 invocations. The
  `team-launcher` help, including the deploy and stale-launcher options, is
  among them.
- A staged release contains and loads the module, and the lint is clean on the
  release's own copy of it.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`9ceed50`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 34,365 lines) | 1 (`launcher_checkout.py`, 632 lines) |
| launcher-checkout `def`/`class` names in `team_launcher.py` | 20 | 0 |
| `git_*_args` builders in `team_launcher.py` | 14 | 2 (the deploy-ref pair) |

### SYRD-293 (slice 6b): owner-correct git execution and project git helpers

Re-measured on `6f0ccb7` before editing. **`scripts/owner_git.py`** (323
lines) received 13 definitions:
- **Owner-correct execution:**
  - `GitOwnerRule`;
  - `_path_owner_user` and `_path_is_under`;
  - `_git_target_path_from_args`, `_git_owner_for_target` and
    `_git_owner_failure`;
  - **`run_owner_correct_git`**.
- **Project git helpers:** `_run_owner_git`, `_git_status_porcelain`,
  `_ensure_project_git_repository`, `_require_existing_project_git_repository`,
  `_commit_project_git_changes` and `_owner_project_git_runner`.

**Stayed.**
- `_normalized_path`, defined twice (rule 12).
- `_project_config_path_owner_user`, which is about who owns a config file, not
  git execution.
- The deploy-ref builders and their two baseline lint findings.
- `_proc_failure_reason`.

**Boundaries.**
- **The chokepoint seam stays on the launcher.** Suites patch
  `team_launcher.run_owner_correct_git`. `project_worktrees`,
  `launcher_checkout` and `project_onboarding` already call
  `launcher.run_owner_correct_git`, and owner_git's own helpers
  (`_git_status_porcelain`, `_owner_project_git_runner`, `_run_owner_git`) do
  the same. None of them binds the new function directly.
- **The chokepoint's own dependencies stay on the launcher too.**
  `current_user_name` (patched in 27 suites), which decides whether git runs
  directly or through `sudo -u <owner>`, is read from the launcher when it
  runs, as are `_proc_failure_reason` and `_normalized_path`.
- **Into the module.** `team_launcher` imports all 13 names explicitly.
  `_require_existing_project_git_repository`, `_commit_project_git_changes`
  and `_ensure_project_git_repository` are patched only around
  `switchyard_new_command`, which stayed.
- **Lint.** `owner_git.py` joined `GIT_LINTED_MODULES`. It defines no builders,
  but the chokepoint's call sites now live there. A builder run through
  `runner(...)` there is reported at `owner_git.py:120`.

**Evidence.**
- The AST proof holds for 13 definitions, including the bound-name check. The
  launcher's `_normalized_path` still returns a `Path`.
- The new `tests/owner_git_boundary_test.py` has 9 checks, and 6 of 6
  mutations are killed by named checks. The mutations:
  - a top-level launcher import;
  - a helper binding the chokepoint locally;
  - the chokepoint deciding who is running itself;
  - the later `_normalized_path` being lost;
  - an export dropped;
  - a builder bypass (caught by the lint).
- Green and identical to the baseline:
  - cross-account presentation (SYRD-66), desktop policy generation, cutover
    and finish-upgrade;
  - installed-release deploy, tenant deploy identity, the SYRD-87 deploy probe
    target, release bootstrap rollback and authority before deploy;
  - the seven earlier boundary tests.
- Red on the baseline, compared case by case with a fresh `HOME` per run:
  - `team_launcher_freshness` 15/1: its red owner-runner case sends the same 9
    git commands through the chokepoint and fails at the same line.
  - `team_launcher_onboarding_git` 7/3: the same 9, 0 and 0 git commands and
    the same failing lines.
  - `team_launcher_new_project` 18/1 and `desktop_access` 3/1.
  - The git lint is 9/1 on both sides.
- CLI help output is byte-identical to the baseline for 6 invocations.
- A staged release contains and loads the module. `run_owner_correct_git` and
  `GitOwnerRule` are the same objects through the launcher, and the lint is
  clean on the release's own copy.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`6f0ccb7`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 33,826 lines) | 1 (`owner_git.py`, 323 lines) |
| owner-git `def`/`class` names in `team_launcher.py` | 13 | 0 |
| Where it sits | three regions: lines ~3979–4054, ~10049 and ~14243–14421 | the whole file |

### SYRD-294 (slice 6c): pane hooks and Codex hook trust

Re-measured on `37bbb53` before editing: 9 definitions, about 190 lines, in four
regions of `team_launcher.py`. They moved into one module,
**`scripts/pane_hooks.py`** (262 lines), because both parts concern the hooks
that run in a role's pane:
- **Board pane hooks:** `install_generated_project_pane_hooks_args`,
  `ensure_generated_project_pane_hooks`, `tenant_hook_accounts` and
  `refresh_role_pane_hooks`.
- **Codex hook trust:** `CodexHookTrustMismatch`,
  `stale_codex_hook_trust_for_roles`, `_codex_hook_trust_reason`,
  `_codex_hook_trust_affected_roles` and `_format_codex_hook_trust_report`.

**Stayed.**
- The viewer-relayout tmux hooks, which are presentation.
- Repository hooks, which are `scripts/repository_hooks.py`.
- `scripts/ticket_board/codex_hook_trust.py` itself, which does the board-side
  hashing and trust reading.

**Boundaries.**
- **Into the module.** `team_launcher` imports the 7 names its callers and the
  suites read, explicitly; its callers are launch, upgrade and first-run setup.
  `workflow_launcher.py` reaches `launcher.ensure_generated_project_pane_hooks`.
- **Patched names.** `ensure_generated_project_pane_hooks` (2 suites) and
  `refresh_role_pane_hooks` (1 suite) are patched only around launcher call
  sites that stayed, and no moved function calls a patched moved name.
- **Out to the launcher, at call time.** `uid_for_user`, `runtime_dir_for_uid`,
  `current_user_name`, `home_dir_for_user`, `role_run_as_user`,
  `_staged_tooling_dir`, `_is_generated_project_layout_template`,
  `_proc_failure_reason`, `_role_cli_name` and `_role_names`.
- **Library imports.** `codex_command_hook_trust_entries` and
  `codex_trusted_hashes` are imported directly from `codex_hook_trust` under
  the launcher's own `_`-aliases, so the moved text is unchanged. Neither is
  patched.

**Evidence.**
- The AST proof holds for 9 definitions, including the bound-name check. No
  name involved is defined twice.
- The new `tests/pane_hooks_boundary_test.py` has 7 checks. It builds the
  hook-installer argv from the launcher's patched home, uid, runtime dir and
  user, both with `sudo` for another user and without it for the owner, and
  drives the trust check through the launcher's patched CLI lookup. 5 of 5
  mutations are killed. The one that first survived, the installer deciding
  who is running by itself, showed the test lacked the owner case; that case
  was added.
- Green and identical to the baseline:
  - pane hook install, generated-layout upgrade, first-run auth, missing-CLI
    install hint and first-run setup (492);
  - Codex folder trust (53), process authority, project provision and
    cutover;
  - the eight earlier boundary tests.
- Red on the baseline, compared case by case:
  - `team_launcher_layout_upgrade_commands` 12/8 (its own cases write pane
    state, which trips the runner's live-state guard);
  - `team_launcher_env_config` 24/2, `team_launcher_new_project` 18/1 and
    `claude_permission_hook` 22/1.
- **Not counted as coverage:**
  - `team_launcher_declarative_workflow` is identical by whole log, but it
    stops at `unshare --map-auto` on both sides, before it reaches its patched
    `ensure_generated_project_pane_hooks`.
  - `claude_permission_hook`'s red case needs a real Claude, which the suite
    stubs refuse.
  - `ticket_board_hermes_hook_events` skips on both sides, because Hermes is
    not installed.
- CLI help output is byte-identical to the baseline for 5 invocations.
- A staged release contains and loads the module.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`37bbb53`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 33,573 lines) | 1 (`pane_hooks.py`, 262 lines) |
| pane-hook/hook-trust `def`/`class` names in `team_launcher.py` | 9 | 0 |
| Where it sits | four regions, lines ~5857 to ~24260 | the whole file |

### SYRD-295 (slice 7a): agent CLI discovery and host-wide promotion

Re-measured on `501ca76` before editing: 52 definitions, about 1,280 lines, in
one main region (12474–13832) plus 17580–17640. That is above the soft limit,
so it became two modules with a one-way import:

- **`scripts/agent_cli_discovery.py`** (435 lines, 21 definitions) holds:
  - the `AGENT_CLI_SCOPE_*` and `CALLER_*` limits;
  - `InvokingAccount` and `invoking_account`, and the account execute-bit
    checks;
  - the caller's PATH from its process tree, `caller_command_search_path`,
    `caller_executable` and `caller_aware_which`;
  - `AgentCliAvailability`, `agent_cli_binary`, `classify_agent_cli`,
    `classify_selected_agent_clis` and `agent_cli_scope_explanation`;
  - `AgentCliUnavailable`.
- **`scripts/agent_cli_promotion.py`** (1,099 lines, 31 definitions) holds:
  - the policies and `_parse_agent_cli_sources`;
  - source validation (`AgentCliSourceRejected`, reachability by strangers,
    unreachable dependencies, detected-path problems, self-contained,
    `resolve_agent_cli_source`);
  - root promotion through the promoter with its rollout journal, and the
    version check in the tenant's context;
  - the offer before launch;
  - require and owner verification for `new`;
  - refreshing the registered CLIs.

  It imports 9 names from discovery. `AgentCliSourceRejected` subclasses
  `AgentCliUnavailable` at import, and discovery never imports promotion.

**Stayed, and why.**
- **The vendor install table and its text.** `AGENT_CLI_INSTALL_COMMANDS`,
  `host_wide_install_instruction`, `_missing_cli_install_clause` and
  `_format_missing_cli_launch_failure` stay in the launcher, because
  `team_launcher_missing_cli_install_hint_test` is a security guard that scans
  `team_launcher.py` (rule 9). Only those formatters may read the table, and
  none may mention anything that executes. Moving
  `host_wide_install_instruction` would have put a table reader outside the
  guard's view. Promotion calls it through the launcher, and the new boundary
  test fails if any other module ever names the table.
- **`PROC_ROOT`,** patched in 3 suites and shared with `process_uid`. The moved
  process-tree walk reads it from the launcher at call time.
- **`recorded_install_command` and `INSTALL_ROLLOUT_LABEL`,** which belong to
  release bootstrap.
- **First-run auth,** left for 7b and 7c.

**Boundaries.**
- **Into the modules.** `team_launcher` imports 42 names explicitly: 18 from
  discovery and 24 from promotion. No moved name is patched by any suite.
  Ten private helpers nothing outside reads are no longer launcher
  attributes.
- **Out to the launcher, at call time.** `PROC_ROOT`,
  `FIRST_RUN_AUTH_STATUS_COMMANDS`, `DEFAULT_PANE_BASE_PATH`,
  `_run_owner_cli_probe`, `current_user_name`, `_repo_root`, `_role_cli_name`,
  `switchyard_registry_dir`, `switchyard_shared_install_root`,
  `host_wide_install_instruction`, and the launcher-imported
  `untrusted_root_executable_reasons` and `TENANT_CONTROL_OWNER_UID`
  (rule 8).
- No default or decorator names a launcher name, and no member is defined
  twice.

**Evidence.**
- The AST proof holds for 52 definitions, including the bound-name check.
- The new `tests/agent_cli_boundary_test.py` has 12 checks, and 7 of 7
  mutations are killed by named checks, including a second reader of the
  install table planted in promotion. It drives:
  - the caller-PATH walk through a fake `/proc` behind the launcher's patched
    `PROC_ROOT`;
  - `agent_cli_binary` through the patched probe table;
  - the promoter path through the patched shared install root and checkout.
- Green and identical to the baseline:
  - `agent_cli_host_wide`;
  - `agent_cli_privileged_promotion` (92): the real promoter run across a user
    and mount namespace, with a fake `sudo` on PATH and nothing written to the
    host;
  - `caller_cli_discovery` (19) and `caller_cli_discovery_privileged` (3);
  - `tenant_launch_unused_cli` (31);
  - the install-command guard, registry, cutover and first-run setup (492);
  - the nine earlier boundary tests.
- Red on the baseline, compared case by case:
  - `tenant_resume_cli_promotion`, 1 case: identical normalised logs. The
    cause depends on this host: its installed
    `/opt/switchyard/current/scripts/switchyard-promote-agent-cli` is
    root-owned, so the crossing goes on to the test's injected runner, which
    is recording only.
  - `team_launcher_new_project` 18/1, `team_launcher_presentation` 11/5,
    `team_launcher_unprivileged_presentation` 7/4 and
    `team_launcher_desktop_layout_path` 8/4. The last differs only in the
    worktree path.
- CLI help output is byte-identical to the baseline for 6 invocations,
  including `new --help` with its agent-CLI options.
- A staged release contains and loads both modules.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`501ca76`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 33,377 lines) | 2 (`agent_cli_discovery.py` 435, `agent_cli_promotion.py` 1,099) |
| agent-CLI discovery/promotion `def`/`class` names in `team_launcher.py` | 51 by name pattern, of which 13 were unrelated (role-account and provider-account helpers the pattern also matches) | 0; the 13 unrelated ones remain, and the 3 install-text formatters were kept on purpose |
| `grep -c agent_cli` in the launcher / the new modules | 68 / — | 35 / 8 + 54 |

### SYRD-296 (slice 7b): first-run workdir trust and setup manifest

Re-measured on `fede447` before editing: 20 definitions, 351 lines, in five
regions of `team_launcher.py` (12421–12511, 12592, 12947–12958, 14482–14829).
They moved into **`scripts/first_run_setup.py`** (443 lines):
- **The manifest:**
  - the step types (`FirstRunAuthLoginStep`, `FirstRunProviderSetupStep`,
    `FirstRunFolderTrustStep`) and `FirstRunSetupManifest`;
  - `build_first_run_setup_manifest`, `_format_first_run_setup_manifest` and
    `print_first_run_setup_manifest`;
  - `_provider_setup_reason`, `_owner_shell_issue`, `_roles_by_first_cli` and
    `_role_names`.
- **Workdir trust:**
  - `FIRST_RUN_TRUST_CLIS` and `_workdir_is_trusted`;
  - the Claude, agy and Codex probes;
  - the trust-path candidates, `_git_common_dir_for` and
    `_first_run_trust_command`.

**Stayed.** The auth phase stays in the launcher for 7c:
`run_first_run_auth_phase`, `FirstRunAuthReport`, `_cli_auth_status`,
`FIRST_RUN_AUTH_STATUS_COMMANDS`, `FIRST_RUN_AUTH_LOGIN_COMMANDS`,
`FIRST_RUN_SETUP_CLIS`, `_provider_account_setup_complete`,
`_owner_user_cli_reminder`, `_read_json_object`, `OwnerShellIssue`, and the
guarded install-table formatter `_missing_cli_install_clause` (rule 9).

**Boundaries.**
- **Into the module.** `team_launcher` imports 11 names explicitly.
  `role_runtime.py` reaches `launcher._workdir_is_trusted` and
  `FIRST_RUN_TRUST_CLIS`, and `pane_hooks.py` reaches `launcher._role_names`.
  Nine private helpers nothing outside reads are no longer launcher
  attributes.
- **The one routing change.** `_workdir_is_trusted` moved, but
  `team_launcher_first_run_models_test` patches it on the launcher and asserts
  the probe is **not** called for detached roles. A direct call from the moved
  manifest would have made that assertion pass vacuously, so the manifest
  calls `launcher._workdir_is_trusted` (rule 13).
- **Out to the launcher, at call time.** All the auth facilities above, plus
  `_role_cli_name`, and `stale_codex_hook_trust_for_roles` and
  `_format_codex_hook_trust_report`, which the launcher imports from
  `pane_hooks` (rule 8).
- **Source guards checked.** The single-login order guard reads
  `run_first_run_auth_phase`, which stayed. No install-table reader moved.
- No default or decorator names a launcher name, and no member is defined
  twice.

**Evidence.**
- The AST proof holds for 20 definitions, including the bound-name check.
- The new `tests/first_run_setup_boundary_test.py` has 8 checks. It builds a
  real manifest whose login, setup and trust steps are decided by the
  launcher's patched `_cli_auth_status`, `_provider_account_setup_complete`,
  `stale_codex_hook_trust_for_roles` and `_workdir_is_trusted`. 5 of 5
  mutations are killed, including the manifest binding the trust probe
  locally, which the existing suite would not have caught.
- Green and identical to the baseline:
  - Codex folder trust (53), first-run setup (492), first-run models,
    single login (34), trust-step silence (32), login inheritance (82) and
    first-run auth;
  - role runtime, worker-pool preflight (47) and start/status (32);
  - the install-command guard and launch without model probes (22);
  - the ten earlier boundary tests.
- Red on the baseline, compared case by case:
  - `team_launcher_first_run_hermes` 5/1: the red case runs the auth phase,
    and the one manifest it builds, and the line it fails at, are identical
    to the baseline.
  - `team_launcher_env_config` 24/2, `team_launcher_new_project` 18/1 and
    `claude_permission_hook` 22/1.
- CLI help output is identical to the baseline for 5 invocations
  (`validate-models --help` exits 1 on both sides, as recorded in SYRD-290).
- A staged release contains and loads the module.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`fede447`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 32,030 lines) | 1 (`first_run_setup.py`, 443 lines) |
| manifest/trust `def`/`class` names in `team_launcher.py` (16 by name pattern) | 16 | 0 |
| Where it sits | five regions, lines 12421–14829 | the whole file |

### SYRD-297 (slice 7c-1): provider auth-status probing

Re-measured on `5beb317` before editing: 10 definitions, 147 lines, in four
regions (12437, 12800, 12903–12957, 14297–14411). They moved into
**`scripts/provider_auth_status.py`** (223 lines):
- `FIRST_RUN_AUTH_STATUS_COMMANDS`, `OWNER_CLI_PROBE_TIMEOUT_SECONDS` and
  `PROBE_TIMED_OUT_STATUS`;
- `_owner_home_for_auth` and `_run_owner_cli_probe`, which runs a probe as the
  owner, with a scrubbed pane identity and a timeout;
- `_owner_cli_is_installed`, `_cli_auth_probe_passed` and `_cli_auth_status`;
- `_claude_account_setup_complete` and `_provider_account_setup_complete`.

The interactive first run (7c-2) and the auth-phase orchestration and report
(7c-3) stayed.

**Boundaries.** Four members are patched on `team_launcher` by the suites:
- `FIRST_RUN_AUTH_STATUS_COMMANDS` (2 suites), patched by **rebinding** the
  table;
- `_owner_home_for_auth` (5);
- `_cli_auth_status` (2);
- `_provider_account_setup_complete` (1).

How their seams are kept:
- **All 10 names are re-exported explicitly.** Launcher callers read their own
  globals, and the modules moved earlier (`first_run_setup`,
  `agent_cli_discovery`, `agent_cli_promotion`, `model_validation`,
  `worker_pool`, `worker_pool_command`, `role_runtime`) keep calling
  `launcher.<name>`.
- **Inside the new module, the patched names are read through the launcher
  too.** `_cli_auth_status` and `_owner_cli_is_installed` read
  `launcher.FIRST_RUN_AUTH_STATUS_COMMANDS`, so a rebound table reaches them
  (rule 13).
- **Out to the launcher, at call time:** `_owner_command_env_args`,
  `_pane_identity_scrubbed_env` and `_read_json_object`.
- **Import time.** `_run_owner_cli_probe`'s `timeout_seconds` default is the
  module's own `OWNER_CLI_PROBE_TIMEOUT_SECONDS`, not a launcher name
  (rule 6). No member is defined twice.

**Evidence.**
- The AST proof holds for 10 definitions, including the bound-name check.
- The new `tests/provider_auth_status_boundary_test.py` has 8 checks. It
  rebinds the launcher's status table and shows that the moved
  `_cli_auth_status`:
  - runs exactly the rebound command in the owner's context, through the
    launcher's patched `_owner_command_env_args` and
    `_pane_identity_scrubbed_env` (cwd, env and timeout recorded);
  - treats a CLI missing from the rebound table as having no probe.

  5 of 5 mutations are killed, including the status function reading its own
  table instead of the launcher's.
- Green and identical to the baseline:
  - first-run auth, first-run setup (492), single login (34), login
    inheritance (82), trust-step silence (32), first-run models, Codex folder
    trust (53) and provider state written as owner (5);
  - worker-pool preflight (47) and start/status (32), and role runtime;
  - the model-probe tool-call evidence (29), owner model catalog (77), launch
    without model probes (22) and model tool-call probe suites;
  - agent CLI host-wide, desktop policy generation (11) and the install-command
    guard;
  - the eleven earlier boundary tests.
- Red on the baseline, compared case by case:
  - `team_launcher_first_run_hermes` 5/1: its red case makes the same 3 status
    probes through the launcher, each `unauthenticated`, and fails at the same
    line.
  - `team_launcher_env_config` 24/2, `team_launcher_new_project` 18/1 and
    `claude_permission_hook` 22/1.
  - `desktop_access` 3/1: its red case, the second launcher copy that patches
    `_owner_home_for_auth`, fails before reaching this code (rule 11), so it is
    not counted as coverage.
- CLI help output is identical to the baseline for 5 invocations.
- A staged release contains and loads the module.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`5beb317`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 31,654 lines) | 1 (`provider_auth_status.py`, 223 lines) |
| auth-status definitions in `team_launcher.py` | 10 | 0 |
| Where it sits | four regions, lines 12437–14411 | the whole file |

### SYRD-298 (slice 7c-2a): pure provider screen classifiers

Re-measured on `9aecc37` before editing. The interactive first run's closure is
51 definitions. Its pure text half, 21 definitions from lines 12921–13280,
moved into **`scripts/provider_screen.py`** (344 lines), a **leaf** that
imports nothing of Switchyard's:
- **Readable text:** `_ANSI_ESCAPE`, `_INVISIBLE_CONTROLS`, `_visible_text`,
  `_visible_lines` and `_draws_something`.
- **Whether a screen asks something:** `provider_is_waiting_for_an_answer` and
  its three independent readings:
  - the answer phrases (`PROVIDER_PENDING_ANSWER_MARKERS`);
  - the choice structure (`_provider_screen_offers_a_choice`,
    `PROVIDER_SELECTION_CURSORS`, `_NUMBERED_OPTION`, `_is_input_box`,
    `_HORIZONTAL_RULE`);
  - the sign-in wait (`_provider_screen_is_waiting_on_a_sign_in`,
    `PROVIDER_WAITING_ON_SIGN_IN_MARKERS`).
- **Settling:** `_screen_is_settled`.
- **Stream re-cutting:** `_TerminalStream`, `_unfinished_escape`,
  `_COMPLETE_CSI` and `_ESCAPE_CARRY_LIMIT`.
- **Frame replacement:** `_FRAME_REPLACED` and `_replaced_frame_starts_at`.

**Stayed for 7c-2b.** The session runner and its terminal ownership: the pty,
the raw terminal, the mode ledger, the narrator, the timing and `SETUP_*`
constants, and `_countdown`. It reaches the classifiers through the launcher's
explicit re-exports.

**Boundaries.** `team_launcher` imports 10 names explicitly. No member is
patched by any suite. The suites' source reads are of code that stayed:
`getsource` of `switchyard_new_command`/`switchyard_main`, and the text of
`run_first_run_auth_phase`. Eleven private helpers nothing outside reads are
no longer launcher attributes.

**Evidence.**
- The AST proof holds for 21 definitions, including the bound-name check.
- **Behavioural differential.** The 9 captured provider screens in
  `tests/fixtures/claude-first-run` were run through the classifiers on both
  trees, from `provider_screen` on the candidate and `team_launcher` on the
  baseline. That covers:
  - every sampled prefix of each screen, for visible text, draws-something,
    waiting-for-an-answer, settled with and without a pending redraw, and the
    replaced-frame start;
  - every two-read split of each screen through `_TerminalStream`.

  All **13,542 cases** hash identically:
  `5e17602bdbc38494ee08d348d3a9d1133b8e7814be2fcd1a2b3efb8990f0698b`.
- The new `tests/provider_screen_boundary_test.py` has 11 checks. It pins the
  leaf property, one set of objects in both import orders, and one captured
  screen per independent reading: the sign-in box by its phrase, the theme
  menu by its choice structure alone, and the Codex login by the sign-in wait
  alone. 6 of 6 mutations are killed. Dropping the sign-in reading first
  survived, because the sign-in box is also caught by its phrase; the Codex
  login screen was added and kills it.
- Green and identical to the baseline:
  - first-run setup (492, the captured-screen suite), single login (34),
    trust-step silence (32), login inheritance (82), first-run auth, first-run
    models and Codex folder trust (53);
  - the twelve earlier boundary tests.
- CLI help output is identical to the baseline for 4 invocations.
- A staged release contains and loads the module.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`9aecc37`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 31,495 lines) | 1 (`provider_screen.py`, 344 lines, a leaf) |
| screen-classifier definitions in `team_launcher.py` | 21 | 0 |
| Where it sits | lines 12921–13280, interleaved with the session runner's constants | the whole file |

### SYRD-299 (slice 7c-2b): foreground first-run session runner

Re-measured on `14d1cc9` before editing, and the design was posted on the
ticket first. The closure is 27 definitions (856 lines), all in one contiguous
region at lines 12914–13853. It moved into **`scripts/provider_session.py`**
(1,022 lines, including a 48-line header):
- **Driving a step:** `run_provider_first_run_session` and
  `_run_provider_first_run`.
- **The pty proxy:** `PtyForegroundSession`, with input forwarding,
  completion detection and child lifecycle.
- **Terminal ownership:**
  - `_RawTerminal` and `_TerminalModeLedger`, with
    `TERMINAL_PRIVATE_MODES_AT_REST`, `_PRIVATE_MODE`, `_KITTY_KEYBOARD` and
    `_MODIFY_OTHER_KEYS`;
  - `_own_the_terminal`, `_terminal_window_size`, `_set_terminal_window_size`
    and `set_terminal_title`.
- **Narration:** `_SetupWindowNarrator` and `_countdown`.
- **Wording and timings:** the `SETUP_WINDOW_*`, `SETUP_STEP_*`,
  `PROVIDER_*` and `FOREGROUND_COMPLETION_POLL_SECONDS` constants.

The `termios`, `tty`, `pty`, `fcntl` and `codecs` imports stay function-local,
as they were.

**Three shared constants moved too:** `SETUP_PURPOSE_SIGN_IN`,
`SETUP_PURPOSE_FOLDER_TRUST` and `FOREGROUND_COMPLETION_TIMEOUT_SECONDS`.
The moved code reads them at import time: `SETUP_STEP_DONE_AT_PROMPT` and
`SETUP_STEP_STALLED_DETAIL` are dicts keyed on the purposes, and the timeout is
a default argument. The extractor refused the first cut for that (rules 6 and
8). None of the three is patched. The launcher re-imports them at its top, so
its remaining defaults (`_run_owner_cli_until`'s timeout and purpose) bind the
same objects when they are defined.

**Boundaries.**
- `provider_session` → `provider_screen`: a direct top-level import of the
  four classifiers it uses (`_TerminalStream`, `_draws_something`,
  `_replaced_frame_starts_at`, `_screen_is_settled`). The leaf never imports
  it back.
- `provider_session` → launcher, at call time only: `_owner_command_env_args`
  and `_pane_identity_scrubbed_env`, each patched by a suite, are read in
  `_run_provider_first_run` through a function-level import.
- Launcher → `provider_session`: an explicit import of 15 names. Five are
  used by the launcher's remaining code; ten more are read by the suites as
  `team_launcher.<name>`. Fifteen private names nothing outside reads are no
  longer launcher attributes.
- Two comments that said the session runner "stays in the launcher" were
  corrected. One is above the launcher's `provider_screen` import; the other is
  in `provider_screen`'s docstring. These are the only non-move edits.

**Evidence.**
- **AST proof.** Before the comment corrections it holds for 30 definitions:
  identical ASTs, the launcher equal to the baseline minus the moved nodes
  plus one import, comments conserved and no unbound names. After the
  corrections, the only difference it reports is those two comment lines.
- The new `tests/provider_session_boundary_test.py` has 8 checks. It pins:
  - one-way dependencies;
  - one set of objects in both import orders, the session's classifiers being
    `provider_screen`'s own;
  - that patches of the two launcher seams reach a started step, through an
    injected runner.

  6 of 6 mutations are killed, each at its own assertion. The two import
  mutations were redone as end-of-file imports, which make no cycle; a
  top-of-file cycle dies on an ImportError before any assertion.
- Green and identical to the baseline:
  - first-run setup (492), which drives real ptys, a `sleep` child and the
    mode ledger;
  - single login (34), trust-step silence (32), login inheritance (82),
    first-run auth, first-run models and Codex folder trust (53);
  - `launch_without_model_probes` (22) and `missing_cli_install_hint`;
  - desktop policy (11), owner model catalog (77), provisioning timing (8)
    and model tool-call probe;
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
- Red on both trees and identical per case:

  | Suite | Pass | Fail |
  |---|---:|---:|
  | first-run Hermes | 5 | 1 |
  | onboarding git | 7 | 3 |
  | env config | 24 | 2 |
  | switchyard resolution (rule 14 runner) | 4 | 7 |
  | desktop access | 3 | 1 |
  | git ownership lint (the known chokepoint findings; line numbers differ) | 9 | 1 |

- The captured-screen digest over 13,542 cases is unchanged
  (`5e17602b…`).
- `--help` output and exit codes are identical to the baseline for all 36
  invocations: bare, plus every verb.
- A staged release contains the module and loads it from the release root.
  The moved names are the launcher's own, and `new --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`14d1cc9`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 31,209 lines) | 1 (`provider_session.py`, 1,022 lines) |
| session/terminal `def`/`class` in `team_launcher.py` | 11 | 0 |
| `pty`/`termios`/`tty` word hits in `team_launcher.py` | 30 | 3 |
| `SETUP_` hits in `team_launcher.py` | 30 | 10 (import list and the auth phase's uses) |

### SYRD-300 (slice 7c-3): first-run authentication phase and report

Re-measured on `57cac70` before editing, and the design was posted on the
ticket first. The raw closure of the named functions is 17 definitions (633
lines). 13 definitions moved into **`scripts/first_run_auth.py`** (703 lines).
They came from two regions, lines 11538–11598 and 12931–13568:
- **The report:** `FirstRunAuthReport` and `OwnerShellIssue`, one of its
  fields.
- **The phase:** `run_first_run_auth_phase`, with `_run_owner_cli_until` and
  the `_provider_setup_instruction`, `_provider_sign_in_instruction` and
  `_folder_trust_instruction` helpers. Its order is unchanged: provider setup,
  then a single login per provider (the account is re-read first), then folder
  trust.
- **The runner sentinel:** `_NoRunnerInjected`, `NO_RUNNER_INJECTED` and
  `foreground_runner_for` (rule 15).
- **The warning and stop gates:** `report_first_run_auth_warnings`,
  `stop_before_launch_for_unauthenticated_providers` and
  `_resumable_next_action`.

**Stayed in the launcher, though inside the raw closure:**
- `_missing_cli_install_clause` and the install table with its other
  formatters. They are under the no-execution guard (rule 9), whose scope is
  unchanged.
- `_pane_identity_scrubbed_env`: a seam `provider_session` and
  `provider_auth_status` read there.
- The generic env helpers (`_env_prefix`, `_env_unset_prefix`,
  `PANE_TARGET_ENV_KEYS`, `PROBE_IDENTITY_ENV_KEYS`), which `role_command`
  also reads.
- `FIRST_RUN_AUTH_LOGIN_COMMANDS`, shared with `first_run_setup` and
  `role_runtime`.

**Boundaries.**
- **At the new module's top:** it imports `FOREGROUND_COMPLETION_TIMEOUT_SECONDS`
  and the two purposes from `provider_session`, because they are def-time
  defaults, and `runtime_catalog`. It never imports the launcher at its top.
- **Patched names:** `run_first_run_auth_phase` (12 sites),
  `report_first_run_auth_warnings` (5) and `_run_owner_cli_until` (4) moved.
  The moved callers reach them, and the session runner's two entry points,
  through `launcher.` at call time. Every other launcher facility is read the
  same way.
- **Exports:** the launcher imports 9 names explicitly. The three instruction
  helpers and `_resumable_next_action` have no outside reader.
- **Stale docstrings corrected:** `provider_session`, `first_run_setup` and
  `provider_auth_status` each said the phase stays in the launcher; each now
  names the new module.

**Source guards.** Both were identified before editing and widened in the
same commit:
- `first_run_single_login_test` slices `run_first_run_auth_phase` out of the
  source text to check the setup-before-login order. It now reads
  `first_run_auth.py`. Its slice is identical to the baseline's once the
  `launcher.` prefixes are removed.
- `launch_without_model_probes_test` requires exactly one
  `validate_models=True`. It now scans the launcher and the new module
  together, and a mutant that adds an opt-in in the new module is caught.
- The `getsource` guards on `switchyard_new_command`/`switchyard_main` and
  the install-table guard read code that stayed.

**Evidence.**
- **AST proof:** it holds for 13 definitions, before the docstring
  corrections, which touch only other modules' docstrings.
- **New boundary test:** `tests/first_run_auth_boundary_test.py` has 11
  checks. It pins:
  - the import footprint (no launcher);
  - one set of objects in both import orders, with the defaults being the
    session runner's own;
  - one sentinel shared by `switchyard new` and the phase, which reads as the
    watched path;
  - no patched step runner called past the launcher;
  - the warning report printing the launcher's install clause and login
    command as they are when it runs.

  8 of 8 mutations are killed, each at its own assertion. That includes an
  opt-in hidden in the new module, killed by the widened model-probe guard.
- **Green and identical to the baseline:**
  - first-run setup (492), single login (34), login inheritance (82) and
    trust-step silence (32);
  - first-run auth, first-run models and Codex folder trust (53);
  - launch without model probes (22), install hint and agent CLI host-wide;
  - model probe evidence (29), model tool-call probe, owner model catalog (77)
    and owner GitHub identity;
  - desktop policy (11) and provisioning timing (8);
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
- **Red on both trees, identical per case** (rule 14 runner, refusing
  provider and `sudo` stubs):

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | env config | 24 | 2 | |
  | first-run Hermes | 5 | 1 | |
  | onboarding git | 7 | 3 | |
  | presentation | 11 | 5 | |
  | switchyard commands | 10 | 2 | |
  | switchyard resolution | 4 | 7 | |
  | git ownership lint | 9 | 1 | line numbers differ |
  | tenant-control bridge e2e | 8 | — | case lines identical; the namespace child then fails on both trees |
  | declarative workflow | — | — | stops at its namespace ownership step on both trees; normalised logs identical |

- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. The phase and the sentinel are the launcher's own objects, and
  `new --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`57cac70`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 30,284 lines) | 1 (`first_run_auth.py`, 703 lines) |
| Where it sits | two regions, lines 11538–11598 and 12931–13568 | the whole file |
| phase/report `def`/`class` in `team_launcher.py` | 12 | 0 |
| `warning: switchyard` lines in `team_launcher.py` | 25 | 17 |

### SYRD-301 (slice 8a): `status` and `release-status` verbs

Re-measured on `f29f5a0` before editing, and the design was posted on the
ticket first. The closure is 21 definitions (692 lines), taken from five
regions: lines 11472–11545, 17365–17877, 21716–21823, 27147–27194 and
27297–27303. All of it moved into **`scripts/project_status.py`** (800 lines):
- **The result types:** `SwitchyardProjectStatus` and
  `SwitchyardRuntimeCopyStatus`.
- **Project status:** `switchyard_project_statuses`,
  `STATUS_PROBE_TIMEOUT_SECONDS`, the root-record fallback
  `root_recorded_tenant_facts`, and the JSON payload helpers.
- **Runtime-copy reporting:** `switchyard_runtime_copy_statuses` with the
  checkout, release and shared-release probes, and the installed wrapper and
  its target parsers.
- **The verbs:** `switchyard_status_command` and
  `switchyard_release_status_command`, their parsers, `format_release_alignment`
  and `close_release_phase` (the `--close` journal path; `rollout_journal`
  stays a function-local import).

**Boundaries.**
- The module imports nothing of Switchyard's at its top. Its 22 launcher
  facilities are read at call time, including `load_project_config`,
  `_switchyard_entries`, `tenant_release_status`, `release_alignment`,
  `record_upgrade_phase` and `pane_liveness`.
- No default argument or decorator names a launcher name.
- `ProjectConfig`, `ReleaseAlignment` and `LauncherCheckoutProbe` are
  annotation-only.
- The launcher imports 11 names explicitly: the 2 verbs and 2 parsers that
  `switchyard_main` dispatches to, and 7 more the suites read.
- The one patched name, `switchyard_status_command`
  (`tenant_control_bridge_e2e_test`), is still dispatched through the
  launcher's global, so the patch still decides what `switchyard status` runs.
- No source guard named the moved code, so none needed widening.

**Evidence.**
- **AST proof:** it holds for 21 definitions: identical ASTs, the launcher
  equal to the baseline minus the moved nodes plus one import, comments
  conserved and no unbound names. There are no non-move edits.
- **New boundary test:** `tests/project_status_boundary_test.py` has 6
  checks. It pins:
  - no Switchyard import at the top;
  - one set of objects in both import orders;
  - `switchyard status --json` reaching the launcher's patched verb;
  - a status reading the launcher's listing and config loader when it runs.

  6 of 6 mutations are killed, each at its own assertion.

  The first run of the "dispatch bypasses the patch" mutant fell through to
  the real verb and read this host's tenants. It was read-only, with `sudo`
  stubbed. The test now replaces the verb's first step with a function that
  fails the test, so a fallthrough stops before any host read.
- **Green and identical to the baseline:**
  - read-only status without root (11), status liveness (20), release-phase
    journal (77) and rollout journal (10);
  - legacy workflow migration (105) and trampoline classification;
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
- **Red on both trees, identical per case** (rule 14 runner, refusing
  provider and `sudo` stubs):

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | design | 17 | 1 | |
  | freshness | 15 | 1 | |
  | tenant suspension | 15 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | tenant-control bridge | 20 | 1 | |
  | switchyard commands | 10 | 2 | |
  | switchyard resolution | 4 | 7 | |
  | tenant-control bridge e2e | 8 | — | case lines identical |
  | git lint | 9 | 1 | line numbers differ |
  | tmux invocation lint | — | — | identical log; stops at a test-file finding on both trees; the new module has no tmux argv |

- **CLI:** `--help` output and exit codes are identical for all 36
  invocations, including `status` and `release-status`.
- **Staged release:** it contains the module and loads it from the release
  root. The verbs are the launcher's own objects, and
  `release-status --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`f29f5a0`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 29,664 lines) | 1 (`project_status.py`, 800 lines) |
| Where it sits | five regions, lines 11472–27303 | the whole file |
| status `def`/`class` in `team_launcher.py` | 20 | 0 |

### SYRD-302 (slice 8b): board and listener service control

Re-measured on `1b07f86`. The seed closure is 22 definitions (384 lines).
**Process note:** the design was posted on the ticket after a local extraction
run rather than before it. Nothing had been committed or published, and the
comment says so. 28 definitions moved into **`scripts/board_services.py`**
(557 lines), from seven regions: lines 2939–2943, 13024–13085, 17015–17018,
22944–23234, 23382–23522, 25117–25130 and 25299–25335.
- **The service user:** `board_service_user`, `_default_board_service_user`,
  `_ensure_board_service_user` and its `_non_login_shell_path`, and
  `_ensure_board_service_peer_auth`.
- **Unit names:** `_board_system_unit`, `_listener_user_unit`,
  `_canary_system_unit`, `_project_service_units`, `managed_unit_names` and
  `_owner_user_unit_path`.
- **The board system unit:** `_board_system_unit_action` and
  `board_system_unit_is_active`.
- **The listener user unit:** `capture_listener_state`,
  `stop_owner_listener` and `start_owner_listener`.
- **Authority units:** `authority_unit_installs`, `activate_board_authority`
  and `restore_installed_units` (rollback).
- **The owner's user manager:** `repair_owner_user_manager`,
  `owner_user_manager_state` and `_run_owner_user_systemctl`, with
  `OWNER_USER_MANAGER_TIMEOUT_SECONDS`,
  `OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS`,
  `OWNER_USER_MANAGER_TIMED_OUT` and `MANAGER_RESPONDING/WEDGED/UNREACHABLE`.
  This family is not in the seed closure but is the listener's only way to
  its manager. The timeout is a def-time default of two of these functions
  (rule 6). `upgrade_project_command` keeps reading the family through the
  launcher.

**Kept in the launcher:**
- `_uid_for_user`: a generic user lookup, patched on the launcher and read
  through it by `agy_credential` and `role_credentials`.
- `_installed_unit_path`, `_tenant_owner_home` and `_owner_user_systemctl`:
  they have callers outside this responsibility.

All four are read at call time.

**Boundaries.**
- The module imports nothing of Switchyard's at its top. `ProjectConfig` and
  `ProjectBoardProvision` are annotation-only.
- The launcher imports 22 names explicitly.
- The five patched names:
  - `capture_listener_state` and `board_system_unit_is_active`
    (`single_owner_staged_tooling_test`) and `_ensure_board_service_peer_auth`
    (`onboarding_git`): their lifecycle callers stay in the launcher.
  - Moved code reads `capture_listener_state` and `_uid_for_user` through the
    launcher.
  - `OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS` is patched by REBINDING in
    `owner_user_manager_recovery_test`. It is now read as
    `launcher.OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS` in
    `repair_owner_user_manager`'s body (rule 16).
- **Source guards:** `agent_cli_host_wide`, `publication_boundary_upgrade`
  and `single_owner_staged_tooling` read call sites that stayed in the
  launcher, so nothing needed widening.

**Evidence.**
- **AST proof:** it holds for 28 definitions: identical ASTs, the launcher
  equal to the baseline minus the moved nodes plus one import, comments
  conserved and no unbound names. There are no non-move edits.
- **Rebinding seam, by time:**
  `test_a_restart_that_does_not_recover_it_stops_the_upgrade` passes on both
  trees. With the rebinding reached it takes **2.5 s**. With the moved code
  reading the constant past the launcher, it still passes but takes
  **60.5 s**.
- **New boundary test:** `tests/board_services_boundary_test.py` has 8
  checks. It pins:
  - no Switchyard import at the top;
  - one set of objects in both import orders;
  - that the patched names are read only as `launcher.<name>`;
  - that a listener stop uses the launcher's owner-command builder and the
    launcher's `capture_listener_state`.

  6 of 6 mutations are killed, each at its own assertion. Its runner only
  records argv; no mutant ran systemctl.
- **Green and identical to the baseline:**
  - legacy presentation migration (158), legacy desktop-policy upgrade (67)
    and upgrade cutover;
  - authority before deploy, installed-release deploy, single-owner staged
    tooling (20) and local publication identity (16);
  - upstream report credential (54), agent CLI host-wide and role-prompt
    integration;
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
- **Red on both trees, identical per case.** The rule 14 runner was used,
  with refusing provider, `sudo`, `systemctl` and `loginctl` stubs; no stub
  was reached.

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | owner user manager recovery | 6 | 3 | |
  | Claude permission hook | 22 | 1 | |
  | presentation window recovery | 19 | 2 | |
  | publication boundary upgrade | 17 | 1 | |
  | design | 17 | 1 | |
  | layout upgrade commands | 12 | 8 | |
  | onboarding git | 7 | 3 | |
  | pinned release resume | 14 | 1 | |
  | project precheck | 34 | 4 | |
  | new prompts | 11 | 3 | |
  | tenant suspension | 15 | 2 | |
  | trusted migration artifact | 6 | 7 | |
  | desktop policy owner | — | — | namespace suite; normalised logs identical |
  | git lint | 9 | 1 | line numbers differ |
  | tmux invocation lint | — | — | identical log; the new module has no tmux use |

  These per-case results were re-run after a timing mutation had briefly
  overlapped the first run; both runs agree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. `start_owner_listener` and the rebinding-patched timeout are the
  launcher's own objects, and `upgrade --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`1b07f86`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 28,942 lines) | 1 (`board_services.py`, 557 lines) |
| Where it sits | seven regions, lines 2939–25335 | the whole file |
| `systemctl` mentions in `team_launcher.py` | 41 | 25 |

### SYRD-303 (slice 9a): workflow adoption, migration and handoff

Re-measured on `1e84784`. The design was posted **before** any edit, when only
a dry run of the extractor had been made. The closure is 19 definitions (966
lines), taken from five regions: lines 14899–15169, 15465–15500, 15544–15951,
16547–16774 and 25531–25582. All of it moved into
**`scripts/workflow_adoption.py`** (1,068 lines):
- **Adoption:** `WorkflowAdoption`, `_canonical_workflow`,
  `_workflow_difference`, `propose_workflow_adoption`, the legacy onboarding
  projection `propose_legacy_workflow_adoption`, `read_board_columns`,
  `write_workflow_record`, and `switchyard_adopt_workflow_command` with its
  parser.
- **Migration:** `WorkflowMigration`, `effective_workflow_document`,
  `plan_workflow_migration`, and `switchyard_migrate_workflow_command` with
  its parser.
- **The root-vouched handoff:** `workflow_handoff_path`,
  `publish_workflow_handoff`, `HandedOffWorkflow`, `read_workflow_handoff` and
  `install_handed_off_workflow`.

**Boundaries.**
- The module imports nothing of Switchyard's at its top. Its 20 launcher
  facilities are read at call time: config loading and verification, trusted
  owner identity, the privileged provision root and no-follow plan reader,
  the recorded and board-declared workflows, pane declarations and the
  resume helpers.
- `rollout_journal.Attempt` and `write_client.UnixHTTPConnection` stay
  function-local imports.
- **Patched names:**
  - `install_handed_off_workflow` is called by `finish_upgrade_command` and
    `_finish_upgrade_preview` through the launcher's name, exactly the
    baseline's two call sites.
  - `read_board_workflow_state` stays in the launcher and is read through it.
- The launcher imports 13 names explicitly.
- No source guard named the moved code. Every suite that reads launcher text
  was run on both trees.

**The bare-import finding (rule 17).** `legacy_workflow_migration_test` went
red on the first candidate because its `tl` was the bare launcher copy. The
finding and the fix were posted on the ticket before the one-line import
change.
- It now passes 105/105, identical per case to the baseline (21/21).
- In an isolated export of the candidate, with the `root_holds` patch
  neutralised, the dry-run case fails. So that patch is what carries the case,
  and it now reaches the moved code.

**Evidence.**
- **AST proof:** it holds for 19 definitions: identical ASTs, the launcher
  equal to the baseline minus the moved nodes plus one import, comments
  conserved and no unbound names.
- **New boundary test:** `tests/workflow_adoption_boundary_test.py` has 9
  checks. It pins:
  - no Switchyard import at the top;
  - one set of objects in both import orders;
  - no patched name read past the launcher;
  - finish-upgrade calling `install_handed_off_workflow` only by the
    launcher's name, at exactly its two baseline sites;
  - a handoff read located by the launcher's provision root and opened by its
    root-owned, single-link reader.

  7 of 7 mutations are killed, each at its own assertion. They ran serially,
  on the clean tree, with nothing else running.
- **Green and identical to the baseline:**
  - adopt workflow, legacy workflow adoption (111), equivalence (22) and
    migration (105, after rule 17);
  - migrate workflow from an installed release (27) and the root workflow
    record;
  - finish-upgrade dry-run parity (21) and pinned release (8), upgrade
    cutover, installed-release deploy and release-phase journal (77);
  - workflow pane rebind (89) and adopt registry config (37);
  - the text-reading guard suites: first-run setup (492), launch without
    model probes (22), install hint, agent CLI host-wide, provisioning timing,
    single-owner tooling, Hermes session isolation, smoke layout,
    presentation titles, registration socket and signoff attribution;
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical log |

- **CLI:** `--help` output and exit codes are identical for all 36
  invocations, including `adopt-workflow`, `migrate-workflow` and
  `finish-upgrade`.
- **Staged release:** it contains the module and loads it from the release
  root, with the verbs the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`1e84784`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 28,485 lines) | 1 (`workflow_adoption.py`, 1,068 lines) |
| Where it sits | five regions, lines 14899–25582 | the whole file |
| `adopt` mentions in `team_launcher.py` | 76 | 24 |

### SYRD-304 (slice 9b): workflow pane rebind and reconciliation

Re-measured on `fcafa65`. The design was posted **before** any edit. The raw
closure is 17 definitions (651 lines). 13 of them, from lines 15251–15841 and
24572–24595, moved into **`scripts/pane_rebind.py`** (677 lines):
- **Reconciliation:** `PANE_BINDING_FIELDS`, `PaneRebind` and
  `plan_pane_rebind` (runtime, target and slot reconciliation);
  `read_runtime_registrations`, `_registration_visibility` and
  `recorded_pre_migration_panes`.
- **The presentation projection:** `ProjectionRewrite`,
  `plan_projection_rewrites` and `_reconciled_presentation_section`.
- **The digest:** `rebind_review_digest`, the exact digest a preview shows
  and an apply presents again.
- **The root prologue:** `root_verified_tenant`.
- **The verb:** `switchyard_rebind_workflow_panes_command` (preview/apply)
  and its parser.

**Kept in the launcher as seams, read at call time:**
- `read_board_workflow_state`: patched, and read by `workflow_adoption`.
- `role_pane_declaration`: read by `workflow_adoption`.
- `role_runtime_binding`: read by `role_command`.
- `presentation_section_for_roles`: the general presentation rule, which is
  tested on its own and belongs with the presentation slices (plan row 12).

**Boundaries.**
- The module imports nothing of Switchyard's at its top; its launcher
  facilities are read at call time.
- `peer_identity`, `rollout_journal` and `workflow_config.RUNTIMES` stay
  function-local imports.
- The launcher imports 4 names explicitly.
- No source guard named the moved code.

**Rule 17 audit.** `workflow_pane_rebind_test` imports the launcher bare but
never assigns to it. Its patched section, the uid-0 privileged prologue,
patches `scripts.team_launcher` through `owner_patches`
(`uid_for_user`, `home_dir_for_user` and the shared `pwd`). The moved
`root_verified_tenant` reads that module, so the suite is unchanged.
- **Evidence:** in an isolated export, the privileged child ran its 7 checks.
- With `owner_patches` neutralised, it fails on the owner's home those
  patches supply.

**Evidence.**
- **AST proof:** it holds for 13 definitions: identical ASTs, the launcher
  equal to the baseline minus the moved nodes plus one import, comments
  conserved and no unbound names.
- **New boundary test:** `tests/pane_rebind_boundary_test.py` has 8 checks.
  It pins:
  - no Switchyard import at the top;
  - one set of objects in both import orders;
  - that the five shared seams are defined elsewhere and read only through
    the launcher;
  - that a reconciled presentation section is derived by the launcher's rule
    when it runs.

  7 of 7 mutations are killed, each at its own assertion. They ran serially,
  on the clean tree, with nothing else running.
- **Green and identical to the baseline:**
  - workflow pane rebind (89, with the privileged child's 7) and
    presentation slot contraction (22);
  - the adoption regression: adopt workflow, legacy adoption (111), legacy
    migration (105) and finish-upgrade dry-run parity (21);
  - role command boundary and every text-reading guard suite;
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical log |

- **CLI:** `--help` output and exit codes are identical for all 36
  invocations, including `rebind-workflow-panes`.
- **Staged release:** it contains the module and loads it from the release
  root, with the verb and `root_verified_tenant` the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`fcafa65`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 27,498 lines) | 1 (`pane_rebind.py`, 677 lines) |
| Where it sits | lines 15251–15841 and 24572–24595 | the whole file |
| `rebind` mentions in `team_launcher.py` | 61 | 10 |

### SYRD-305 (slice 10a): role-account migration

Re-measured on `1e0dd96`. The design was posted **before** any edit. The
closure is 16 definitions (648 lines), lines 16598–17354. All of it moved
into **`scripts/role_account_migration.py`** (747 lines):
- **The config half:** `upgrade_role_accounts_in_config`.
- **Rendering the root migration script:** `render_role_account_migration`
  and `account_existence_guard`, with the access builders it composes:
  - `role_path_access_commands`;
  - `configured_commit_store_paths` and `commit_store_read_commands_for`;
  - `repository_copy_confinement_commands_for`;
  - `director_control_access_commands_for`;
  - `_ACL_NAMED_USER` and `named_acl_users`.
- **Root's trusted copy:** `publish_role_account_migration`,
  `trusted_role_account_migration_path`, `_privileged_artifact_boundary`,
  `remove_untrusted_role_account_migration` and
  `_privileged_directory_is_closed`.
- **The operator instruction:** `role_account_migration_instruction`.

**Boundaries.**
- No member is patched, and no other module reads one.
- The 21 launcher facilities they use are read at call time: account naming,
  user and home resolution, the privileged provision root and its modes, and
  tooling staging.
- `project_provision` and `commit_repos` stay function-local imports.
- The lifecycle callers stay in the launcher: `new`, `upgrade` and the
  identities cutover. The launcher imports 10 names explicitly.
- No source guard names the moved code, and it defines no git builder.
- No bare-importing suite drives it (rule 17).

**Evidence.**
- **AST proof:** it holds for 16 definitions: identical ASTs, the launcher
  equal to the baseline minus the moved nodes plus one import, comments
  conserved and no unbound names.
- **New boundary test:** `tests/role_account_migration_boundary_test.py` has
  6 checks. It pins:
  - no Switchyard import at the top;
  - one set of objects in both import orders;
  - that root's trusted copy is placed by the launcher's provision root,
    directory and migration name as they are when asked;
  - that its trust walk stops at the launcher's redirected root.

  6 of 6 mutations are killed, each at its own assertion. They ran serially,
  on the clean tree.
- **Green and identical to the baseline:**
  - role-account script order, role path access, tenant source confinement
    (17), board-skill staged bundle and upgrade cutover;
  - privileged artifact privacy, authority before deploy and installed-release
    deploy;
  - legacy presentation migration (158) and legacy desktop-policy upgrade (67);
  - upstream report credential (54) and every text-reading guard suite;
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
- **Red on both trees, identical per case.** The rule 14 runner was used,
  with refusing provider, `sudo`, `systemctl` and `loginctl` stubs; no stub
  was reached.

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | trusted migration artifact | 6 | 7 | |
  | role-control sudoers install | 7 | 1 | |
  | owner user manager recovery | 6 | 3 | |
  | Claude permission hook | 22 | 1 | |
  | presentation window recovery | 19 | 2 | |
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | design | 17 | 1 | |
  | layout upgrade commands | 12 | 8 | |
  | pinned release resume | 14 | 1 | |
  | new prompts | 11 | 3 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | desktop policy owner | — | — | namespace suite; normalised logs identical |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical |

- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root, with the render and publish functions the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`1e0dd96`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 26,888 lines) | 1 (`role_account_migration.py`, 747 lines) |
| Where it sits | lines 16598–17354 | the whole file |
| `setfacl` mentions in `team_launcher.py` | 6 | 0 |

### SYRD-306 (slice 10b): provider-state generation and runtime-registration reads

Re-measured on `96953de`. The design was posted **before** any edit. The raw
closure is 16 definitions (341 lines).
- `_read_json_object` and `FIRST_RUN_SETUP_CLIS` stay in the launcher as
  seams.
- Two names outside that closure move with it:
  - `RUNTIME_REGISTRATION_TIMEOUT_SECONDS`, a def-time default of the wait
    (rule 6);
  - `RuntimeRegistrationWait`, the wait's result type.
- The provider-state writers take `_drop_to_account` as a def-time default,
  and `_run_as_account` is also used by `_report_new_project_to_caller`, which
  stays. So both went to a stdlib-only leaf, **`scripts/account_drop.py`**
  (57 lines, the `host_accounts` precedent), which both sides import.

**Into `scripts/provider_runtime_state.py` (448 lines), 16 definitions:**
- **Provider-state generation:** `PROVIDER_STATE_RECORD_NAME`,
  `provider_state_generation`, `_provider_state_record_path`,
  `recorded_provider_state_generation`, `_provider_state_store_account`,
  `provider_state_store_problem`, `_nearest_existing_parent`,
  `_path_owner_name`, `record_provider_state_generation` and
  `unreadable_provider_state_roles`.
- **Runtime registration:** `RUNTIME_REGISTRATION_POLL_SECONDS`,
  `RUNTIME_REGISTRATION_TIMEOUT_SECONDS`, `RuntimeRegistrationWait`,
  `read_runtime_assignments`, `await_runtime_registration` and
  `read_runtime_assignment_details`.

**Boundaries.**
- The state module imports only the leaf at its top; the leaf imports only
  stdlib.
- The launcher facilities are read at call time: `_read_json_object`,
  `FIRST_RUN_SETUP_CLIS`, `role_session_dir`, `_role_cli_name`,
  `_write_json_atomic`, `current_user_name`, `_provider_account_setup_complete`
  and `ROLE_CREDENTIAL_ARTIFACTS`.
- The patched `await_runtime_registration` is still called by
  `recovery_readiness_problems` through the launcher's name.
- `presentation_controller` and `project_status` keep reading the poll
  interval and the assignment details through the launcher's re-exports.
- The launcher imports 11 names from the state module and both names from the
  leaf.
- No guard or bare-importing suite names the moved code (bash scan of 17
  text-reading and 6 bare suites).

**Evidence.**
- **AST proof:** it holds for both modules, 16 + 2 definitions: identical
  ASTs, the launcher equal to the baseline minus the moved nodes plus two
  imports, comments conserved and no unbound names.
  - Its first run caught three imports missing from the hand-written header
    (`hashlib`, `datetime`, `timezone`); they were added before any test ran.
- **New boundary test:** `tests/provider_runtime_state_boundary_test.py` has
  15 checks. It pins:
  - the import direction (the leaf, then the state module, never the
    launcher);
  - one set of objects in both orders;
  - that the writers' and `_run_as_account`'s `drop` default is the leaf's one
    function;
  - that the wait's timeout default is the very object the launcher's
    recovery check defaults to;
  - that the seams are read through the launcher, and that the recovery check
    calls the wait by the launcher's name;
  - that a recorded generation is read by the launcher's reader under its
    session directory.

  8 of 8 mutations are killed, each at its own assertion. They ran serially,
  on the clean tree.
- **Green and identical to the baseline:**
  - provider state written as owner (5), first-run login inheritance (82) and
    legacy presentation launch (136);
  - pane liveness (21), status liveness (20) and read-only status (11);
  - resume provision (all three) and repository boundary repair (11);
  - `switchyard new` opens its window (14) and provisioning timing (8);
  - every text-reading guard suite and every boundary test except
    `ticket_board_board_authority`, which is red identically on the baseline
    and is unrelated.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | runtime registration wait | 11 | 1 | see below |
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | presentation window recovery | 19 | 2 | |
  | presentation | 11 | 5 | |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical |

- **A baseline hazard, noted.** `runtime_registration_wait`'s one failing
  case (`test_resume_readiness_passes_once_the_registrations_arrive`) and one
  read-only-status message reach `sudo -u <owner> tmux list-panes`. The
  refusing stub refused it, identically on both trees. Without the stub those
  would be real `sudo` calls.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains both modules and loads them from the release
  root, with the wait and the privilege drop the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`96953de`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 26,225 lines) | 2 (`provider_runtime_state.py`, 448 lines; `account_drop.py`, 57 lines) |
| Where it sits | lines 4890–5178, 14529–14645 and 15587–15625 | the two files |
| `runtime_registration` mentions in `team_launcher.py` | 11 | 10 (import list and callers) |

### SYRD-308 (slice 10c): role-identity cutover

Re-measured on `ce43dcf`. The design was posted **before** any edit. The raw
closure is 27 definitions (1,108 lines). The ten named seams stay, and 17
definitions (~906 lines) moved into **`scripts/role_identity_cutover.py`**
(1,012 lines):
- **Identity proof:** `canonical_role_identities`, `running_role_identities`,
  `settled_role_identities`, `role_process_identity_gaps` and the two
  recovery-probe constants.
- **The cutover and its rollback:** `role_account_cutover` and
  `revert_incomplete_role_account_cutover`.
- **Repatriation:** `repatriate_role_runtime_state`,
  `_copy_tree_without_overwrite`, `_assign_tree_owner`,
  `_worktree_ownership` and `_chown_tree`.
- **Interrupted provider state:** `_interrupted_provider_state_roles` and
  `_finish_interrupted_provider_state`.
- **Readiness:** `verify_role_board_writes`.
- **The orchestration:** `cutover_role_identities_command`.

The cutover command's def-time default `DEFAULT_TENANT_RELEASE_DEPLOY_REF` is
shared with two release functions that stayed, so it went to the one-constant
leaf **`scripts/release_refs.py`** (15 lines, rule 6). The moved code came
from nine regions of the baseline: 935, 17296–17309, 17415–17553, 18652–18721, 18893–18909, 19047–19360, 20286–20302, 20540–20582, 20668–21013.

**Seams kept in the launcher, read at call time:**
- process identity: `process_uid`, `PROC_ROOT` and `_proc_effective_uid`;
- presentation: `reconnect_presentation` and `presentation_is_attached`;
- sessions: `_start_role_sessions_without_a_window`;
- the release transaction: `capture_installed_units`,
  `capture_release_pointer`, `deploy_release_in_transaction` and
  `restore_release_pointer`.

**Patched names that moved:** `cutover_role_identities_command` and
`_interrupted_provider_state_roles`. The launcher still calls each at its one
baseline site, by its own name.

**Boundaries.**
- The module reads 58 launcher facilities at call time.
- It imports only the leaf at its top.
- The launcher imports 9 names from it, and the leaf's constant.

**Source guards and rule 17.** `publication_boundary_upgrade_test` slices
`upgrade_project_command` to `def _role_accounts_ready(`. That slice contains
no moved definition, so its text is unchanged. None of the other text-reading
suites or bare-importing suites references moved code.

**The rule 18 defect.** The first candidate made
`team_launcher_authority_before_deploy_test` and
`team_launcher_upgrade_cutover_test` red, on the shadowed `launcher` parameter.
- The extractor and the proof were fixed, and the module was re-extracted from
  the baseline: the launcher text is byte-identical, and only that function's
  alias changed.
- Both suites are green again.
- The defect and the fix were posted on the ticket before publishing.

**Evidence.**
- **AST proof:** it holds for both modules, 17 + 1 definitions, including the
  new step 6.
- **New boundary test:** `tests/role_identity_cutover_boundary_test.py` has
  16 checks. It pins:
  - the import direction;
  - one set of objects in both orders;
  - that the default ref is one object across the cutover and the two
    release functions;
  - that the launcher calls the patched names at their one site each;
  - that the generic seams are defined elsewhere and read only through the
    launcher;
  - that no call-time import rebinds a parameter or local;
  - that target identities come from the launcher's naming and home lookup.

  10 of 10 mutations are killed, including one that reintroduces the shadow.
  They ran serially, on the clean tree.
- **Stubs.** The runs used refusing provider, `sudo`, `systemctl` and
  `loginctl` stubs, and a tmux stub that refuses any call without an explicit
  `-L`/`-S` socket, since such a call would reach the live server. Isolated
  sockets pass through. No stub was reached.
- **Green and identical to the baseline on the fixed tree:**
  - identity-cutover end-to-end, cutover failure propagation, upgrade cutover
    and authority before deploy;
  - finish-upgrade parity and pinned release, release-phase journal,
    installed-release deploy and legacy workflow migration;
  - legacy presentation launch and migration, single-owner tooling and local
    publication identity;
  - process authority, upstream report credential and every text-reading
    guard suite;
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
  - `privileged_front_door` passed (202 checks) on both trees in this run and
    failed on both in the first run. It is environment-dependent, and both
    trees agreed in each run.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | Claude permission hook | 22 | 1 | |
  | owner user manager recovery | 6 | 3 | |
  | presentation window recovery | 19 | 2 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | design | 17 | 1 | |
  | layout upgrade commands | 12 | 8 | |
  | new project | 18 | 1 | |
  | pinned release resume | 14 | 1 | |
  | new prompts | 11 | 3 | |
  | trusted migration artifact | 6 | 7 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | desktop policy owner | — | — | normalised logs identical |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical |

- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains both modules and loads them from the release
  root, with the cutover command and the default ref the launcher's own
  objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`ce43dcf`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 25,822 lines) | 1 (`role_identity_cutover.py`, 1,012 lines), plus the 15-line leaf |
| Where it sits | nine regions | the whole file |
| cutover `def`/`class` in `team_launcher.py` | 15 | 0 |

### SYRD-309 (slice 11a): tmux viewer builders and launch

Re-measured on `3b60d23`. The design was posted **before** any edit. The
closure is 31 definitions (286 lines), from lines 717–719 and 1904–2246. All of it moved
into **`scripts/tmux_viewer.py`** (404 lines):
- **Viewer defaults:** `DEFAULT_VIEWER_COLUMNS/ROWS`,
  `DEFAULT_TMUX_HISTORY_LIMIT` and `VIEWER_CELL_ASPECT`.
- **Session option argv:** `tmux_set_*_args` and
  `configure_tmux_session_options`.
- **Viewer argv:** `tmux_viewer_*_args` and `tmux_viewer_set_role_arg`.
- **Layout:** `_tmux_layout_checksum`, `_tmux_layout_spans`, `viewer_grid`
  and `viewer_layout_string`.
- **Re-layout hooks:** `viewer_layout_helper_path`, the relayout and
  observer hook argv, `VIEWER_RELAYOUT_UNAVAILABLE_NOTE` and
  `install_viewer_relayout_hook`.
- **The launch:** `launch_tmux_viewer_session`.

**Boundaries.**
- No member is patched.
- `presentation_controller` reads 7 of them through the launcher, and
  `scripts/switchyard-viewer-layout` imports `viewer_layout_string` from it.
- The launcher imports 18 names explicitly.
- `_quote_command` and the has-session/kill-session argv stay in the launcher
  and are read at call time. The top imports only stdlib.
- `viewer_layout_helper_path` resolves from the module's own location. It sits
  in the same `scripts/` directory, so it names the same helper, in a checkout
  and in a staged release; both are checked.
- No moved function binds `launcher` (rule 18); proof step 6 holds.
- The tmux invocation lint scans the whole `scripts/` tree, so the moved
  builders stay in its scope.

**Guards widened, with evidence both ways.**
- **`team_launcher_presentation_titles_test`** now reads `tmux_viewer.py` as
  well:
  - it asserts the `def` there;
  - it keeps both "exactly 2" line counts across the launcher, the viewer
    module and `presentation_controller`;
  - it slices the two files that carry `"set-titles-string"`.

  The baseline version of the guard **fails** on the candidate, and a
  title line planted in the new module is caught by the widened one.
- **`team_launcher_viewer_test`**'s "no `DEFAULT_VIEWER_SESSION`" check now
  reads both files. In an isolated export, the baseline guard **lets** a
  `DEFAULT_VIEWER_SESSION` planted in the new module through; the widened guard
  kills it.
- The other two suites that name moved code call it through the launcher's
  re-exports, or check an unrelated role-CLI table. They are unchanged.

**Evidence.**
- **AST proof:** it holds for 31 definitions, including step 6.
- **New boundary test:** `tests/tmux_viewer_boundary_test.py` has 15 checks.
  It pins:
  - no Switchyard import at the top;
  - one set of objects in both import orders;
  - the names other modules read;
  - the helper beside it;
  - a hook quoted by the launcher's `_quote_command` as patched, through a
    recording runner (no tmux runs).

  7 of 7 mutations are killed, including the two aimed at the widened guards.
  They ran serially, on the clean tree.
- **Green and identical to the baseline, under the rule 19 stub:**
  - viewer builds all six panes (7) and viewer landscape layout (298);
  - attach by role, presentation titles, desktop handoff titles and the tmux
    panes suites;
  - legacy presentation launch and migration, slot contraction, cross-account
    presentation and operator display recovery;
  - every text-reading guard suite and every boundary test except
    `ticket_board_board_authority`, which is red identically on the baseline
    and is unrelated.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | merge gate helper | 12 | 1 | |
  | presentation window recovery | 19 | 2 | |
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | runtime registration wait | 11 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | desktop layout path | 8 | 4 | |
  | new project | 18 | 1 | |
  | presentation | 11 | 5 | |
  | unprivileged presentation | 7 | 4 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | tenant-control bridge e2e | 8 | — | case lines identical |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical |

- **Stub hits:** identical on both trees. They are the known `sudo` in
  `runtime_registration_wait`, and `merge_gate_helper`'s socketless
  `list-panes` (rule 19).
- **CLI:** `--help` output and exit codes are identical for 37 invocations:
  all 36 `switchyard` invocations plus `switchyard-viewer-layout`.
- **Staged release:** it contains the module and loads it from the release
  root. The launch is the launcher's own object, and the helper path resolves
  inside the release; `switchyard-viewer-layout --help` exits 0 there.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`3b60d23`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 24,899 lines) | 1 (`tmux_viewer.py`, 404 lines) |
| Where it sits | lines 717–719 and 1904–2246 | the whole file |
| viewer `def`s in `team_launcher.py` | 26 | 0 |
| `"set-hook"` argv literals in `team_launcher.py` | 2 | 0 |

### SYRD-310 (slice 11b): session records and seeding

Re-measured on `01b036b`. The design was posted **before** any edit. The
closure is 25 definitions (420 lines). Joining it:
- the record timeout and poll constants, def-time defaults of the reporters
  and of `launch_project` and `switchyard_new_command` (rule 6);
- `LaunchSessionRecordStatus`, the reporters' result type.

28 definitions, from lines 649, 712, 967–972, 1143–1176, 3131–3666, moved into
**`scripts/session_records.py`** (572 lines):
- **Record names and dirs:** `session_file_name`, `pane_state_file_name`
  and the legacy and interim seed directories.
- **Choosing the session:** `session_id_for_role` and its superseded and
  unverified-resume variants.
- **Hook freshness and launch outcome:** the pane launch-outcome and
  runtime-hook sources, the hook rules, and the `LAUNCH_RESUME_FALLBACK_*`
  and pane-state skew constants.
- **Reporting:** `launch_session_record_statuses` and
  `report_launch_session_records`.
- **Records:** the `_session_record_*` readers, the payload checks and
  `clear_session_record_for_role`.
- **Seeding:** `seed_session_dir_from_legacy_sources` and
  `seed_default_session_dir_from_legacy_sources`.

**Boundaries.**
- **The patched `report_launch_session_records`** is still called by
  `launch_project` and `switchyard_new_command` through the launcher's name,
  at both baseline sites.
- **The patched `DEFAULT_SESSION_DIR`** stays in the launcher. The moved
  code reads it there at call time, and it is not a default anywhere in the
  moved code.
- `role_session_dir` and the private writers are read at call time too.
- The 4 names other modules read through the launcher (`role_credentials`,
  `role_identity_cutover`, `presentation_controller`, `role_command`,
  `role_runtime`) are among the launcher's 16 explicit exports. The test
  helpers' star import still resolves.
- No moved function binds an import alias (rule 18).
- The top imports only stdlib.

**Guards.** A scan of the 22 text-reading suites found four that name
members; none reads moved code as text.
- `provisioning_stage_timing`, `codex_rendered_scrollback`,
  `hermes_session_isolation` and `viewer_test` only call the moved functions.
- Their text reads are of `switchyard_new_command` and
  `_uses_fresh_session_per_ticket`, which stayed.
- No widening was needed; each suite is compared below.
- **Rule 17:** `codex_effort_config_key_test` imports the launcher bare but
  only calls `session_file_name`. It is included, and identical.

**Evidence.**
- **AST proof:** it holds for 28 definitions, including step 6.
- **New boundary test:** `tests/session_records_boundary_test.py` has 19
  checks. It pins:
  - no Switchyard import at the top;
  - one set of objects in both orders;
  - the names other modules read;
  - the timeout and poll defaults as one object across the reporters,
    `launch_project` and `switchyard new`;
  - the patched reporter at its two sites by the launcher's name;
  - `DEFAULT_SESSION_DIR` read only through the launcher;
  - seeding driven on temp directories through the launcher's patched default
    store and private writer.

  7 of 7 mutations are killed, each at its own assertion. They ran serially,
  on the clean tree.
- **Stubs:** refusing provider, `sudo`, `systemctl` and `loginctl` stubs,
  and the rule 19 tmux stub. The only stub hit is `merge_gate_helper`'s
  socketless `list-panes`, identical on both trees; no fixture test was
  masked.
- **Green and identical to the baseline:** every other suite in the set of
  47, including the four guard suites, codex effort config key, and every
  boundary test except `ticket_board_board_authority`, which is red
  identically on the baseline and is unrelated.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | session report | 12 | 1 | the running-attach message |
  | resume detached | 8 | 1 | shared-checkout call order |
  | reload attach | 12 | 1 | the research `new-session` call |
  | pane paths | 9 | 2 | |
  | project artifacts | 10 | 6 | |
  | env config | 24 | 2 | |
  | desktop access | 3 | 1 | |
  | merge gate helper | 12 | 1 | |
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | layout upgrade commands | 12 | 8 | |
  | new project | 18 | 1 | |
  | presentation | 11 | 5 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical |

  The failing cases in the three session suites are launch-flow fixture
  expectations. None is a record-reading or seeding case.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root, with the reporter and the timeout constant the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`01b036b`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 24,575 lines) | 1 (`session_records.py`, 572 lines) |
| Where it sits | five regions, lines 649, 712, 967–972, 1143–1176, 3131–3666 | the whole file |
| record/seeding `def`/`class` in `team_launcher.py` | 21 | 0 |

### SYRD-311 (slice 11c): session paths and initial pane state

Re-measured on `4025709`. The design was posted **before** any edit. The raw
closure is 13 definitions (108 lines). 11 of them, from lines 1256–1314, 1361–1367, 2745–2766, 3111–3136, moved
into **`scripts/session_paths.py`** (177 lines):
- **Session directories:** `_explicit_session_dir_from_env`,
  `default_session_dir_for_user`, `account_session_dir`,
  `session_dir_uses_user_runtime` and `role_session_dir`.
- **Pane-state directories:** `_explicit_pane_state_dir_from_env`,
  `default_pane_state_dir_for_user`, `shared_pane_state_dir` and
  `role_pane_state_dir`.
- **Initial idle state:** `seed_initial_pane_idle_state` and
  `clear_pane_idle_state_for_role`.

**Kept in the launcher (rule 20):**
- `DEFAULT_SESSION_DIR` (in the closure, rebinding-patched 12 times);
- `LIVE_PGU_STATE_DIR_NAME` (in the closure; it builds that default at import
  time);
- `DEFAULT_PANE_STATE_DIR` (an out-edge, rebinding-patched 6 times, and the
  def-time default of four launcher functions that stay).

The moved resolvers read all three through the launcher when they run. No leaf
was used, because no def-time default in the moved code needs one.

**Patched functions that moved:** `account_session_dir` (4), `role_session_dir`
(3) and `seed_initial_pane_idle_state` (2).
- Their launcher callers resolve the launcher's names.
- The moved `role_session_dir` calls `account_session_dir` as
  `launcher.account_session_dir`.
- The extracted modules that read these through the launcher are unchanged:
  `provider_runtime_state`, `session_records`, `role_runtime`,
  `role_identity_cutover`, `project_provision`, `presentation_controller`,
  `worker_pool` and `workflow_launcher`.
- The launcher imports 9 names explicitly. The top imports only stdlib.
- No moved function binds an import alias (rule 18).
- **Correction to the design comment:** I gave `role_pane_state_dir` calling
  `role_session_dir` as the example. It does not. The real in-module seam is
  `role_session_dir` calling `account_session_dir`, which the boundary test
  pins.

**Guards.** Three text-reading suites name members; none reads moved code as
text. `provisioning_stage_timing` calls `seed_initial_pane_idle_state`, and
the provider-state and session-record boundary tests check that their own
modules read `role_session_dir` through the launcher, which still holds. No
widening was needed. No bare-importing suite references moved code
(rule 17).

**A process slip, disclosed.** My export-insertion script's anchor did not
match on the first attempt. The launcher then lacked the exports; the proof
failed on the unbound names, and I had already committed that state to the
local wip branch. The block was inserted at the correct anchor and the wip
commit amended before any test ran. Nothing was published in between.

**Evidence.**
- **AST proof:** it holds for 11 definitions, including step 6.
- **New boundary test:** `tests/session_paths_boundary_test.py` has 11
  checks. It pins:
  - no Switchyard import at the top;
  - one set of objects in both orders;
  - that the three constants are defined only in the launcher and never read
    bare here;
  - that **rebinding `team_launcher.DEFAULT_SESSION_DIR` reaches the owner's
    and an account's resolvers**, and rebinding `DEFAULT_PANE_STATE_DIR`
    reaches the pane-state resolver;
  - a role's session directory resolved through the launcher's patched
    `account_session_dir`;
  - an idle-state seed written by the launcher's writer under its file name.

  8 of 8 mutations are killed. They include capturing either default instead
  of reading it through the launcher, defining a default in the moved module,
  and bypassing the account helper or the writer. They ran serially, on the
  clean tree.
- **Green and identical to the baseline:** every other suite of the 47. That
  includes the three boundary tests of the modules that read these paths
  (provider-state, session records, cutover), and every boundary test except
  `ticket_board_board_authority`, which is red identically on the baseline and
  is unrelated.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | pane paths | 9 | 2 | |
  | env config | 24 | 2 | the two suites that rebind the defaults |
  | session report | 12 | 1 | |
  | new project | 18 | 1 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | merge gate helper | 12 | 1 | |
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | listener pane-state authority | 19 | 0 | per case; the whole suite fails identically at its Postgres `run_database_checks` phase |
  | declarative workflow | — | — | normalised logs identical |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical |

- **Stubs:** refusing provider, `sudo`, `systemctl` and `loginctl` stubs,
  and the rule 19 tmux stub. The only stub hit is `merge_gate_helper`'s,
  identical on both trees.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. `role_session_dir` is the launcher's own object, and
  `DEFAULT_SESSION_DIR` is defined in the launcher and not in the module.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`4025709`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 24,100 lines) | 1 (`session_paths.py`, 177 lines), with the three shared constants kept in the launcher |
| Where it sits | four regions, lines 1256–1314, 1361–1367, 2745–2766, 3111–3136 | the whole file |
| path/idle-state `def`s in `team_launcher.py` | 11 | 0 |

### SYRD-312 (slice 11d): role session start and stop

Re-measured on `cbee510`. The design was posted **before** any edit. The
closure is exactly 7 definitions (194 lines), from lines 3079–3127, 3323–3328, 3611–3725, 19158–19188, 20628–20659, all
moved into **`scripts/role_sessions.py`** (252 lines):
- `_start_role_session`;
- `_start_role_sessions_without_a_window`;
- `stop_role_sessions`;
- `record_unverified_resume_for_role`;
- `_uses_fresh_session_per_ticket`;
- `tmux_has_session_by_name_args` and `tmux_kill_session_by_name_args`.

**Boundaries.**
- **Patched:** `_start_role_sessions_without_a_window` and
  `stop_role_sessions`, 4 sites each. `stop_project` calls the latter at its
  one baseline site by the launcher's name, and `role_identity_cutover`
  calls both as `team_launcher.X`.
- **External readers:** `role_command` (`_uses_fresh_session_per_ticket`),
  `tmux_viewer` (the has/kill-by-name argv) and `role_identity_cutover`
  (start/stop). All 7 moved names are exported.
- **Read at call time:** 23 launcher facilities, including the `RESUME_*`
  constants, the tmux session argv, the pane launcher, the per-role process
  runner and the session record/path/idle-state functions from 11a–11c.
  Rule 20 holds.
- The top imports only stdlib, and no moved function binds an import alias
  (rule 18).
- No git builder moved, and the tmux invocation lint scans all of
  `scripts/`.

**Guards.**
- **`hermes_session_isolation`** takes `getsource` of
  `team_launcher._uses_fresh_session_per_ticket`. That function needed no
  inserted import, so its source is byte-identical. The boundary test checks
  that `getsource` through the launcher resolves to the new file, and that it
  still decides by the setting, not by the CLI's name.
- **`role_identity_cutover_boundary_test`** requires
  `_start_role_sessions_without_a_window` to be absent from the cutover module
  and read there through the launcher; both still hold.
- No widening was needed. No bare-importing suite references moved code.

**Evidence.**
- **AST proof:** it holds for 7 definitions, including step 6.
- **New boundary test:** `tests/role_sessions_boundary_test.py` has 16
  checks. It pins:
  - no Switchyard import at the top;
  - one set of objects in both orders;
  - each external reader reaching its names through the launcher;
  - `stop_project`'s single call by the launcher's name, and no bare call to
    either patched function in the module;
  - the fresh-session rule's source through the launcher;
  - a stop driven through the launcher's per-role runner and has/kill argv,
    with the caller's runner failing the test if used directly.

  7 of 7 mutations are killed. One, "a stop uses the caller's runner instead
  of the launcher's per-role one", **survived** the first version of the test,
  because the same recorder stood for both runners. The test now separates
  them and kills it.
- **Green and identical to the baseline:**
  - Hermes session isolation, stop/reload, tmux panes and attach by role;
  - upgrade cutover and identity-cutover end-to-end;
  - viewer builds all six panes (7) and landscape layout (298);
  - the cutover, viewer, session-paths and session-records boundary tests;
  - every text-reading guard suite;
  - every boundary test except `ticket_board_board_authority`, which is red
    identically on the baseline and is unrelated.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | session report | 12 | 1 | |
  | presentation | 11 | 5 | |
  | presentation window recovery | 19 | 2 | |
  | viewer | 21 | 5 | |
  | switchyard commands | 10 | 2 | |
  | switchyard resolution | 4 | 7 | |
  | desktop access | 3 | 1 | |
  | merge gate helper | 12 | 1 | |
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | tenant-control bridge e2e | 8 | — | case lines identical |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical |

- **Stubs:** the rule 19 tmux stub and the refusing provider and service
  stubs. The only stub hit is `merge_gate_helper`'s, identical on both
  trees.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root, with both patched functions the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`cbee510`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 23,997 lines) | 1 (`role_sessions.py`, 252 lines) |
| Where it sits | five regions, lines 3079–3127, 3323–3328, 3611–3725, 19158–19188, 20628–20659 | the whole file |
| start/stop `def`s in `team_launcher.py` | 7 | 0 |

### SYRD-313 (slice 11e): role pane entry and resume verification

Re-measured on `1eaf302`. The design was posted **before** any edit. The raw
closure is exactly 42 definitions (576 lines). Its four rebound constants
stay in the launcher (rule 20): `AGY_CONVERSATION_ROOT`,
`RESUME_STARTUP_TIMEOUT_SECONDS`, `RESUME_STARTUP_POLL_SECONDS` and
`DETACHED_SESSION_STABILITY_SECONDS`.

The other 38 formed more than one domain. As the ticket asked, a bounded split
was proposed; it is kept in this one candidate as separate modules, and the
design offered to re-scope into separate tickets instead. No such request came
before submission.
- **`scripts/provider_resume.py`** (360 lines, 29 definitions). Whether a
  provider session can be resumed, and whether it was:
  - the Claude, Codex and agy session-store checks and the `_uses_*` rules;
  - Hermes homes: shared and private entries, lock guards and
    `prepare_hermes_home_for_role`;
  - `_resume_preflight_allows_attempt` and `clear_unverified_resume_for_role`;
  - the launch-status constants and `_resume_launch_status`;
  - `_resume_launch_verified` and `_detached_launch_verified`.
- **`scripts/role_pane_entry.py`** (407 lines, 9 definitions):
  - `run_role_pane`, `run_detached_role`,
    `ensure_visible_role_session_for_viewer` and `attach_role_to_slot`;
  - `tmux_new_session_args` and `tmux_attach_args`;
  - the ambient-session conflict checks and `desktop_reload_is_safe`.
- **`scripts/launcher_env.py`** (35 lines): `_env_first` and
  `DEFAULT_PANE_STATE_DIR`.
  - All four entry points take `DEFAULT_PANE_STATE_DIR` as a default
    argument, so it must be imported when they are defined (rule 6). Its
    definition calls `_env_first`, so both moved.
  - The launcher re-imports both, so the suites' rebinding of
    `team_launcher.DEFAULT_PANE_STATE_DIR` still reaches `session_paths`, which
    reads it there at call time.
  - **Correction to the design comment:** it said three launcher functions
    that stay also took this default. Measured on both trees, only the four
    moved entry points do. The leaf is justified by those four alone, and the
    boundary test pins the measured set.

The moved code came from five regions of the baseline: 755–774, 980–1018, 3051–3311, 3550–3794, 20513–20613.

**Boundaries.**
- **Patched entry points:** `ensure_visible_role_session_for_viewer` (11),
  `run_role_pane` (4) and `run_detached_role` (2). The launcher calls them by
  its own name at the same number of sites as the baseline (3, 1 and 2).
  `presentation_controller` and `role_runtime` reach the first through the
  launcher. `display_recovery_live_tmux` patches it on `pc.team_launcher`, the
  canonical module.
- **External readers:** all are re-exported: 2 names from the leaf, 13 from
  `provider_resume` and 5 from `role_pane_entry`. The readers are
  `role_identity_cutover`, `role_command`, `role_sessions`,
  `presentation_controller` and `role_runtime`.
- **Imports:** `role_pane_entry` imports seven resume names directly from
  its sibling (none is patched) and the default from the leaf. No module
  imports the launcher at its top.
- No moved function binds an import alias (rule 18).

**Guards.**
- `team_launcher_viewer_test`'s "no `DEFAULT_VIEWER_SESSION`" check now also
  reads `role_pane_entry.py`, where making a session visible to the viewer now
  lives.
- The Hermes guard reads a function moved in SYRD-312, and the scrollback
  check concerns the role-CLI table; both are unchanged.
- **Rule 17:** the live-tmux suite, which imports the launcher bare, patches
  the canonical module.

**Evidence.**
- **AST proof:** it holds across the three modules (2 + 29 + 9
  definitions), including step 6.
- **New boundary test:** `tests/role_pane_entry_boundary_test.py` has 28
  checks. It pins:
  - the import direction (leaf, then resume, then entry; never the
    launcher);
  - identity for all three modules in both orders;
  - `DEFAULT_PANE_STATE_DIR` as one object for the four entry points and the
    launcher's name, with no other function carrying it;
  - that a launcher rebinding still reaches `session_paths`;
  - that the rebound constants are the launcher's alone, with a rebound
    `AGY_CONVERSATION_ROOT` being where the resume check looks (temp dir);
  - the patched entry points called by the launcher's name.

  8 of 8 mutations are killed. They include a second default object in either
  module, a captured agy root, and an entry point called past the patch. They
  ran serially, on the clean tree.
- **Green and identical to the baseline:**
  - Hermes session isolation, stop/reload and attach by role;
  - viewer builds all six panes (7) and landscape layout (298);
  - the role-sessions (16), session-paths (11), session-records (19), cutover
    (16) and tmux-viewer (15) boundary tests;
  - every text-reading guard suite and every boundary test except
    `ticket_board_board_authority`, which is red identically on the baseline
    and is unrelated.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | presentation | 11 | 5 | |
  | viewer | 21 | 5 | |
  | pane paths | 9 | 2 | |
  | env config | 24 | 2 | |
  | switchyard commands | 10 | 2 | |
  | desktop access | 3 | 1 | |
  | merge gate helper | 12 | 1 | |
  | privileged front door | 36 | 1 | |
  | publication boundary upgrade | 17 | 1 | |
  | Codex rendered scrollback | 7 | 1 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | git lint | 9 | 1 | |
  | tmux invocation lint | — | — | identical |

- **Stubs:** the rule 19 tmux stub and the refusing provider and service
  stubs. The only stub hit is `merge_gate_helper`'s, identical on both
  trees.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains all three modules and loads them from the
  release root. `run_role_pane`, `DEFAULT_PANE_STATE_DIR` and the resume
  preflight are the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`1eaf302`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 23,804 lines) | 3 (`role_pane_entry.py` 407, `provider_resume.py` 360, `launcher_env.py` 35) |
| Where it sits | five regions, lines 755–774, 980–1018, 3051–3311, 3550–3794, 20513–20613 | three files, one per domain |
| moved `def`/`class` in `team_launcher.py` | 30 | 0 |

### SYRD-314 (slice 11f): tmux session argv and pane-command matching

Re-measured on `4296bed`. The design was posted **before** any edit. The
closure is exactly 8 definitions, all functions, one domain:
- the session argv builders `tmux_has_session_args`, `tmux_kill_session_args`
  and `tmux_current_command_args`;
- `pane_command_args` and `pane_command`;
- live matching: `expected_live_commands`, `process_tree_contains_command`
  and `live_command_matches_role`.

They moved unchanged into **`scripts/tmux_session_argv.py`** (152 lines).
The moved code came from 6 regions of the baseline: 3058–3063, 3070–3071, 3153–3155, 3241–3242, 3277–3291, 3306–3376.

**Boundaries.**
- **Patched builders:** `tmux_has_session_args` and `tmux_kill_session_args`
  are rebound on the launcher by `role_sessions_boundary_test` (2x each,
  measured on the baseline). Both stay launcher seams: the module never calls
  them bare, and every reader reaches them as `launcher.<name>`.
- **Call-time seams:** `live_command_matches_role` asks
  `launcher.pane_pid_for_role` and `launcher.process_tree_command_names`
  when it runs, and falls back to the runner it was given.
- **External readers:** 5 names are re-exported. `provider_resume`,
  `role_identity_cutover`, `role_pane_entry`, `role_runtime`,
  `role_sessions` and `worker_pool` read them through the launcher.
- The module imports only the standard library, with `RoleConfig` under
  `TYPE_CHECKING`, and never the launcher at its top. No moved function binds
  an import alias (rule 18). No rebound constant moved (rule 20).

**Evidence.**
- **AST proof:** it holds, including step 6.
- **New boundary test:** `tests/tmux_session_argv_boundary_test.py` has
  27 checks. It pins:
  - no Switchyard import at the top;
  - identity in both import orders;
  - every reference in every reader going through the launcher (rule 21);
  - no bare call of a patched builder in the module;
  - live matching deciding by the launcher's patched pid and process-tree
    lookups, never asking the runner when a pid exists;
  - the pid-0 fallback asking tmux for `#{pane_current_command}` through the
    given runner only.

  7 of 7 mutations are killed. They are: the module importing the launcher,
  a dropped export, a reader reaching a builder past the launcher, a bare
  builder call in the module, the pid or the process tree read past the
  launcher, and the launcher rebinding a moved name. The reader mutant first
  survived a weaker guard (rule 21). They ran serially, on the clean tree.
- **Green and identical to the baseline:**
  - stop/reload, attach by role, pane commands, tmux panes and tmux cleanup;
  - Hermes session isolation, desktop hand-off titles and presentation
    titles;
  - legacy presentation launch and the three resume-provision suites;
  - the cutover failure propagation, identity cutover e2e and upgrade cutover
    suites;
  - role runtime and the worker-pool lifecycle, start/status and preflight
    suites;
  - viewer builds all six panes and landscape layout;
  - the role-sessions, role-pane-entry, cutover, tmux-viewer and worker-pool
    command boundary tests.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | new project | 18 | 1 | |
  | presentation | 11 | 5 | |
  | first run hermes | 5 | 1 | |
  | resume detached | 8 | 1 | |
  | pinned release resume | 14 | 1 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | tenant resume cli promotion | 13 | 1 | |
  | tmux invocation lint | — | — | identical output |

- **Stubs:** the rule 19 tmux stub and the refusing provider and service
  stubs. No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. The builders, `live_command_matches_role` and `pane_command` are the
  launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`4296bed`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 23,198 lines) | 1 (`tmux_session_argv.py`, 152) |
| Where it sits | 6 regions, lines 3058–3063, 3070–3071, 3153–3155, 3241–3242, 3277–3291, 3306–3376 | one file |
| moved `def`/`class` in `team_launcher.py` | 8 | 0 |

**Next slice** (measured from a call graph of `4296bed`, not of this
candidate; the figures were confirmed unchanged on `65f094a` in SYRD-316, rule 22): 12a, presentation reconnect,
attach check and hand-back. Its closure is 6 definitions / 156 lines.
`reconnect_presentation` is patched 6x, `presentation_is_attached` 2x and
`hand_presentation_back_to_the_caller` 1x. `role_identity_cutover` reads the
first two through the launcher, and `presentation_controller` reads the three
hand-off helpers.

### SYRD-316 (slice 12a): presentation reconnect, attach check and hand-back

Re-measured on `65f094a`, with the call graph regenerated on that tree. The
design was posted **before** any edit. The closure is exactly 6
definitions, one presentation-surface domain:
- after a cutover: `reconnect_presentation` and `presentation_is_attached`,
  which ask `presentation_controller` when they run and never let a failure
  end the cutover;
- handing the window back across the lifecycle bridge:
  `hand_presentation_back_to_the_caller`, `render_presentation_handoff`,
  `presentation_slot_titles` and `PRESENTATION_HANDOFF_FD_ENV`.

They moved unchanged into **`scripts/presentation_reconnect.py`** (213
lines). The hand-back takes `layout: str = LAYOUT_MODE_SEPARATE` as a default
argument, so (rule 6) the four-line `LAYOUT_MODE_*` block moved unchanged to
the leaf **`scripts/layout_modes.py`** (19 lines). Both the module and the
launcher import it, and the default is the launcher's own object. None of
the four is rebound anywhere, so rule 20 did not hold them in the launcher.
The moved code came from 6 regions of the baseline: 832–835, 1878–1932, 3790–3790, 3821–3833, 3874–3914, 18380–18427.

**Boundaries.**
- **Patched entry points (exact):** `reconnect_presentation` is rebound on
  `scripts.team_launcher` by `team_launcher_upgrade_cutover_test` and
  `presentation_window_recovery_test`. `presentation_is_attached` and the
  hand-back are never rebound; guards name them as launcher seams. The
  cutover reaches the first two as `team_launcher.X`, and `launch_project`
  calls the hand-back by the launcher's name at its two baseline sites.
- **Call time:** the hand-back reads `switchyard_pane_launcher_for` (rebound
  by `team_launcher_desktop_handoff_titles_test`), `pane_window_program`,
  `project_window_title` and `PRESENTATION_HANDOFF_SCHEMA` from the launcher;
  slot titles read `project_window_title` and `pane_split_title` there.
- **External readers:** `role_identity_cutover` and `presentation_controller`
  read 7 of these names, every reference through the launcher
  (rule 21). All 10 moved names are re-exported.
- No moved function binds an import alias (rule 18); both wrappers keep
  their unchanged call-time import of `presentation_controller`.

**Guards.** `tenant_resume_presentation_handoff` reads the bridged branch of
`launch_project` and `role_identity_cutover_boundary` counts launcher call
sites of other names; both read code that stayed. No widening was needed.

**Evidence.**
- **AST proof:** it holds for both modules (4 + 6 definitions),
  including step 6.
- **New boundary test:** `tests/presentation_reconnect_boundary_test.py` has
  35 checks. It pins:
  - the import direction (leaf, then module; never the launcher);
  - identity for both modules in both orders;
  - the hand-back's default being the leaf's and the launcher's object;
  - every reader reference going through the launcher (rule 21);
  - the hand-back called by the launcher's name, and no entry point called
    past it here;
  - the wrappers asking a patched controller: a disabled presentation is
    neither reconnected nor inspected, a failure is reported and never
    raised, an unreadable window is not attached;
  - slot titles from the launcher's patched titles;
  - the hand-back writing nothing without the bridge's descriptor, and with
    one (a pipe this test owns) writing the payload built from the
    launcher's patched pane launcher, titles and schema.

  12 of 12 mutations are killed, serially and on the clean tree. One kill
  printed no reason in the runner's summary; applied alone, the fixture's own
  `RuntimeError` escaped the wrapper, which is the property under test.
- **Green and identical to the baseline:** `role_identity_cutover_boundary_test`, `team_launcher_upgrade_cutover_test`, `switchyard_new_opens_its_window_test`, `team_launcher_generated_layout_upgrade_test`, `team_launcher_desktop_handoff_titles_test`, `operator_display_recovery_test`, `display_attach_focus_test`, `display_recovery_live_tmux_test`, `legacy_presentation_launch_test`, `legacy_presentation_migration_test`, `presentation_slot_contraction_test`, `presentation_label_schema_test`, `syrd_66_cross_account_presentation_test`, `team_launcher_presentation_titles_test`, `tenant_control_handoff_eof_test`, `resume_provision_desktop_test`, `team_launcher_identity_cutover_e2e_test`, `team_launcher_cutover_failure_propagation_test`, `viewer_builds_all_six_panes_test`, `viewer_landscape_layout_test`, `team_launcher_stop_reload_test`, `role_sessions_boundary_test`, `role_pane_entry_boundary_test`, `tmux_session_argv_boundary_test`, `tmux_viewer_boundary_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | presentation window recovery | 19 | 2 | |
  | env config | 24 | 2 | |
  | switchyard commands | 10 | 2 | |
  | desktop layout path | 8 | 4 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | presentation | 11 | 5 | |
  | pane paths | 9 | 2 | |
  | layout upgrade commands | 12 | 8 | |
  | unprivileged presentation | 7 | 4 | |
  | tenant stop presentation window | 5 | 1 | |
  | desktop access | 3 | 1 | |
  | viewer | 21 | 5 | |
  | privileged front door | 36 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **`tenant_control_bridge_e2e`** has no `test_` functions, so it was compared
  by its output. It is red on both trees, identically. Its namespace child ran
  8 of its 12 cases, all ok on both. The ninth re-executes Python as a sandbox
  account, which cannot import a tree under this user's home, and the loop
  stops there. Run alone in the suite's own namespace setup, the hand-off
  case fails identically on both trees at the same assertion. It drives a
  stand-in launcher that writes a fixed payload, so it does not reach the
  moved code.
- **Stubs:** the rule 19 tmux stub and the refusing provider and service
  stubs. No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains both modules and loads them from the
  release root. `reconnect_presentation`, the hand-back and
  `LAYOUT_MODE_SEPARATE` are the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`65f094a`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 23,099 lines) | 2 (`presentation_reconnect.py` 213, `layout_modes.py` 19) |
| Where it sits | 6 regions, lines 832–835, 1878–1932, 3790–3790, 3821–3833, 3874–3914, 18380–18427 | one module and its leaf |
| moved `def`/`class` in `team_launcher.py` | 10 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12b,
the desktop hand-off half. Seeded from `complete_desktop_presentation`,
`launch_presentation_terminal`, `validated_presentation_handoff` and
`presentation_pane_program_problem`, its closure is 20 definitions /
535 lines. `complete_desktop_presentation` is rebound on
4 test lines. `presentation_pane_program_problem` is read by
`presentation_controller`, and `PRESENTATION_HANDOFF_SCHEMA` by the module
this slice created. The closure includes the owned-directory walk and the
crossing desktop-layout writers, so the privileged and no-follow guards are
in scope. It may need a bounded split.

### SYRD-317 (slice 12b): desktop presentation hand-off

Re-measured on `1e7776e`, with the call graph regenerated on that tree. The
design was posted **before** any edit. The seeded closure is exactly
20 definitions, and it holds two responsibilities. As the ticket
allowed, it was split within this candidate; both halves moved unchanged.
- **`scripts/desktop_presentation.py`** (508 lines, 17 definitions): the
  desktop account's half of the hand-off.
  - Terminal choice: the `PRESENTATION_TERMINALS` allowlist with
    `TERMINAL_STAYS`/`TERMINAL_RETURNS`, `available_presentation_terminal`,
    `missing_terminal_refusal`, `terminal_launch_args` and
    `launch_presentation_terminal`.
  - What crosses, and its checks: `PRESENTATION_HANDOFF_SCHEMA`, the title
    bounds, `presentation_handoff_path`, `presentation_title_problem`,
    `presentation_pane_program_problem` and `validated_presentation_handoff`.
  - Completing it: `complete_desktop_presentation`,
    `_tenant_has_desktop_access` and `_registered_project_name`.
- **`scripts/desktop_layout_writer.py`** (199 lines, 3 definitions):
  writing a layout into a desktop account's home. It holds root's crossing
  write through the owner-bound, no-follow directory walk:
  `_open_owned_directory_chain`, `_write_crossing_desktop_layout` and
  `write_desktop_layout`. It is kept apart because it is the privileged half,
  and it has readers beyond the desktop half.

The moved code came from 6 regions of the baseline: 1623–1692, 1888–1954, 3732–3739, 3752–3867, 3907–4068, 21895–22046.

**Boundaries.**
- **Rebound seams called from inside the moved code:**
  `complete_desktop_presentation` (4 lines), `write_desktop_layout` (5),
  `_tenant_has_desktop_access` (6) and `available_presentation_terminal` (4).
  Every such call is `launcher.X` at call time, including the desktop half's
  call into the writer. The bridge exec still calls
  `complete_desktop_presentation` by the launcher's name.
- **Launcher names read at call time:** 20, of which nine are rebound by
  suites (`current_user_name` 99 lines, `uid_for_user` 68,
  `launch_konsole_window` 26, among others).
- **External readers**, each reference through the launcher (rule 21):
  - `presentation_controller`: `presentation_pane_program_problem` and
    `write_desktop_layout`;
  - `presentation_reconnect`: `PRESENTATION_HANDOFF_SCHEMA`;
  - `upstream_report`: `_open_owned_directory_chain`.

  All 20 names are re-exported.
- **Imports:** the desktop half imports the layout modes from the existing
  leaf, and neither module imports the other or the launcher at its top.
- **Rules:** no moved default names a launcher object (rule 6); no moved
  constant is rebound (rule 20); no moved function binds an import alias
  (rule 18).

**Guards.** `team_launcher_smoke_layout_test`'s "no `getattr(runner,
"process_launcher"`" check now reads `desktop_presentation.py` too, where the
terminal's `process_launcher` now lives, and asserts that it is there to be
read. Nothing else that scans source matched moved code.

**Evidence.**
- **AST proof:** it holds for both modules, including step 6. Step 5 caught a
  missing `import os` in the first draft of the desktop half's header, before
  anything ran.
- **New boundary test:** `tests/desktop_presentation_boundary_test.py` has
  34 checks.
  - The import direction, identity in both orders, and rule 21 for every
    reader.
  - The rebound seams, never called bare in either module.
  - **Owned fixtures instead of the bridge's.** `complete_desktop_presentation`
    is driven from a hand-off file in a directory the test owns, with
    `/usr/bin/true` as the real root-owned pane program:
    - a good hand-off is consumed once;
    - its layout goes to the launcher's patched writer, and the launcher's
      patched Konsole launch opens it;
    - another schema is refused before anything is written;
    - a missing hand-off is a fault only when the launcher's patched answer
      says the tenant has a window;
    - the viewer layout starts one terminal through a fake process launcher.
  - **The no-follow walk** runs in a home the test owns:
    - missing components are created 0700;
    - a symlinked component is refused, and nothing is made through it;
    - a home another uid owns is refused.
  - **The writer** refuses to cross without root, and writes the caller's own
    layout through the launcher's patched private writer.

  15 of 15 mutations are killed, serially and on the clean tree. Each
  kill's own last error line was read (rule 23).
- **Green and identical to the baseline:** `presentation_reconnect_boundary_test`, `legacy_presentation_migration_test`, `operator_display_recovery_test`, `legacy_presentation_launch_test`, `team_launcher_smoke_layout_test`, `team_launcher_desktop_handoff_titles_test`, `team_launcher_pane_commands_test`, `team_launcher_presentation_titles_test`, `team_launcher_session_sources_test`, `upstream_report_boundary_test`, `tenant_control_handoff_eof_test`, `syrd_66_cross_account_presentation_test`, `display_attach_focus_test`, `privileged_helper_no_follow_test`, `privileged_plan_read_no_follow_test`, `host_desktop_approval_test`, `switchyard_new_opens_its_window_test`, `tmux_session_argv_boundary_test`, `role_pane_entry_boundary_test`, `upstream_report_credential_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | presentation window recovery | 19 | 2 | |
  | layout upgrade commands | 12 | 8 | |
  | unprivileged presentation | 7 | 4 | |
  | desktop layout path | 8 | 4 | |
  | project role prompts | 6 | 2 | |
  | project artifacts | 10 | 6 | |
  | control repo | 7 | 3 | |
  | konsole | 14 | 2 | |
  | switchyard commands | 10 | 2 | |
  | presentation | 11 | 5 | |
  | tenant stop presentation window | 5 | 1 | |
  | session report | 12 | 1 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | tenant resume cli promotion | 13 | 1 | |
  | reload attach | 12 | 1 | |
  | pane paths | 9 | 2 | |
  | resume detached | 8 | 1 | |
  | freshness | 15 | 1 | |
  | switchyard new prompts | 11 | 3 | |
  | desktop access | 3 | 1 | |
  | privileged front door | 36 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **`tenant_control_bridge_e2e`** has no `test_` functions and is compared by
  its output: red on both trees with identical output; its namespace child passes the same 8 of its 12 cases on both, then stops at the same environment limit (a sandbox account cannot import a tree under this user's home). As recorded in SYRD-316, its hand-off case
  drives a stand-in launcher and does not reach the moved code; the owned
  fixtures above are the evidence for it.
- **`team_launcher_desktop_policy_owner`** has no `test_` functions either.
  Its output is identical on both trees: its namespace child refuses
  `/etc/switchyard/provision` as owned by uid 65534 before any case runs. It
  names none of the moved code.
- **Stubs:** the rule 19 tmux stub and the refusing provider and service
  stubs. No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains both modules and loads them from the
  release root. `complete_desktop_presentation`, `write_desktop_layout` and
  `_open_owned_directory_chain` are the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`1e7776e`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 22,940 lines) | 2 (`desktop_presentation.py` 508, `desktop_layout_writer.py` 199) |
| Where it sits | 6 regions, lines 1623–1692, 1888–1954, 3732–3739, 3752–3867, 3907–4068, 21895–22046 | one module per half |
| moved `def`/`class` in `team_launcher.py` | 20 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12c,
the Konsole window launch and the GUI session environment. Seeded from
`launch_konsole_window`, `konsole_launch_args` and `_gui_launch_prefix`, its
closure is 20 definitions / 273 lines.
`launch_konsole_window` is rebound on 26 test lines and read by
`presentation_controller` and `desktop_presentation`. `desktop_presentation`
also reads `_gui_launch_prefix`, `gui_program_path`,
`_make_konsole_log_readable` and `_refusal_command` through the launcher.

### SYRD-318 (slice 12c): Konsole window launch and GUI session environment

Re-measured on `8b5a332`, with the call graph regenerated on that tree. The
design was posted **before** any edit. The closure is exactly 20
definitions, one responsibility -- starting a window in the desktop
account's GUI session -- and moved unchanged into
**`scripts/gui_window_launch.py`** (389 lines):
- **Crossing and environment:** the Wayland variable names,
  `normalize_wayland_display`, `gui_privilege_drop_args`,
  `_gui_launch_prefix`, `GUI_ENVIRONMENT_ALLOWLIST`, `gui_environment_args`,
  `_gui_runtime_dir`, `gui_program_path` and `_refusal_command`.
- **Konsole:** the window-title default (`KONSOLE_DEFAULTS_NAME`,
  `KONSOLE_WINDOW_TITLE_DEFAULTS`, `FALLBACK_XDG_CONFIG_DIRS`,
  `write_konsole_config_defaults`), `konsole_launch_args`,
  `launch_konsole_window`, `_make_konsole_log_readable` and
  `_print_konsole_early_exit`.

The moved code came from 4 regions of the baseline: 855–858, 1476–1705, 1824–1896, 1908–1920. The
extractor had spaced out two runs of adjacent constants; they were put back
exactly as the baseline wrote them (checked against the baseline text), and
the proof re-run.

**Boundaries.**
- **Seams (rule 24):**
  - `launch_konsole_window` is rebound by seven suites. The launcher still
    calls it by its own name from `launch_project` and
    `replace_presentation_window_command`.
  - `_gui_launch_prefix`, `gui_program_path` and `_make_konsole_log_readable`
    are rebound on the launcher by SYRD-317's boundary test. The moved code
    calls all three as `launcher.X`, as the baseline's callers read the
    launcher's globals.
- **Call time:** `uid_for_user`, `_gui_home`, `DEFAULT_PANE_BASE_PATH`,
  `default_gui_user`, `GUI_USER_ENV` and `_int_env` stay in the launcher and
  are read there. So is `_env_first`: it lives in the environment leaf, but
  suites rebind it on the launcher and the baseline read the launcher's
  global.
- **External readers:** `presentation_controller` (`launch_konsole_window`)
  and `desktop_presentation` (`launch_konsole_window`, `_gui_launch_prefix`,
  `gui_program_path`, `_make_konsole_log_readable`, `_refusal_command`), every
  reference through the launcher (rule 21). All 20 names are
  re-exported.
- **Rules:** no moved default names a launcher object (rule 6); no moved
  constant is rebound (rule 20); no moved function binds an import alias
  (rule 18).

**Guards.** `team_launcher_smoke_layout_test`'s "no `getattr(runner,
"process_launcher"`" check now also reads `gui_window_launch.py`, and asserts
that `process_launcher` is there to be read. The scripts-wide tmux, git and
agent-CLI scans already cover the new module. No other source guard reads
moved text.

**Evidence.**
- **AST proof:** it holds, including step 6, before and after the constants'
  spacing was restored.
- **New boundary test:** `tests/gui_window_launch_boundary_test.py` has
  36 checks, on owned fixtures only.
  - The import direction, identity in both orders, and rule 21.
  - The four seams, and `_env_first`, never reached past the launcher.
  - The crossing, with the effective uid passed in and the launcher's
    account lookups patched:
    - root crosses with `sudo -u <account> -H --`;
    - it refuses root, a uid-0 account and an unknown account;
    - an unprivileged caller does not cross;
    - the crossing carries exactly the `env -i` allowlist.
  - The prefix asks the launcher's rebound `_env_first` for the host
    display.
  - The Konsole argv and the refusal command come from the launcher's
    patched seams.
  - `launch_konsole_window`, with a fake process launcher:
    - Konsole starts in its own session;
    - its log goes through the launcher's patched helper;
    - an early exit returns its status with its output;
    - a refusal runs through the runner;
    - the title default sits beside the layout with the layout's mode.
  - The log is handed to the invoking account.

  14 of 14 mutations are killed, serially and on the clean tree. Each
  kill's own last error line was read (rule 23).
- **Green and identical to the baseline:** `agent_cli_boundary_test`, `desktop_presentation_boundary_test`, `host_desktop_approval_test`, `legacy_presentation_launch_test`, `legacy_presentation_migration_test`, `presentation_reconnect_boundary_test`, `switchyard_new_opens_its_window_test`, `team_launcher_desktop_handoff_titles_test`, `team_launcher_presentation_titles_test`, `team_launcher_smoke_layout_test`, `team_launcher_tmux_panes_test`, `operator_display_recovery_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | presentation window recovery | 19 | 2 | |
  | privileged front door | 36 | 1 | |
  | control repo | 7 | 3 | |
  | desktop layout path | 8 | 4 | |
  | env config | 24 | 2 | |
  | konsole | 14 | 2 | |
  | layout upgrade commands | 12 | 8 | |
  | resume detached | 8 | 1 | |
  | switchyard commands | 10 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | presentation | 11 | 5 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | tenant stop presentation window | 5 | 1 | |
  | pane paths | 9 | 2 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **Suites with no `test_` functions**, compared by their whole output, identical on both trees and naming none of the moved code: `tenant_control_bridge_e2e` (the same 8 of 12 namespace cases pass, then the same environment stop) and `team_launcher_desktop_policy_owner` (refused at namespace setup before any case). Neither is claimed as coverage; the owned fixtures above are the evidence.
- **Stubs:** the rule 19 tmux stub and the refusing provider and service
  stubs. No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. `launch_konsole_window`, `gui_privilege_drop_args` and
  `GUI_ENVIRONMENT_ALLOWLIST` are the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`8b5a332`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 22,357 lines) | 1 (`gui_window_launch.py`, 389) |
| Where it sits | 4 regions, lines 855–858, 1476–1705, 1824–1896, 1908–1920 | one file |
| moved `def`/`class` in `team_launcher.py` | 20 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12d,
the presentation layout files.
- **Size:** seeded from `materialize_layout`, `default_layout_output_path`,
  `desktop_presentation_layout_path`, `desktop_layout_destination_problem`,
  `ensure_layout_output_owner` and `desktop_state_dir`, the closure is
  10 definitions / 183 lines.
- **Seams:** `materialize_layout` is rebound 2x and
  `default_layout_output_path` 3x.
- **Readers:** `presentation_controller`, `role_runtime`,
  `desktop_presentation`, `desktop_layout_writer` and `presentation_reconnect`.
- **Shared names:** it pulls in `pane_split_title`, `role_display_name` and
  `inert_pane_command`, which other modules also read.

After it, the desktop-account and layout-mode detection
(`presentation_gui_user`, `detected_invoking_desktop`,
`resolve_layout_mode`) is 5 definitions / 106 lines.
