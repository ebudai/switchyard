# SYRD-272: source size inventory and launcher extraction plan

This document serves the SYRD-272 parent. Each extraction child updates it:
the before/after table, the slice log, and the plan's next entry. SYRD-286 was
the first slice, SYRD-287 the second, SYRD-288 the third, SYRD-289
the fourth, SYRD-290 the fifth, SYRD-291 the sixth, SYRD-292
slice 6a, SYRD-293 slice 6b, SYRD-294 slice 6c,
SYRD-295 slice 7a, SYRD-296 slice 7b, SYRD-297 slice 7c-1, SYRD-298
slice 7c-2a, SYRD-299 slice 7c-2b, SYRD-300 slice 7c-3, SYRD-301
slice 8a, SYRD-302 slice 8b, SYRD-303 slice 9a, SYRD-304 slice 9b, SYRD-305 slice 10a, SYRD-306 slice 10b, SYRD-308 slice 10c, SYRD-309
slice 11a, SYRD-310 slice 11b, SYRD-311 slice 11c, SYRD-312 slice 11d, SYRD-313 slice 11e, SYRD-314 slice 11f, SYRD-316 slice 12a, SYRD-317 slice 12b, SYRD-318 slice 12c, SYRD-319 slice 12d, SYRD-320 slice 12e, SYRD-321 slice 12f, SYRD-323 slice 12g, SYRD-324 (a preservation follow-up), SYRD-325 slice 12h, SYRD-326 slice 12i, SYRD-327 slice 12j, SYRD-328 slice 12k, SYRD-329 slice 12l, SYRD-330 slice 12m, SYRD-331 slice 12n, SYRD-332 slice 12o, SYRD-333 slice 12p, SYRD-334 slice 12q, SYRD-335 slice 12r, SYRD-337 slice 12s, SYRD-339 slice 12t, SYRD-340 slice 12u, SYRD-341 slice 12v, SYRD-342 slice 12w, SYRD-344 slice 12x, SYRD-345 slice 12y, SYRD-346 slice 12z, SYRD-347 slice 12aa, SYRD-348 slice 12ab, SYRD-349 slice 12ac, SYRD-350 slice 12ad, SYRD-351 slice 12ae, SYRD-352 slice 13a, SYRD-353 slice 13b, SYRD-354 slice 13c, SYRD-355 slice 14a, SYRD-356 slice 14b and SYRD-357 slice 14c.

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
- SYRD-318 was integrated as `4606264ba98e74b067535a6982c2988bde9bbce3`, which
  is SYRD-319's baseline.
- SYRD-319 was integrated as `3c0346aecabe94477a959ffbb40561ce616dcf09`, which
  is SYRD-320's baseline.
- SYRD-320 was integrated as `f489f32788fcd78d66221ef697fb1593fa429d84`, which
  is SYRD-321's baseline.
- SYRD-321 was integrated as `82f761623685e8b9f5dd3766f4d714ddba116384`, which
  is SYRD-323's baseline.
- SYRD-323 was integrated as `3d7d180b5399d8cae137699ecaedaa96255babdd`, and
  SYRD-324 on it as `d7d4a0a9936ac0bd1d8c7ee7f2acbe53d5e6537d`, which is SYRD-325's baseline.
- SYRD-325 was integrated as `c1766678b0027e4cc8dc7ba7ed217a4feb86dfb1`, which
  is SYRD-326's baseline.
- SYRD-326 was integrated as `6219de05121dbab0b8d167b0ae905d36d6b0de1d`, which
  is SYRD-327's baseline.
- SYRD-327 was integrated as `0a82149c444380f1cf3e397b645d4201a756b2e8`, which
  is SYRD-328's baseline.
- SYRD-328 was integrated as `f722260b336a5969ce0cb55ed77244ea826f2b27`, which
  is SYRD-329's baseline.
- SYRD-329 was integrated as `2afb97e42c0c884ef3ba5b96e25db228f217fefe`, which
  is SYRD-330's baseline.
- SYRD-330 was integrated as `823f5f0e5d99d6efd2ff14339373a62d614a407f`, which
  is SYRD-331's baseline.
- SYRD-331 was integrated as `e729f9ade4f29405063164d3cb4c67c33b88d787`, which
  is SYRD-332's baseline.
- SYRD-332 was integrated as `a6a1d52af66555d866b19e643031100f4b3d3a4a`, which
  is SYRD-333's baseline.
- SYRD-333 was integrated as `f7196926a83dd7cd4c7ed94b7c24e15081c498ec`, which
  is SYRD-334's baseline.
- SYRD-334 was integrated as `1aa7e713763024dd5fb868ab1d647a35bc6bb67a`, which
  is SYRD-335's baseline.
- SYRD-335 was integrated as `ad85b30b9a374fb9ae17c1e3d1ade71b3734dbfc`, which
  is SYRD-337's baseline.
- SYRD-337 was integrated as `6b483e988dc9d0e1b15368f71aa2fa998ff994ed`, which
  is SYRD-339's baseline.
- SYRD-339 was integrated as `d274fc5d5dc213ab651695d29049e9d6d6df7a50`, which
  is SYRD-340's baseline.
- SYRD-340 was integrated as `c83111a8662c7a589e9e1876a0fe23549e68ef43`, which
  is SYRD-341's baseline.
- SYRD-341 was integrated as `3ef1d428614bede28da942de101145378dc15c35`, which
  is SYRD-342's baseline.
- SYRD-342 was integrated as `f0219303544fd4cf16a36e984f246f3953d0d6ef`, which
  is SYRD-344's baseline.
- SYRD-344 was integrated as `0bc27610de484e66dfbb7a72ee31a89629146fe3`, which
  is SYRD-345's baseline.
- SYRD-345 was integrated as `ce84e93eb93d60bae2c04ebf5c6cc1018ef917c1`, which
  is SYRD-346's baseline.
- SYRD-346 was integrated as `788d2c0b3a8b2eb42b8fd63d32b4983ded17ba2b`, which
  is SYRD-347's baseline.
- SYRD-347 was integrated as `b18852551800f905398840b9464168cbcc21e46d`, which
  is SYRD-348's baseline.
- SYRD-348 was integrated as `0ac170e30025cfb35a8396a1132da1f6b20496b7`, which
  is SYRD-349's baseline.
- SYRD-349 was integrated as `05ea70a9b4fd7a4678c8f68012c828a8ddf76b8e`, which
  is SYRD-350's baseline.
- SYRD-350 was integrated as `7dcb5d478fb4f93499101f7391bbb9742798f45b`, which
  is SYRD-351's baseline.
- SYRD-351 was integrated as `3328f0e4faca96ee68769abd665b226fbc6c8cb4`, which
  is SYRD-352's baseline.
- SYRD-352 was integrated as `e921d47770a5b530cbd9deb010047c046c09693c`, which
  is SYRD-353's baseline.
- SYRD-353 was integrated as `d1116a8e88083f35c238f26cca82a5150ca047e7`, which
  is SYRD-354's baseline.
- SYRD-354 was integrated as `16629f8422a5da5599d5ae349855754e84cfb58c`, which
  is SYRD-355's baseline.
- SYRD-355 was integrated as `5b97e18a635d78c99d955940aedd57a76d9bd5dc`, which
  is SYRD-356's baseline.
- SYRD-356 was integrated as `424064e7e4319d5f031f3dd8761da46e582f464f`, which
  is SYRD-357's baseline.
- Soft limit: 1,250 lines, advisory. The pre-commit warning is unchanged.

## 1. Tracked source inventory

These are the tracked non-test files over the soft limit at the baseline,
followed by the parent's list. Each "after" column is that child's candidate.

| File | Baseline | After SYRD-286 | After SYRD-287 | After SYRD-288 | After SYRD-289 | After SYRD-290 | After SYRD-291 | After SYRD-292 | After SYRD-293 | After SYRD-294 | After SYRD-295 | After SYRD-296 | After SYRD-297 | After SYRD-298 | After SYRD-299 | After SYRD-300 | After SYRD-301 | After SYRD-302 | After SYRD-303 | After SYRD-304 | After SYRD-305 | After SYRD-306 | After SYRD-308 | After SYRD-309 | After SYRD-310 | After SYRD-311 | After SYRD-312 | After SYRD-313 | After SYRD-314 | After SYRD-316 | After SYRD-317 | After SYRD-318 | After SYRD-319 | After SYRD-320 | After SYRD-321 | After SYRD-323 | After SYRD-325 | After SYRD-326 | After SYRD-327 | After SYRD-328 | After SYRD-329 | After SYRD-330 | After SYRD-331 | After SYRD-332 | After SYRD-333 | After SYRD-334 | After SYRD-335 | After SYRD-337 | After SYRD-339 | After SYRD-340 | After SYRD-341 | After SYRD-342 | After SYRD-344 | After SYRD-345 | After SYRD-346 | After SYRD-347 | After SYRD-348 | After SYRD-349 | After SYRD-350 | After SYRD-351 | After SYRD-352 | After SYRD-353 | After SYRD-354 | After SYRD-355 | After SYRD-356 | After SYRD-357 | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `scripts/team_launcher.py` | 38,527 | 37,808 | 36,670 | 36,379 | 35,730 | 34,987 | 34,365 | 33,826 | 33,573 | 33,377 | 32,030 | 31,654 | 31,495 | 31,209 | 30,284 | 29,664 | 28,942 | 28,485 | 27,498 | 26,888 | 26,225 | 25,822 | 24,899 | 24,575 | 24,100 | 23,997 | 23,804 | 23,198 | 23,099 | 22,940 | 22,357 | 22,053 | 21,864 | 21,722 | 21,414 | 21,147 | 20,801 | 20,475 | 20,397 | 20,198 | 20,096 | 19,936 | 19,885 | 19,840 | 19,820 | 19,782 | 19,729 | 19,707 | 19,706 | 19,650 | 19,600 | 19,411 | 19,392 | 18,818 | 18,683 | 18,462 | 18,362 | 18,306 | 18,180 | 18,096 | 17,926 | 17,423 | 17,104 | 16,660 | 16,552 | 16,467 | Plan in §3. |
| `scripts/worker_pool_command.py` | — | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | New in SYRD-286. |
| `scripts/role_credentials.py` | — | — | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | New in SYRD-287. |
| `scripts/agy_credential.py` | — | — | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | New in SYRD-287. |
| `scripts/upstream_report.py` | — | — | — | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | New in SYRD-288. |
| `scripts/host_accounts.py` | — | — | — | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 41 | 41 | 41 | New in SYRD-288: a dependency-free leaf. SYRD-355 moved `local_account_exists` here, for `add_project_role_command`'s default. |
| `scripts/project_onboarding.py` | — | — | — | — | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | New in SYRD-289. |
| `scripts/role_command.py` | — | — | — | — | — | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | 212 | New in SYRD-290. |
| `scripts/model_validation.py` | — | — | — | — | — | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | 694 | New in SYRD-290. |
| `scripts/project_worktrees.py` | — | — | — | — | — | — | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 742 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | 765 | New in SYRD-291. SYRD-333 appended the launch's worktree preparation. |
| `scripts/launcher_checkout.py` | — | — | — | — | — | — | — | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | 632 | New in SYRD-292. |
| `scripts/owner_git.py` | — | — | — | — | — | — | — | — | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | New in SYRD-293. |
| `scripts/pane_hooks.py` | — | — | — | — | — | — | — | — | — | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | New in SYRD-294. |
| `scripts/agent_cli_discovery.py` | — | — | — | — | — | — | — | — | — | — | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | 435 | New in SYRD-295. |
| `scripts/agent_cli_promotion.py` | — | — | — | — | — | — | — | — | — | — | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | 1,099 | New in SYRD-295. |
| `scripts/first_run_setup.py` | — | — | — | — | — | — | — | — | — | — | — | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | 443 | New in SYRD-296. |
| `scripts/provider_auth_status.py` | — | — | — | — | — | — | — | — | — | — | — | — | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | 223 | New in SYRD-297. |
| `scripts/provider_screen.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | 344 | New in SYRD-298: a leaf. |
| `scripts/provider_session.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | 1,022 | New in SYRD-299: imports only `provider_screen` at its top. |
| `scripts/first_run_auth.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | 703 | New in SYRD-300: imports `provider_session` and `runtime_catalog` at its top, never the launcher. |
| `scripts/project_status.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | New in SYRD-301: imports nothing of Switchyard's at its top. |
| `scripts/board_services.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | 557 | New in SYRD-302: imports nothing of Switchyard's at its top. |
| `scripts/workflow_adoption.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | 1,068 | New in SYRD-303: imports nothing of Switchyard's at its top. |
| `scripts/pane_rebind.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | 677 | New in SYRD-304: imports nothing of Switchyard's at its top. |
| `scripts/role_account_migration.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | 747 | New in SYRD-305: imports nothing of Switchyard's at its top. |
| `scripts/provider_runtime_state.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | 448 | New in SYRD-306: imports only the `account_drop` leaf at its top. |
| `scripts/account_drop.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | 57 | New in SYRD-306: a stdlib-only leaf (the privilege drop). |
| `scripts/role_identity_cutover.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | 1,012 | New in SYRD-308: imports only the `release_refs` leaf at its top. |
| `scripts/release_refs.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | 15 | New in SYRD-308: a one-constant leaf (the default deploy ref). |
| `scripts/tmux_viewer.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | 404 | New in SYRD-309: imports nothing of Switchyard's at its top. |
| `scripts/session_records.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | 572 | New in SYRD-310: imports nothing of Switchyard's at its top. |
| `scripts/session_paths.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | 177 | New in SYRD-311: imports nothing of Switchyard's at its top. |
| `scripts/role_sessions.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | 252 | New in SYRD-312: imports nothing of Switchyard's at its top. |
| `scripts/provider_resume.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | 360 | New in SYRD-313: imports nothing of Switchyard's at its top. |
| `scripts/role_pane_entry.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | New in SYRD-313: imports only `launcher_env` and `provider_resume` at its top. |
| `scripts/launcher_env.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | 35 | New in SYRD-313: a stdlib-only leaf (`_env_first`, `DEFAULT_PANE_STATE_DIR`). |
| `scripts/tmux_session_argv.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | 152 | New in SYRD-314: imports nothing of Switchyard's at its top. |
| `scripts/presentation_reconnect.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | 213 | New in SYRD-316: imports only `layout_modes` at its top. |
| `scripts/layout_modes.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | 19 | New in SYRD-316: a stdlib-only leaf (the four `LAYOUT_MODE_*` constants). |
| `scripts/desktop_presentation.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | 508 | New in SYRD-317: imports only `layout_modes` at its top. |
| `scripts/desktop_layout_writer.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | 199 | New in SYRD-317: imports nothing of Switchyard's at its top; root's no-follow crossing write. |
| `scripts/gui_window_launch.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | 389 | New in SYRD-318: imports nothing of Switchyard's at its top; the root→desktop crossing and the Konsole window. |
| `scripts/presentation_layout_files.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 256 | 256 | 256 | 256 | 256 | 256 | 256 | 256 | 256 | 256 | 256 | 256 | 256 | 256 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | 323 | New in SYRD-319: imports nothing of Switchyard's at its top; the layout file's path, owner and content. SYRD-335 appended Konsole layout-command quoting. |
| `scripts/desktop_detection.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | 195 | New in SYRD-320: imports only `layout_modes` at its top; whose desktop, and which layout. |
| `scripts/presentation_windows.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | 381 | New in SYRD-321: imports nothing of Switchyard's at its top; holds both copies of `presentation_window_processes`, in order. |
| `scripts/display_bridge.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | 320 | New in SYRD-323: imports nothing of Switchyard's at its top; the display bridge's verdict and install. |
| `scripts/desktop_approval.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | 407 | New in SYRD-325: imports nothing of Switchyard's at its top; the host's standing desktop approval and `approve-desktop`. |
| `scripts/desktop_policy.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | 384 | New in SYRD-326: imports nothing of Switchyard's at its top; deciding a project's desktop policy. |
| `scripts/project_desktop.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | 122 | New in SYRD-327: imports nothing of Switchyard's at its top; verifying, installing and recording a project's desktop policy. |
| `scripts/legacy_presentation.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | 255 | New in SYRD-328: imports nothing of Switchyard's at its top; legacy presentation migration and the presentation-config wrappers. |
| `scripts/presentation_window_replacement.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | 140 | New in SYRD-329: imports only the layout modes at its top; `switchyard replace-window`. |
| `scripts/live_role_runtime.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 208 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | 262 | New in SYRD-330: imports nothing of Switchyard's at its top; live CLI and model detection and the stale-provider-runtime drop. SYRD-331 appended the reload's config sync. |
| `scripts/owner_state_dirs.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | 84 | New in SYRD-332: imports nothing of Switchyard's at its top; the owner's session and pane-state directories. |
| `scripts/board_authority_preflight.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | 65 | New in SYRD-334: never imports or names the launcher; the board-authority launch preflight. |
| `scripts/pane_launcher_preflight.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | 52 | New in SYRD-337: imports nothing of Switchyard's at its top; the pane-launcher executable preflight. |
| `scripts/launch_phases.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 87 | 229 | 326 | 597 | 665 | 665 | 665 | 665 | 665 | 665 | 665 | 665 | 665 | 665 | 665 | 665 | 665 | 665 | New in SYRD-339: imports nothing of Switchyard's at its top; `launch_project`'s phases, starting with P3 (runners, owner delegation and paths). SYRD-340 appended P5 (pre-launch preparation). SYRD-341 appended P6 (layout, plan and dry run). SYRD-342 appended P7+P8 (worker start and presentation). SYRD-344 appended P9 (the launch's report and records). |
| `scripts/github_identity.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 629 | 629 | 629 | 629 | 629 | 629 | 629 | 629 | 629 | 629 | 629 | 629 | 629 | New in SYRD-345: imports nothing of Switchyard's at its top; the owner's GitHub identity status and the set and clear repairs, with their plan writer and key checks. |
| `scripts/upgrade_phases.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 222 | 516 | 654 | 754 | 942 | 1094 | 1094 | 1094 | 1094 | 1094 | 1094 | 1094 | New in SYRD-346: imports nothing of Switchyard's at its top; `upgrade_project_command`'s phases, starting with U4 (role tooling: preview and privileged staging). SYRD-347 appended U5 (identity and accounts). SYRD-348 appended U6 (finish). SYRD-349 appended U3 (generated artifacts and the upstream report). SYRD-350 appended U2 (manager, desktop and repatriation). SYRD-351 appended U1 (desktop decision and source pinning). |
| `scripts/director_upgrade.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 204 | 204 | 204 | 204 | 204 | 204 | New in SYRD-352: imports nothing of Switchyard's at its top; `switchyard finish-upgrade`, the director's half of an upgrade. |
| `scripts/privileged_runtime_plan.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 585 | 585 | 585 | 585 | 585 | New in SYRD-353: imports nothing of Switchyard's at its top; root's runtime baseline and plan helpers: owner evidence, ownership repair, baseline reconstruction, the projection, current identities and privacy. |
| `scripts/runtime_artifact_refresh.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 365 | 365 | 365 | 365 | New in SYRD-354: imports nothing of Switchyard's at its top; the generated runtime artifact refresh and its four private helpers. |
| `scripts/project_role_add.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 498 | 498 | 498 | New in SYRD-355: imports nothing of Switchyard's but the `host_accounts` leaf at its top; `switchyard add-role`'s command and its nine helpers. |
| `scripts/project_vcs_close_role.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 151 | 151 | New in SYRD-356: imports nothing of Switchyard's at its top; `switchyard set-vcs-close-role`'s command and its three helpers. |
| `scripts/project_role_plan_support.py` | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | 142 | New in SYRD-357: imports nothing of Switchyard's at its top; the role-plan data and board-unit helpers `project_role_add` and `project_vcs_close_role` share. |
| `scripts/ticket_board/schema.sql` | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | Proposed exception: one DDL document applied whole. It is still reviewed as its own child. |
| `scripts/ticket_board/project_provision.py` | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | Parent list; needs a child. |
| `scripts/ticket_board/notify_listener.py` | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | Parent list; needs a child. |
| `scripts/presentation_controller.py` | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | Parent list; needs a child. |
| `scripts/ticket_board/app.py` | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | Parent list; needs a child. |
| `scripts/ticket_board/server.py` | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | Parent list; needs a child. |
| `scripts/ticket_board/migrations/pgu921_syrd11_declarative_workflow.sql` | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | Proposed exception: a migration is immutable history. |
| `scripts/ticket_board/frontend_script_core.py` | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | Parent list; generated front-end asset. Its boundary is the asset, not Python modules. |
| `scripts/ticket_board/write_client.py` | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | Parent list; needs a child. |
| `scripts/ticket-board-service.sh` | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | Parent list; a shell entry point. Split along its own subcommands. |
| `scripts/ticket_board/migrations/pgu528_workflow_rbac_config_authoritative.sql` | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | Proposed exception: migration. |
| `scripts/ticket_board/migrations/pgu589_depersonalize_user_role.sql` | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | Proposed exception: migration. |
| `deploy/SYRD-87-recover-syrd-runtime.sh` | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | Proposed exception: a one-off recovery packet kept as a record. |

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
25. **Measure a "shared" helper's references before giving it a leaf.** A
    helper other modules read is not thereby shared inside the launcher. In
    SYRD-319, `pane_split_title`, `role_display_name` and `inert_pane_command`
    looked shared; measured across all of `scripts/`, each had exactly one
    caller in the closure, and the other readers already went through the
    launcher. They moved with their caller, and no leaf was needed. A leaf is
    for a def-time default or a caller that must import the name at its top
    (rule 6), not for a name that is merely read elsewhere.
26. **A name defined twice moves with every copy, in order.** Callers look a
    name up when they run, so the last top-level copy is the one they reach.
    Moving one copy would leave the other to re-bind the name, and keeping the
    first in the launcher is not neutral either: the launcher's import sits at
    its top, and a later `def` of the same name would re-bind it to the dead
    copy. SYRD-321 moved both copies of `presentation_window_processes` in
    baseline order. The extractor's `all_copies` moves every copy, with spans
    and call-time aliases per node. The proof compares every copy in order,
    and it fails when the copies are swapped or either one is dropped.
27. **A name a function binds itself is never the launcher's.** A
    function-level import, a parameter or a local shadows the launcher's name
    of the same spelling, and the moved code must keep reading its own.
    SYRD-323's `display_bridge_state` imports `TENANT_CONTROL_ROOT` from
    `project_provision`. The launcher binds its own `TENANT_CONTROL_ROOT`, a
    different object that three suites rebind, and the extractor had rewritten
    the local read to `launcher.TENANT_CONTROL_ROOT` -- invisibly to proof
    step 1, which reads `launcher.X` back as `X`. The extractor now leaves
    every name a function binds untouched, and proof step 7 independently
    flags any `launcher.X` whose `X` the function binds itself.
    SYRD-324 corrected the one earlier site this found
    (`role_account_migration.render_role_account_migration`, from SYRD-305).
    It also made the scan scope-aware: parameters of every kind; assignment,
    for, with, except, walrus and comprehension targets; match captures;
    imports; nested definitions; and a `global` declaration un-binding a
    name. Nested functions and lambdas are scanned as their own scopes. It
    reports zero across every module this refactor created.

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
| 12d | **Presentation layout files** (SYRD-319) | 256 (10 defs) | the three helpers thought shared each had one caller in the closure, so no leaf; four rebound seams (two from suites, two from our boundary tests' keyword rebinding) reached through the launcher |
| 12e | **Desktop account and layout-mode detection** (SYRD-320) | 195 (9 defs) | layout modes from their leaf; `detected_invoking_desktop`, `presentation_gui_user`, `default_gui_user`, `_env_first` and `current_user_name` reached through the launcher; a reader the ticket did not list (`agy_credential`) found and tested |
| 12f | **Presentation window processes and closing** (SYRD-321) | 381 (11 names, 12 nodes) | both copies of `presentation_window_processes` moved in baseline order, so the later still binds; the SYRD-43 root-window check's baseline defect preserved and reported |
| 12g | **Display bridge setup and readiness** (SYRD-323) | 320 (6 defs) | the function-level `project_provision` imports kept the function's own (rule 27); the extractor fixed and proof step 7 added before any edit; `display_bridge_launch_problem` and `display_bridge_state` reached through the launcher |
| 12h | **Desktop approval command and records** (SYRD-325) | 407 (10 defs) | the operator resolver and desktop sessions stay the functions' own imports (rule 27); the user-name check, JSON writer, privileged uid and no-follow reader read from the launcher at call time |
| 12i | **Desktop policy resolution** (SYRD-326) | 384 (11 defs) | nothing in the closure rebound; the approval records, installer, loader, prompts and current user read from the launcher at call time; the `desktop_access` helpers stay the functions' own imports (rule 27) |
| 12j | **Project desktop preparation and install** (SYRD-327) | 122 (3 defs) | both entry points stay launcher seams, `configure_project_desktop` reaching `prepare_project_desktop` through the launcher; the presentation-config pair measured and declined as a different responsibility |
| 12k | **Legacy presentation migration and config** (SYRD-328) | 255 (8 defs) | `_gui_home` stayed in the launcher (shared, rebound in 7 suites) and is read through it; two neighbour guards widened to follow the moved callers |
| 12l | **Presentation window replacement** (SYRD-329) | 140 (1 def) | `switchyard_pane_launcher_for` stayed in the launcher (shared, rebound on 3 lines in the suites, read through the launcher by four other modules) and is read through it; four neighbour guards widened to follow the moved caller |
| 12m | **Live CLI and model detection and the stale-provider-runtime drop** (SYRD-330) | 208 (6 defs) | `KNOWN_LIVE_CLI_NAMES` and the process and pane primitives stayed in the launcher and are read through it; two reader tables widened to the new reader |
| 12n | **The reload's config sync** (SYRD-331) | +54 in `live_role_runtime.py` (1 def) | appended to the existing module, whose earlier content is unchanged; its detectors, config reader and writer, role runner, has-session argv and session directory read through the launcher; the tenant-config write pinned |
| 12o | **Owner state directories** (SYRD-332) | 84 (4 defs) | the current user, runtime directory and failure reason read through the launcher; `pwd` is the launcher's own module, so the suites' passwd fakes reach it; containment and the install argv pinned with a fake passwd |
| 12p | **The launch's worktree preparation** (SYRD-333) | +23 in `project_worktrees.py` (1 def) | appended to the existing module, whose earlier content is unchanged; `ensure_project_worktrees` and `WorktreeProvisionResult` read through the launcher though defined there |
| 12q | **The board-authority launch preflight** (SYRD-334) | 65 (1 def) | reads nothing from the launcher; its board client stays the function's own import; `launch_project` calls it by the launcher's rebound name, so a refusal still stops the launch before any change |
| 12r | **Konsole layout-command quoting** (SYRD-335) | +67 in `presentation_layout_files.py` (5 defs) | appended beside its one reader, whose earlier content is unchanged; every call and pattern read goes through the launcher; quoting pinned against the baseline's own outputs |
| 12s | **The pane-launcher executable preflight** (SYRD-337) | 52 (1 def) | `pane_window_program` stayed (four readers) and is read through the launcher; `launch_project` calls the check by its patchable name |
| 12t | **`launch_project` P3: runners, owner delegation and paths** (SYRD-339) | 87 (`LaunchSetup` + 1 def) | the phase's 10 statements, unchanged, return a frozen `LaunchSetup` of its 8 outputs; `launch_project` calls it at P3's old position and unpacks them; proved by a new phase proof |
| 12u | **`launch_project` P5: pre-launch preparation** (SYRD-340) | +142 in `launch_phases.py` (`LaunchPreparation` + 1 def) | 5 statements unchanged; flow-sensitive interface 12 in / 5 out; both `return 1` carried as `exit_code` and returned by `launch_project` at the same point; four neighbour guards followed the moved calls |
| 12v | **`launch_project` P6: layout, plan and dry run** (SYRD-341) | +97 in `launch_phases.py` (1 def) | 4 statements unchanged; flow-sensitive interface 14 in / 0 out; returns `int \| None`, the dry run's `return 0` returned by `launch_project` at the same point; two neighbour guards followed the moved calls |
| 12w | **`launch_project` P7+P8: worker start and presentation** (SYRD-342) | +271 in `launch_phases.py` (`WorkerStartup` + 1 def) | 10 statements unchanged, the failure recorder nested and byte-equal; interface 18 in / 4 out; three terminal returns verbatim, dispatched by type before any output is read; seven neighbour guards follow the moved calls (five widened) |
| 12x | **`launch_project` P9: the launch's report and records** (SYRD-344) | +68 in `launch_phases.py` (1 def) | 6 statements unchanged, ending in the launch's own return; 16 in / 0 out; `launch_project` returns its answer at the old suffix; two neighbour guards followed the moved calls |
| 12y | **GitHub identity: status, set and clear** (SYRD-345) | `scripts/github_identity.py`, 629 lines (7 definitions) | moved unchanged in baseline order; 9 launcher names and 2 patched seams read at call time; every name re-exported; no guard table counted them |
| 12z | **Upgrade U4: role tooling preview and privileged staging** (SYRD-346) | `scripts/upgrade_phases.py`, 222 lines (`UpgradeToolingStaged` + 1 def) | 1 statement (152 lines) unchanged, five `return 1` verbatim; 10 reads plus 2 carried outputs; the publication source guard followed the moved call |
| 12aa | **Upgrade U5: identity and accounts** (SYRD-347) | +294 in `upgrade_phases.py` (`UpgradeIdentitiesDone` + 1 def) | 16 statements unchanged, both returns verbatim; 15 reads plus the `release_report_config` carry (`config` carries itself); three guards followed the moved calls |
| 12ab | **Upgrade U6: finish** (SYRD-348) | +138 in `upgrade_phases.py` (1 def) | 16 statements unchanged, ending in the upgrade's own `return 1`/`return 0`; 10 inputs; the upgrade now ends in `return _finish_upgrade(...)`; one guard followed the moved calls |
| 12ac | **Upgrade U3: generated artifacts and the upstream report** (SYRD-349) | +100 in `upgrade_phases.py` (1 def) | 14 statements unchanged; 11 inputs; the one always-assigned output returned bare and assigned back; no guard counted its calls |
| 12ad | **Upgrade U2: manager, desktop and repatriation** (SYRD-350) | +188 in `upgrade_phases.py` (`UpgradeStateReady` + 1 def) | 13 statements unchanged, five `return 1` verbatim; 8 inputs (config and source_repo carry themselves); three guards followed the moved calls |
| 12ae | **Upgrade U1: desktop decision and source pinning** (SYRD-351) | +152 in `upgrade_phases.py` (`UpgradeSourcePinned` + 1 def) | 12 statements unchanged, four returns verbatim (a stale pin's own code, 0 included); 10 inputs, 5 always-assigned outputs, no carries; one guard followed the moved call |
| 13a | **The director's upgrade completion, `finish_upgrade_command`** (SYRD-352) | new `director_upgrade.py`, 204 lines (1 def) | the whole function unchanged; 19 launcher names (21 reads) through a call-time seam, `_finish_upgrade_preview` among them; `os`/`subprocess` own; one guard followed the moved call |
| 13b | **Root's runtime baseline and plan helpers** (SYRD-353) | new `privileged_runtime_plan.py`, 585 lines (13 defs) | unchanged, in baseline order; 24 launcher names (30 reads) at call time, including the 6 the moved definitions use of each other; stdlib own; no guard changed |
| 13c | **The generated runtime artifact refresh** (SYRD-354) | new `runtime_artifact_refresh.py`, 365 lines (5 defs) | unchanged, in baseline order; 24 launcher names (39 reads) at call time, the helpers among them; stdlib own; one guard (SYRD-353's own) followed the moved calls |
| 14a | **Adding a role to a tenant** (SYRD-355) | new `project_role_add.py`, 498 lines (10 defs); `local_account_exists` to `host_accounts.py` | unchanged, in baseline order; 74 call-time reads including the launcher's own `__file__`; the default is the leaf's object; no guard changed |
| 14b | **The VCS close-role command** (SYRD-356) | new `project_vcs_close_role.py`, 151 lines (4 defs) | unchanged, in baseline order; 29 call-time reads, the seven shared plan-data facilities among them; no guard changed |
| 14c | **The shared role-plan data and board-unit helpers** (SYRD-357) | new `project_role_plan_support.py`, 142 lines (7 defs) | unchanged, in baseline order; 12 call-time reads, the inter-helper calls among them; both consumer modules byte-identical; one guard widened |
| next | The role-runtime command — **suggested, for a Director decision** | `set_project_role_runtime_command` (150 lines) and `_role_named` (5) | measured on SYRD-357's candidate; `_owner_catalog_args` is read by `model_validation` and stays a seam |
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

### SYRD-319 (slice 12d): presentation layout files

Re-measured on `4606264`, with the call graph regenerated on that tree. The
design was posted **before** any edit. The closure is exactly 10
definitions, one responsibility -- the presentation layout file: where it
goes, who owns it, and what it says -- and moved unchanged into
**`scripts/presentation_layout_files.py`** (256 lines):
- **Where, and who owns it:** `desktop_state_dir`,
  `desktop_presentation_layout_path`, `desktop_layout_destination_problem`,
  `default_layout_output_path`, `chown_layout_output_args` and
  `ensure_layout_output_owner`.
- **What it says:** `materialize_layout`, with `pane_split_title`,
  `role_display_name` and `inert_pane_command`.

The moved code came from 5 regions of the baseline: 1034–1057, 3107–3125, 3147–3209, 3241–3268, 3291–3349.

**No leaf (rule 25).** The ticket asked whether the three helpers other
modules read merit a leaf. Every reference was measured across `scripts/`:
- `role_display_name` has one caller, `pane_split_title`;
- `pane_split_title` is called only by `materialize_layout`, and by
  `presentation_reconnect` through the launcher;
- `inert_pane_command` is called only by `materialize_layout`, and by
  `presentation_controller` through the launcher.

None is a def-time default anywhere. So they moved with their one caller, and
the other readers still go through the launcher, where a patch is seen.

**Boundaries.**
- **Seams (rule 24).** Every form was scanned, including keyword rebinding in
  our own boundary tests.
  - `materialize_layout` is rebound by `team_launcher_viewer` (2 lines).
  - `default_layout_output_path` is rebound by
    `legacy_presentation_migration` and `presentation_window_recovery`
    (3 lines).
  - `desktop_state_dir` is rebound by SYRD-317's boundary test, and
    `pane_split_title` by SYRD-316's. The keyword form is what found these
    two.

  The launcher calls the first three by its own names from the five functions
  that stayed with it. Inside the module, every call to any of the four is
  `launcher.X`, including `desktop_presentation_layout_path`'s call to
  `default_layout_output_path`.
- **Call time:** the pane command and program, the window title, the layout
  leaves, the owner state path, the failure command and account lookups stay
  in the launcher and are read there. Among them are `current_user_name`
  (rebound in 28 suites) and `_gui_home` (5).
- **External readers**, every reference through the launcher (rule 21):
  - `presentation_controller`: `inert_pane_command`,
    `default_layout_output_path`, `desktop_presentation_layout_path` and
    `ensure_layout_output_owner`;
  - `role_runtime`: `default_layout_output_path`;
  - `desktop_presentation`: `desktop_state_dir`;
  - `desktop_layout_writer`: `desktop_layout_destination_problem`;
  - `presentation_reconnect`: `pane_split_title`.

  All 10 names are re-exported.
- **Rules:** no moved default names a launcher object (rule 6); nothing
  moved is a rebound constant (rule 20); no moved function binds an import
  alias (rule 18).

**Guards.** No source guard or lint reads moved text. The `chown -R` repair
is checked behaviourally by the suites that record runner calls, and the
scripts-wide scans cover the new module. No widening was needed.

**Evidence.**
- **AST proof:** it holds, including step 6.
- **New boundary test:** `tests/presentation_layout_files_boundary_test.py`
  has 42 checks, on owned fixtures only.
  - The import direction, identity in both orders, rule 21, and the seams at
    the launcher's call sites and never past the launcher here.
  - `materialize_layout` writing into a temp dir from the launcher's patched
    lookups:
    - inert commands titled by the patched `pane_split_title`;
    - a failed role's command;
    - each role's own account;
    - more panes than one window holds, refused before anything is written.
  - The titles as a person reads them, and the Konsole quoting.
  - Where a desktop account's copy goes: through the launcher's home and
    state path, and through a rebound `desktop_state_dir`.
  - Where a layout may not cross to: another directory, a path that climbs
    out, a missing project.
  - The owner repair: no call for the owner itself; otherwise the exact
    `chown -R` argv, and a failure that says why.

  14 of 14 mutations are killed, serially and on the clean tree. Each
  kill's own last error line was read (rule 23).
- **Green and identical to the baseline:** `desktop_presentation_boundary_test`, `gui_window_launch_boundary_test`, `legacy_presentation_launch_test`, `legacy_presentation_migration_test`, `operator_display_recovery_test`, `presentation_reconnect_boundary_test`, `role_model_repair_test`, `role_runtime_test`, `team_launcher_desktop_handoff_titles_test`, `team_launcher_pane_commands_test`, `team_launcher_presentation_titles_test`, `team_launcher_smoke_layout_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | presentation window recovery | 19 | 2 | |
  | privileged front door | 36 | 1 | |
  | control repo | 7 | 3 | |
  | desktop layout path | 8 | 4 | |
  | layout upgrade commands | 12 | 8 | |
  | pane paths | 9 | 2 | |
  | presentation | 11 | 5 | |
  | switchyard new prompts | 11 | 3 | |
  | tenant suspension | 15 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | viewer | 21 | 5 | |
  | tenant stop presentation window | 5 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **Suites with no `test_` functions**, compared by their whole output, identical on both trees and naming none of the moved code: `tenant_control_bridge_e2e` (the same 8 of 12 namespace cases pass, then the same environment stop) and `team_launcher_desktop_policy_owner` (refused at namespace setup before any case). Neither is claimed as coverage; the owned fixtures above are the evidence.
- **Stubs:** the rule 19 tmux stub and the refusing provider and service
  stubs. No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. `materialize_layout`, `desktop_state_dir` and `pane_split_title` are
  the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`4606264`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 22,053 lines) | 1 (`presentation_layout_files.py`, 256) |
| Where it sits | 5 regions, lines 1034–1057, 3107–3125, 3147–3209, 3241–3268, 3291–3349 | one file |
| moved `def`/`class` in `team_launcher.py` | 10 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12e,
the desktop account and layout-mode detection.
- **Size:** seeded from `default_gui_user`, `pinned_presentation_gui_user`,
  `presentation_gui_user`, `detected_invoking_desktop` and
  `resolve_layout_mode`, the closure is 9 definitions /
  139 lines, with `GUI_USER_ENV` and its legacy name.
- **Seams:** `detected_invoking_desktop` is rebound on
  4 lines, `presentation_gui_user` on
  2 and `default_gui_user` on 2.
- **Readers:** `presentation_controller` reads `presentation_gui_user`, and
  `gui_window_launch` reads `default_gui_user` and `GUI_USER_ENV` through the
  launcher.

### SYRD-320 (slice 12e): desktop account and layout-mode detection

Re-measured on `3c0346a`, with the call graph regenerated on that tree. The
design was posted **before** any edit. The closure is exactly 9
definitions, one responsibility -- whose desktop, and which layout -- and
moved unchanged into **`scripts/desktop_detection.py`** (195 lines):
- **The desktop account:** `GUI_USER_ENV`, `LEGACY_GUI_USER_ENV`,
  `default_gui_user`, `pinned_presentation_gui_user` and
  `presentation_gui_user`.
- **The layout mode:** `detected_invoking_desktop`,
  `_desktop_from_loginctl_output`, `_desktop_is_kde` and
  `resolve_layout_mode`.

`_gui_home` sits between them in the file but is not in the closure, and
stayed. The moved code came from 3 regions of the baseline: 887–888, 1423–1479, 1489–1578.
The extractor had spaced out the two adjacent account-variable constants;
they were put back as the baseline wrote them, and the proof was re-run.

**Boundaries.**
- **Imports:** the layout modes come from the existing leaf
  `scripts/layout_modes.py`, imported canonically: the same objects, never
  rebound. No moved default names a launcher object (rule 6), so no new leaf
  was needed (rule 25).
- **Seams (rule 24, every form scanned):**
  - `detected_invoking_desktop` is rebound on 4 lines by
    `tenant_resume_presentation_handoff`, which then calls
    `resolve_layout_mode`; the moved `resolve_layout_mode` reaches it as
    `launcher.detected_invoking_desktop`.
  - `presentation_gui_user` (2 lines, `desktop_layout_path`) and
    `default_gui_user` (2 lines, `presentation_titles`) are not called inside
    the module.

  The launcher's remaining callers keep calling by its names. The call sites,
  counted on the baseline outside the moved code and unchanged, are
  `default_gui_user` 2, `presentation_gui_user` 1 and `resolve_layout_mode` 3.
- **Call time:** `_env_first` (rebound in 3 suites), `current_user_name` (31)
  and `TENANT_CONTROL_CALLER_ENV` stay in the launcher and are read there.
- **External readers**, every reference through the launcher (rule 21):
  - `presentation_controller`: `presentation_gui_user`;
  - `gui_window_launch`: `default_gui_user` and `GUI_USER_ENV`;
  - `agy_credential`: `default_gui_user`. This reader was not in the
    ticket's list; it was found by measuring, and it is tested.

  All 9 names are re-exported.

**Guards.** No source guard or lint reads moved text. The nearby
launcher-source read, `tenant_resume_presentation_handoff`'s check of the
bridged branch, reads `launch_project`. No widening was needed.

**Evidence.**
- **AST proof:** it holds, including step 6, before and after the constants'
  spacing was restored.
- **New boundary test:** `tests/desktop_detection_boundary_test.py` has
  29 checks, on owned fixtures only.
  - The import direction, identity in both orders (layout modes included),
    rule 21, the seams at the launcher's call sites, and nothing rebound
    reached past the launcher.
  - Account precedence with only the named variables set:
    - a Wayland policy's account wins;
    - otherwise the configured, legacy, `SUDO_USER` and bridged names, in
      that order;
    - root is never chosen, from the environment or from a policy;
    - then the owner, then the launcher's current user.
  - `default_gui_user`'s order through the launcher's `_env_first`.
  - Detection through a fake `loginctl` runner that asks about the human's
    session, not the owner's.
  - A rebound `detected_invoking_desktop` deciding `auto`, and the
    unknown-mode refusal.

  13 of 13 mutations are killed, serially and on the clean tree. Each
  kill's own last error line was read (rule 23).
- **Green and identical to the baseline:** `control_repository_placeholder_test`, `gui_window_launch_boundary_test`, `legacy_presentation_migration_test`, `operator_display_recovery_test`, `presentation_layout_files_boundary_test`, `team_launcher_presentation_titles_test`, `team_launcher_session_sources_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | presentation window recovery | 19 | 2 | |
  | privileged front door | 36 | 1 | |
  | agy credential seeding | 26 | 10 | |
  | control repo | 7 | 3 | |
  | desktop layout path | 8 | 4 | |
  | env config | 24 | 2 | |
  | konsole | 14 | 2 | |
  | layout upgrade commands | 12 | 8 | |
  | pane paths | 9 | 2 | |
  | presentation | 11 | 5 | |
  | resume detached | 8 | 1 | |
  | session report | 12 | 1 | |
  | switchyard commands | 10 | 2 | |
  | switchyard new prompts | 11 | 3 | |
  | viewer | 21 | 5 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | tenant stop presentation window | 5 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **Suites with no `test_` functions**, compared by their whole output, identical on both trees and naming none of the moved code: `tenant_control_bridge_e2e` (the same 8 of 12 namespace cases pass, then the same environment stop) and `team_launcher_desktop_policy_owner` (refused at namespace setup before any case). Neither is claimed as coverage; the owned fixtures above are the evidence.
- **Stubs:** the rule 19 tmux stub and the refusing provider and service
  stubs, `loginctl` included. No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. `resolve_layout_mode`, `presentation_gui_user` and `GUI_USER_ENV` are
  the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`3c0346a`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 21,864 lines) | 1 (`desktop_detection.py`, 195) |
| Where it sits | 3 regions, lines 887–888, 1423–1479, 1489–1578 | one file |
| moved `def`/`class` in `team_launcher.py` | 9 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12f,
presentation window processes and closing.
- **Size:** seeded from `close_presentation_window`,
  `close_desktop_presentation`, `desktop_presentation_windows`,
  `unsafe_presentation_report` and `unsafe_root_presentation_windows`, the
  closure is 8 definitions / 230 lines.
- **Seams and readers:** `close_desktop_presentation` is rebound on
  2 lines. `presentation_controller` reads
  `presentation_window_processes`, and `project_status` reads
  `unsafe_root_presentation_windows`.
- **A duplicate:** `presentation_window_processes` is defined twice in the
  launcher (candidate lines 2873 and
  18175), and the later definition is the one every
  caller reaches (rule 12). That slice has to move or keep both and prove
  which one binds.

### SYRD-321 (slice 12f): presentation window processes and closing

Re-measured on `f489f32`, with the call graph regenerated on that tree. The
design, including a defect report, was posted **before** any edit.

**The duplicate definition (rule 12).** `presentation_window_processes` is
defined twice in the baseline:
- **First copy (SYRD-43).** It scans `PROC_ROOT` by marker substrings and
  returns `UnsafePresentationWindow` results that carry `.uid`.
- **Second copy (SYRD-193).** It scans `/proc` for Konsole windows whose argv
  holds the exact presentation-layout path, and returns
  `PresentationWindowProcess` results that carry `owner_uid`.

Nothing captures the first at import time -- no default, decorator or
module-level reference -- so every caller has reached the second since
SYRD-193.

**The move.** The closure -- 11 names, 12 top-level nodes -- moved
unchanged into **`scripts/presentation_windows.py`** (381 lines), BOTH copies
in baseline order (rule 26). Inside the module the second copy is defined last
and binds. The launcher imports that name, so
`team_launcher.presentation_window_processes` is the second copy's object, as
before, and no copy is left in the launcher to re-bind it. The first copy's
own helpers (`PRESENTATION_PROGRAM_NAMES`, `_proc_cmdline`,
`presentation_layout_markers`) are used by nothing else (measured), so they
moved with it (rule 25). `PROC_ROOT`, `_proc_effective_uid` and
`_command_name` have other users and stay in the launcher. The moved code came
from 5 regions of the baseline: 2788–2798, 2806–2811, 2836–2938, 18166–18275, 20746–20828.

**The tooling for it.**
- **The extractor's `all_copies`:** every copy moves, with spans and call-time
  aliases per node.
- **Proof step 1:** it compares every copy in order.
- **Regression:** SYRD-320's spec, run through the old and new extractor in
  throwaway worktrees, produced byte-identical files and reports, and the
  proof still holds on SYRD-320's candidate.
- **Negative checks, before relying on it:** the proof fails when the two
  copies are swapped, when the dead copy is dropped, and when the live copy is
  dropped.

**A baseline defect, preserved and reported.**
`unsafe_root_presentation_windows` (SYRD-43) was written against the first
copy and reaches the second:
- with no matching window it returns `[]`;
- with a matching window, whoever owns it, it raises `AttributeError:
  'PresentationWindowProcess' object has no attribute 'uid'`;
- with `config_path=None` it raises too;
- it can never report the root `*-konsole-layout.json` window it was written
  to catch.

`switchyard status`, the attach path of `launch_project` and `upgrade` call it
unguarded. Two baseline-red cases in `team_launcher_unprivileged_presentation`
and two in `team_launcher_desktop_layout_path` are exactly this, identical on
both trees. The extraction keeps it byte for byte, and the boundary test pins
the raise, so a fix has to change that check on purpose. It was reported to
the Director on the ticket.

**Boundaries.**
- **Seams:** `close_desktop_presentation` is rebound on 2 lines. Every form
  was scanned for every member, and nothing else in the closure is rebound.
  `presentation_window_processes` and `desktop_presentation_windows` are
  reached as `launcher.X` by the moved callers, the same objects the
  baseline's global lookups gave.
- **Launcher call sites:** the launcher's calls to the entry points are
  unchanged -- measured on both trees outside the moved code: 5, 4, 1 and 1.
- **External readers**, every reference through the launcher (rule 21):
  `presentation_controller` (`presentation_window_processes`) and
  `project_status` (`unsafe_root_presentation_windows`). All 11
  names are re-exported; the dead copy is not reachable by name.

**A neighbour guard, widened.** SYRD-319's boundary test counted
`default_layout_output_path` and `desktop_state_dir` calls in the launcher
alone. Two of those callers moved here, so the guard now counts the same
baseline totals across the launcher (by its own name) and
`presentation_windows.py` (through the launcher). Mutants that make either
moved caller bypass the launcher are killed by it, so it is no weaker.

**Evidence.**
- **AST proof:** it holds, including step 6 and both copies.
- **New boundary test:** `tests/presentation_windows_boundary_test.py` has
  28 checks, on owned fake `/proc` trees and a recording signaller only.
  Every scan is given its tree, because the binding copy ignores `PROC_ROOT`.
  It pins:
  - the binding (the later copy, its return type, required `config_path`,
    none left in the launcher), identity, rule 21 and the seams;
  - whole-argument matching in either spelling, Konsole only, the owner uid,
    no `.backup`;
  - close: SIGTERM each match, a window gone in between ignored, the others
    reported;
  - the desktop close: every window, exit 1 on a problem, a rebound scan
    obeyed;
  - the defect, as-is.

  18 of 18 mutations are killed, serially and on the clean tree, and each
  kill's own last error line was read (rule 23). A first run left a survivor
  -- the desktop scan had no `.backup` fixture -- and a swapped-copies kill
  that came from an unrelated fixture crash. The test gained the fixture, and
  the binding check now runs first; both are now killed by their property.
- **Green and identical to the baseline:** `agent_cli_boundary_test`, `caller_cli_discovery_test`, `desktop_detection_boundary_test`, `operator_display_recovery_test`, `project_status_boundary_test`, `readonly_status_without_root_test`, `role_identity_cutover_boundary_test`, `team_launcher_status_liveness_test`.
  `presentation_layout_files_boundary_test` is green on both trees in its own
  form: the baseline's guard on the baseline, the widened one here.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | presentation window recovery | 19 | 2 | |
  | privileged front door | 36 | 1 | |
  | desktop layout path | 8 | 4 | |
  | presentation | 11 | 5 | |
  | tenant suspension | 15 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | tenant stop presentation window | 5 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **Suites with no `test_` functions**, compared by their whole output, identical on both trees and naming none of the moved code: `tenant_control_bridge_e2e` (the same 8 of 12 namespace cases pass, then the same environment stop) and `team_launcher_desktop_policy_owner` (refused at namespace setup before any case). Neither is claimed as coverage; the owned fixtures above are the evidence.
- **Stubs:** One suite hit stubs, identically on both trees: `readonly_status_without_root` reached the refusing `sudo -n -u` stub once and the socketless `tmux list-panes -a` refusal once, and passes on both; nothing reached a real sudo or a live tmux server.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. `presentation_window_processes` there is the later copy, and it and
  `close_desktop_presentation` are the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`f489f32`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 21,722 lines) | 1 (`presentation_windows.py`, 381) |
| Where it sits | 5 regions, lines 2788–2798, 2806–2811, 2836–2938, 18166–18275, 20746–20828 | one file |
| moved top-level nodes in `team_launcher.py` | 12 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12g,
the display bridge.
- **Size:** seeded from `display_bridge_state`,
  `display_bridge_launch_problem` and `ensure_display_bridge`, the closure is
  6 definitions / 261 lines, including the sudoers rule
  check.
- **Seams and readers:** `display_bridge_launch_problem` is rebound on
  5 lines and read by `presentation_controller`.

Also measured on this candidate, for later slices: legacy presentation
migration, 7 definitions / 184 lines; the desktop approval command and its
records, 10 / 330; and `replace_presentation_window_command`, 2 / 108.

### SYRD-323 (slice 12g): display bridge setup and readiness

Re-measured on `82f7616`, with the call graph regenerated on that tree. The
design, with a tooling fix and a finding about SYRD-305, was posted
**before** any edit. The closure is exactly 6 definitions, one
responsibility -- whether a crossing window's tabs can reach the tenant's
display sessions, and installing the bridge when they cannot -- and moved
unchanged into **`scripts/display_bridge.py`** (320 lines):
`DisplayBridgeState`, `SUDOERS_RULE_MODE`, `sudoers_rule_state`,
`display_bridge_state`, `display_bridge_launch_problem` and
`ensure_display_bridge`.

`_privileged_upgrade_check_command` sits between two of them and stayed. The
launcher's two duplicated names (`_normalized_path`, `PROC_ROOT`) are not
read by the closure. The moved code came from 2 regions of the baseline:
5369–5577, 5586–5647.

**The tooling fix (rule 27).** Before any edit, the extractor was found to
rewrite a name the function binds itself: `display_bridge_state` and
`display_bridge_launch_problem` import `TENANT_CONTROL_ROOT` from
`project_provision`. The launcher's own `TENANT_CONTROL_ROOT` is a different
object, and suites rebind it. The fix and its checks:
- the extractor leaves any name the function binds untouched;
- proof step 7 flags any `launcher.X` whose `X` the function binds itself;
- step 7 fails on the unfixed extractor's output for this slice (both
  functions) and passes on SYRD-320's and SYRD-321's candidates;
- the fixed extractor is byte-identical to its predecessor on those two
  specs.

**A finding about SYRD-305.** Step 7 run over all 42 modules this refactor
created reports exactly one: `role_account_migration.render_role_account_migration`
calls `launcher.role_tooling_staging_commands`, where the baseline called its
own function-level import of that name from `project_provision`. Measured:
- the launcher binds the very same function object;
- nothing in `scripts/` or `tests/` rebinds it.

So there is no behaviour difference; only a launcher patch would now be seen
there. The integrated code was not changed in this slice; the finding is
recorded on SYRD-323 for the Director.

**Boundaries.**
- **Seams (rule 24, every form scanned):** `display_bridge_launch_problem` is
  rebound on 5 lines, and its only caller, `presentation_controller`, reads
  it through the launcher. Nothing else in the closure is rebound.
- **Call time:** the three calls of `display_bridge_state` inside the module
  and `current_user_name` (rebound in 32 suites) read the launcher.
- **Launcher callers:** `upgrade_project_command` calls
  `ensure_display_bridge` by the launcher's name.
- **Exports:** all 6 names are re-exported.
- **Imports:** the grant name, privileged root and rule text stay the
  functions' own imports from `project_provision`.
- **Rules:** no moved default names a launcher object (rule 6); nothing
  rebound moves (rule 20); no alias binding (rule 18).

**Guards.** No source guard reads moved text. The nearby launcher-source reads
look at the new-project flow and `launch_project`, and the display-attach
checks read the standalone helper.

**Evidence.**
- **AST proof:** it holds, steps 1-7.
- **New boundary test:** `tests/display_bridge_boundary_test.py` has 36
  checks, on grant and rule files this test writes in its own temporary
  directory and owns, with this account passed in as the owner the check
  expects. No sudo, visudo, /etc or real grant is touched.
  - The import direction, identity, rule 21, and the seams.
  - The privileged names never read through the launcher: a rebound
    `team_launcher.TENANT_CONTROL_ROOT` is not where the grant is looked for.
  - Every verdict:
    - not needed, install;
    - refuse, for a writable grant, another owner's grant, another project,
      another owner named, another person authorized, a writable rule, a
      symlinked rule (nothing written through it) and another owner's rule;
    - unverified, for a rule only root can read;
    - install, for drifted text or mode;
    - present.
  - `ensure_display_bridge`: a dry run writes nothing; the install runs
    `sh -euc` through the runner given and is read back; an install that
    exits 0 but leaves the rule wrong is not trusted; failed, refused and
    unverified bridges say why and change nothing.
  - `display_bridge_launch_problem`'s wording, and a rebound verdict obeyed.

  15 of 15 mutations are killed, serially and on the clean tree. Each
  kill's own last error line was read (rule 23). A first run left one
  survivor -- no fixture had a grant of another owner -- and the test gained
  it.
- **Green and identical to the baseline:** `director_upgrade_boundary_test`, `display_attach_focus_test`, `legacy_presentation_migration_test`, `operator_display_recovery_test`, `presentation_windows_boundary_test`, `privileged_front_door_test`, `privileged_helper_boundary_test`, `single_owner_staged_tooling_test`, `team_launcher_upgrade_cutover_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | presentation window recovery | 19 | 2 | |
  | desktop layout path | 8 | 4 | |
  | tenant resume presentation handoff | 27 | 2 | |
  | role control sudoers install | 7 | 1 | |
  | tenant control helper repair | 22 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **Suites with no `test_` functions**, compared by their whole output, identical on both trees and naming none of the moved code: `tenant_control_bridge_e2e` (the same 8 of 12 namespace cases pass, then the same environment stop) and `team_launcher_desktop_policy_owner` (refused at namespace setup before any case). Neither is claimed as coverage; the owned fixtures above are the evidence.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root. `ensure_display_bridge` and `display_bridge_launch_problem` are the
  launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`82f7616`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 21,414 lines) | 1 (`display_bridge.py`, 320) |
| Where it sits | 2 regions, lines 5369–5577, 5586–5647 | one file |
| moved `def`/`class` in `team_launcher.py` | 6 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12h,
the desktop approval command and its records.
- **Size:** seeded from `switchyard_approve_desktop_command` and the
  approval readers and writers, the closure is 10 definitions /
  330 lines.
- **Seams and readers:** `read_host_desktop_approval` is rebound on
  1 line, and no other module reads
  the closure.

Also measured on this candidate:
- legacy presentation migration, 7 / 184, which pulls in
  `_gui_home` (rebound on 21 lines and read by four modules);
- `replace_presentation_window_command`, 2 / 108, which pulls
  in `switchyard_pane_launcher_for`, also read by four modules.

Both need their shared helper settled first (rule 25).

### SYRD-324 (preservation follow-up): the role migration's own import restored

No lines moved, so the size table is unchanged: the launcher is untouched and
`scripts/role_account_migration.py` keeps its line count.

**The site.** SYRD-323's proof step 7 found one earlier extraction that read
a function's own import through the launcher.
`render_role_account_migration` imports `role_tooling_staging_commands` from
`project_provision` at function level. Before SYRD-305 it called that local
name; since SYRD-305 it had called `launcher.role_tooling_staging_commands`.
The launcher binds the very same function object and nothing rebinds it, so
no ordinary run behaved differently. A launcher patch was visible there,
though, and it should not have been.

**The correction.** One token: the call is the bare local name again.
`launcher.switchyard_shared_install_root()` beside it stays, because the
baseline read the launcher's global there too, so it is a seam.

**Evidence.**
- **Against the parent of `96953de` (the pre-SYRD-305 launcher):**
  - the restored function, normalized as proof step 1 normalizes, equals the
    original exactly, and so do its defaults and comments;
  - the call is a bare name bound by the function's own `project_provision`
    import;
  - proof step 7 is clean for the module;
  - the unfixed function normalizes equal too -- which is exactly why step 1
    alone never saw it.
- **Scope-aware rule 27 scan:** the stricter scan's self-test finds every
  binding form it names and does not flag a `global`. On all 43 refactor
  modules it found exactly this site before the correction, and zero after.
- **Regression:** `tests/role_account_migration_boundary_test.py` now has
  9 checks. The new case renders the migration for an owned temporary
  tenant -- rendered as text only, never run -- with `project_provision`'s
  staging builder recording, the launcher's name a trap, and the launcher's
  install root patched. It asserts:
  - the recorded lines are in the script;
  - the trap was never reached;
  - the builder was given the launcher's patched install root.

  An AST check pins the bare call.
- **Mutations**, serially and on the clean tree, both killed:
  - re-creating the bug is caught by the AST check, and, run alone, by the
    trap;
  - reading the install root past the launcher is caught by the recorded
    argument.
- **Suites:** the suites that render this migration ran on both this tree
  and the pristine baseline `3d7d180`.
  - Green on both: `team_launcher_role_account_script_order`,
    `board_skill_staged_bundle`, `ticket_board_tenant_source_confinement`
    and `team_launcher_upgrade_cutover`; so is the boundary test (9 checks
    here, 6 on the baseline, which lacks the new case).
  - `role_control_sudoers_install` is red on both trees, identically: 7 pass
    and 1 fail, case by case, as in SYRD-323.
- **Release and CLI:** the staged release carries the bare local call, and
  CLI `--help` output and exit codes are identical for all 36 invocations.

### SYRD-325 (slice 12h): desktop approval command and records

Re-measured on `d7d4a0a`, with the call graph regenerated on that tree. The
design was posted **before** any edit. The closure is exactly 10
definitions, one responsibility -- this host's standing desktop approval --
and moved unchanged into **`scripts/desktop_approval.py`** (407 lines):
- **Where it lives:** the path (`DEFAULT_DESKTOP_APPROVAL_SETTING_PATH`, with
  its comment block) and `_desktop_approval_setting_path`.
- **At provisioning:** `read_host_desktop_approval` and
  `write_host_desktop_approval`.
- **Root's record:** `read_desktop_approval_record` and
  `write_desktop_approval_record`, plus `DESKTOP_APPROVAL_REVOKED`.
- **The command:** `_desktop_approval_operator`, `_desktop_approval_lines` and
  `switchyard_approve_desktop_command`.

The `DESKTOP_FROM_*` constants beside the path, the CLI parser and the
dispatch stayed. The moved code came from 2 regions of the baseline:
1082–1082, 9849–10199.

**Boundaries.**
- **Seams (rule 24, every form scanned):** `read_host_desktop_approval` is
  patched once, and only the launcher's callers use it. Those callers -- 3
  sites, plus 1 for the host writer and 1 for the command, measured -- keep
  calling by the launcher's name. Nothing else in the closure is rebound.
- **Call time:** the user-name check, `_write_json_atomic`,
  `expected_privileged_uid` and `read_plan_no_follow` are read from the
  launcher; the last three are rebound in suites.
- **Rule 27:** `resolve_operator` and `scripts.desktop_access as desktop` stay
  the functions' own imports. Proof step 7 and the scope-aware scan (all 44
  refactor modules) report zero.
- **Readers:** no other module reads the closure. All 10 names are
  re-exported.
- **Rules:** defaults are stdlib or literals (rule 6); nothing rebound moves
  (rule 20).

**Guards.** No source guard reads the moved text.

**Evidence.**
- **AST proof:** it holds, steps 1-7.
- **New boundary test:** `tests/desktop_approval_boundary_test.py` has
  32 checks. It works on records this test writes in its own temporary
  directory, under the documented privileged-root seam, so this account is
  the privileged owner; the operator, desktop sessions and effective uid are
  fakes, and no /etc file or real operator is touched. A non-root account
  cannot give a file to root's group, so the record's `fchown` is recorded --
  asserted to ask for the privileged uid and group 0 -- while its mode and
  rename are real. It covers:
  - identity, the seams and rule 27;
  - the host approval's write (through the launcher's JSON writer) and read,
    and a malformed user refused;
  - `approve-desktop`: not root, nobody to attribute, no reference, several
    desktops, an unknown account (none of which writes anything); an approval
    naming the account, the person who elevated and why; 0600; approving
    twice; show; a revocation keeping the record with nobody approved;
    revoking twice; a record anyone could have written not written over;
  - a symlink planted at the staged name refused, with nothing written
    through it;
  - a write that does not read back approving nothing;
  - the launcher's no-follow reader and privileged uid obeyed when rebound.

  15 of 15 mutations are killed, serially and on the clean tree. Each
  kill's own last error line was read (rule 23).
- **Green and identical to the baseline:** `desktop_policy_generation_test`, `display_bridge_boundary_test`, `host_desktop_approval_test`, `privileged_action_catalogue_test`, `resume_provision_desktop_test`, `team_launcher_legacy_desktop_policy_upgrade_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | privileged front door | 36 | 1 | |
  | switchyard commands | 10 | 2 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **`team_launcher_desktop_policy_owner`** has no `test_` functions; its whole output is identical on both trees (refused at namespace setup before any case), it names none of the moved code, and it is not claimed as coverage.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations, `approve-desktop` included.
- **Staged release:** it contains the module and loads it from the release
  root, and its `approve-desktop --help` exits 0. The command, the host
  reader and the path are the launcher's own objects.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`d7d4a0a`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 21,147 lines) | 1 (`desktop_approval.py`, 407) |
| Where it sits | 2 regions, lines 1082–1082, 9849–10199 | one file |
| moved `def`/`class` in `team_launcher.py` | 10 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12i,
desktop policy resolution.
- **Size:** seeded from `_resolve_desktop_policy`, `approved_desktop_policy`,
  `install_recovered_desktop_access` and `upgrade_desktop_policy_decision`,
  the closure is 11 definitions / 327 lines, including the
  `DESKTOP_FROM_*` constants.
- **Seams and readers:** nothing in it is rebound in any scanned form, and no
  other module reads it.

Also measured on this candidate:
- `prepare_project_desktop` and `configure_project_desktop`, 3 /
  77: `prepare_project_desktop` is rebound on
  7 lines and read by three modules;
- legacy presentation migration, 7 / 184, and window
  replacement, 2 / 108. Each needs a helper four modules
  read settled first.

### SYRD-326 (slice 12i): desktop policy resolution

Re-measured on `c176667`, with the call graph regenerated on that tree. The
design was posted **before** any edit, and the ticket's dependency and reader
claims were checked independently: a text scan of `scripts/`, not only the
graph.

The closure is exactly 11 definitions, one responsibility -- deciding a
project's desktop policy, and where the decision came from -- and moved
unchanged into **`scripts/desktop_policy.py`** (384 lines):
- the five `DESKTOP_FROM_*` sources, restored to their contiguous block and
  checked against the baseline text;
- `_resolve_desktop_policy`, with `_desktop_host_is_headless` and
  `_prompt_choice`, both measured to have no other caller;
- `approved_desktop_policy` and `install_recovered_desktop_access`;
- `upgrade_desktop_policy_decision`.

The moved code came from 5 regions of the baseline: 1090–1094, 5969–5999, 9856–9998, 10661–10774, 15325–15362.

**Boundaries.**
- **Seams:** no member is rebound in any scanned form, and no other module
  reads any of them. The launcher's three callers keep calling by its names:
  - `switchyard_new_command` calls `_resolve_desktop_policy`;
  - `_finish_provision_after_packet` calls `install_recovered_desktop_access`;
  - `upgrade_project_command` calls `upgrade_desktop_policy_decision`.
- **Call time**, read from the launcher:
  - `read_host_desktop_approval` and `write_host_desktop_approval`, now in
    `desktop_approval`, reached through the launcher's names as on the
    baseline;
  - `configure_project_desktop`;
  - `_load_json`, `_prompt_bool`, `_read_prompt` and
    `SWITCHYARD_PROMPT_MAX_ATTEMPTS`;
  - `current_user_name`.
- **Rule 27:** the `scripts.desktop_access` imports in three functions stay
  theirs. Proof step 7 and the scope-aware scan (45 refactor modules) report
  zero.
- **Rules:** defaults are builtins or stdlib (rule 6); nothing rebound moves
  (rule 20). All 11 names are re-exported.

**Guards.** No source guard reads the moved text.

**A neighbour guard, widened.** SYRD-325's boundary test counted the calls to
`read_host_desktop_approval` (3) and `write_host_desktop_approval` (1) in the
launcher alone. All four callers moved here, and they still call through the
launcher. The guard now counts the same baseline totals across the launcher
(by its own name) and `desktop_policy.py` (through the launcher). A mutant
that makes a moved caller bypass the launcher is killed by it, and it is
green on both trees, each in its own form.

**Evidence.**
- **AST proof:** it holds, steps 1-7, before and after the constants' block
  was restored.
- **New boundary test:** `tests/desktop_policy_boundary_test.py` has 31
  checks, on owned fixtures only -- fake owner lookups, scripted answers, a
  recording installer and temporary files. No desktop, logind, grant or /etc
  file is touched. It covers:
  - identity, the launcher's call sites and rule 27;
  - `switchyard new`'s decision in its order:
    - a policy file (through the launcher's loader), then `--headless`, with
      both at once refused;
    - an ambiguous host stopping, with or without `--yes`;
    - headless offered only where there is no desktop, and declining it
      provisioning nothing;
    - a recorded approval used through the launcher's reader;
    - `--yes` never granting;
    - the person choosing headless or recording an approval through the
      launcher's writer;
  - recovery:
    - the desktop comes from the host's record;
    - a tenant naming another desktop, or asking with no record behind it,
      is refused;
    - the install goes through the installer given, or the launcher's
      `configure_project_desktop` as rebound, and a failed install says so;
  - an upgrade never assuming a policy.

  14 of 14 mutations are killed, serially and on the clean tree. Each
  kill's own last error line was read (rule 23). A first run had four kills
  by incidental crashes -- an exhausted list of scripted answers, and a
  subscript on a missing policy. The scripted answers now fail with "the
  decision asked a question it had no business asking", and the checks guard
  their subscripts, so every kill now names its property.
- **Green and identical to the baseline:** `desktop_policy_generation_test`, `host_desktop_approval_test`, `resume_provision_desktop_test`, `team_launcher_legacy_desktop_policy_upgrade_test`, `privileged_action_catalogue_test`, `team_launcher_upgrade_cutover_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | privileged front door | 36 | 1 | |
  | desktop access | 3 | 1 | |
  | new project | 18 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **`team_launcher_desktop_policy_owner`** has no `test_` functions; its whole output is identical on both trees (refused at namespace setup before any case), it names none of the moved code, and it is not claimed as coverage.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root, and its `new --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`c176667`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 20,801 lines) | 1 (`desktop_policy.py`, 384) |
| Where it sits | 5 regions, lines 1090–1094, 5969–5999, 9856–9998, 10661–10774, 15325–15362 | one file |
| moved `def`/`class` in `team_launcher.py` | 11 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12j,
project desktop preparation and install.
- **Size:** `prepare_project_desktop`, `configure_project_desktop` and
  `DESKTOP_ENV_KEYS`, 3 definitions / 77 lines.
- **Seams:** `prepare_project_desktop` is rebound on
  7 lines and read by
  `first_run_auth`, `presentation_controller` and `workflow_launcher`.
  `configure_project_desktop` is rebound on
  2 lines and read by the
  module this slice created.

The presentation-config pair (2 / 13) could join it.
Legacy migration and window replacement still wait on their shared helpers.

### SYRD-327 (slice 12j): project desktop preparation and install

Re-measured on `6219de0`, with the call graph regenerated on that tree. The
design was posted **before** any edit. The closure is exactly 3
definitions, one responsibility -- making a project's desktop policy real --
and moved unchanged into **`scripts/project_desktop.py`** (122 lines):
- `DESKTOP_ENV_KEYS`;
- `prepare_project_desktop`: validate, optionally install, verify as the
  tenant, and give every role the verified environment;
- `configure_project_desktop`: install, prepare, record, and on failure undo
  and restore.

The moved code came from 1 region of the baseline: 5164–5244.

**The presentation-config pair was measured and declined.**
`presentation_controller_enabled` wraps the presentation controller, and
`presentation_section_for_roles` is the legacy-presentation rule. Neither
touches the desktop policy, the environment keys or the installer, so they
belong with legacy presentation migration, the next slice.

**Boundaries.**
- **Seams (rule 24, every form scanned):**
  - `prepare_project_desktop` is rebound on 7 lines in four suites;
    `configure_project_desktop` on 2, one of them SYRD-326's boundary test by
    keyword.
  - The launcher calls them at 6 and 2 sites by its own names.
  - `configure_project_desktop`'s call of `prepare_project_desktop` goes
    through the launcher, as the baseline's global lookup did.
- **Call time:** `_load_json`, `_owner_command_args`, `_write_json_atomic` and
  `current_user_name` are read from the launcher.
- **Readers, each through the launcher (rule 21):**
  - `first_run_auth`: `prepare_project_desktop` and `DESKTOP_ENV_KEYS`;
  - `presentation_controller` and `workflow_launcher`:
    `prepare_project_desktop`;
  - `desktop_policy`: `configure_project_desktop`.
- **Rule 27:** both functions' `desktop_access` import stays theirs. Proof
  step 7 and the scope-aware scan (46 refactor modules) report zero.
- **The helper:** the default helper, `Path(__file__)` beside the module,
  still resolves to `scripts/desktop_access.py`. The test pins it, and the
  staged release confirms the file beside the module there.
- **Rules:** defaults unchanged (rule 6); nothing rebound moves (rule 20).
  All 3 names are re-exported.

**Guards.** No source guard reads the moved text.

**Evidence.**
- **AST proof:** it holds, steps 1-7.
- **New boundary test:** `tests/project_desktop_boundary_test.py` has 30
  checks, with `desktop_access` patched to record validate, install and
  uninstall, and recording or refusing runners. No grant, desktop, sudo or
  tenant is touched. It covers:
  - identity, the readers, the seams and call sites, and rule 27;
  - `prepare_project_desktop`:
    - headless removes and unsets the desktop variables and verifies nothing;
    - Wayland verifies as the tenant through the launcher's owner command and
      the runner given, and each role gets exactly the verified environment;
    - it installs only when asked, with the helper beside it;
    - a failed verification stops with its reason;
  - `configure_project_desktop`:
    - a dry run changes nothing;
    - the policy is prepared through the launcher's seam and then recorded;
    - a failure takes the new policy off and puts the old one back;
    - a desktop the install cannot reach is explained, not dumped.

  15 of 15 mutations are killed, serially and on the clean tree, and each
  kill's own last error line was read (rule 23). On the first run, the
  bypass mutant ran the real prepare with the real runner; my refusing sudo
  stub stopped it. The configure cases now pass a runner that refuses outright,
  so the test itself kills it, and it never depends on the stub.
- **Green and identical to the baseline:** `desktop_policy_boundary_test`, `desktop_policy_generation_test`, `display_recovery_live_tmux_test`, `first_run_auth_boundary_test`, `first_run_setup_completion_test`, `host_desktop_approval_test`, `legacy_presentation_migration_test`, `resume_provision_desktop_test`, `team_launcher_first_run_auth_test`, `team_launcher_legacy_desktop_policy_upgrade_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | privileged front door | 36 | 1 | |
  | presentation | 11 | 5 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **Suites with no `test_` functions**, compared by their whole output, identical on both trees:
  - `team_launcher_desktop_policy_owner` is refused at namespace setup before any case.
  - `tenant_control_bridge_e2e` passes the same 8 of 12 namespace cases, then stops at the same environment
    limit. It does name moved code: its `DISPATCHER_SOURCE` template rebinds
    `team_launcher.prepare_project_desktop` in a child process. Only its 9th and 10th cases use that
    template, and those are exactly the ones this environment never reaches: the 9th stops the loop,
    and the 10th never runs.
  - Neither is claimed as coverage; the seam is covered by the owned boundary test.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root, `desktop_access.py` is beside it, and `new --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`6219de0`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 20,475 lines) | 1 (`project_desktop.py`, 122) |
| Where it sits | 1 region, lines 5164–5244 | one file |
| moved `def`/`class` in `team_launcher.py` | 3 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12k,
legacy presentation migration with the presentation config.
- **Size:** 9 definitions / 197 lines, including
  `_gui_home`.
- **`_gui_home`** is rebound on 21 lines and read
  through the launcher by four modules, so it should stay in the launcher and
  be read there (rule 25).
- **`presentation_section_for_roles`** is rebound
  2x and read by `pane_rebind`.

### SYRD-328 (slice 12k): legacy presentation migration and config

Re-measured on `0a82149`, with the call graph regenerated on that tree. The
design was posted **before** any edit. The graph's closure held nine
definitions including `_gui_home`. **`_gui_home` stayed in the launcher**: it
is shared account lookup, rebound in 7 suites and read through the launcher
by four modules, and the moved code reads it there.

The remaining 8 definitions -- legacy presentation migration and the
presentation-config wrappers -- moved unchanged into
**`scripts/legacy_presentation.py`** (255 lines):
- `LegacyPresentationMigration`;
- `presentation_controller_enabled`;
- `presentation_section_for_roles` and `legacy_presentation_section`;
- `legacy_presentation_migration` and `_desktop_state_root_problem`;
- `migrate_legacy_presentation` and `legacy_presentation_refusal`.

The moved code came from 2 regions of the baseline: 5169–5311, 5320–5381.
`_privileged_upgrade_check_command`, which sits between them, stayed.

**Boundaries.**
- **Seams (rule 24, every form scanned):** `presentation_section_for_roles`
  is rebound on 2 lines and read by `pane_rebind` through the launcher. The
  launcher's four callers keep its names, one site each (measured).
- **Call time:** accounts, homes (`_gui_home`), the desktop account, the
  layout paths, the JSON reader and writer and the config loader are read
  through the launcher, including names now in modules it re-exports and
  rebinds.
- **Rule 27:** the presentation controller stays each function's own import.
  Proof step 7 and the scope-aware scan (47 refactor modules) report zero.
- **Rules:** defaults unchanged (rule 6); nothing rebound moves (rule 20).
  All 8 names are re-exported.

**Neighbour guards widened**, as stated in the design and as SYRD-321 and
SYRD-326 did:
- `desktop_detection_boundary_test` now counts `presentation_gui_user`'s
  baseline call site in `legacy_presentation.py`, through the launcher.
- `presentation_layout_files_boundary_test` counts `desktop_state_dir`'s site
  there too.

The totals are unchanged, a bypass mutant of each moved caller is killed, and
both are green on both trees.

**Evidence.**
- **AST proof:** it holds, steps 1-7.
- **New boundary test:** `tests/legacy_presentation_boundary_test.py` has
  33 checks. The desktop state root is a temporary home this account
  owns, the account lookups are patched, and the config writer and loader
  record. It covers:
  - the home asked of the launcher;
  - identity, `_gui_home` staying, the reader, the seams and rule 27;
  - the two section rules agreeing;
  - every migration outcome, including too many slots and a state root
    that is a symlink, another account's or no account's;
  - migrate: refused, not needed, dry run, another project's config, written
    as the owner and reloaded;
  - the fallback refusal.

  17 of 17 mutations are killed, serially and on the clean tree. Each
  kill's own last error line was read (rule 23). A first run had four kills
  by incidental crashes. The migrate cases now refuse to read a config they
  should not reach, and a first-running check pins the home the walk asks the
  launcher for; every kill now names its property.
- **Green and identical to the baseline:** `desktop_detection_boundary_test`, `legacy_presentation_launch_test`, `legacy_presentation_migration_test`, `pane_rebind_boundary_test`, `presentation_layout_files_boundary_test`, `presentation_slot_contraction_test`, `presentation_windows_boundary_test`, `project_desktop_boundary_test`, `team_launcher_upgrade_cutover_test`, `workflow_pane_rebind_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | privileged front door | 36 | 1 | |
  | presentation | 11 | 5 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers |

- **`team_launcher_desktop_policy_owner`** has no `test_` functions; its whole output is identical on both trees (refused at namespace setup before any case), it names none of the moved code, and it is not claimed as coverage.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations.
- **Staged release:** it contains the module and loads it from the release
  root, with `_gui_home` still the launcher's, and `upgrade --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`0a82149`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 20,397 lines) | 1 (`legacy_presentation.py`, 255) |
| Where it sits | 2 regions, lines 5169–5311, 5320–5381 | one file |
| moved `def`/`class` in `team_launcher.py` | 8 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it): 12l,
window replacement.
- **Size:** `replace_presentation_window_command` alone; its closure also
  holds `switchyard_pane_launcher_for`.
- **The shared helper:** `switchyard_pane_launcher_for` is rebound
  3x and read through the
  launcher by four modules, so it should stay (rule 25).

That completes the desktop and presentation group apart from the launch
itself. `launch_project`'s closure is 35 definitions /
957 lines; the function alone is 487 lines, rebound on
39. It needs its own bounded split.

### SYRD-329 (slice 12l): presentation window replacement

Re-measured on `f722260`, with the call graph regenerated on that tree. The
design was posted **before** any edit; it also corrected a figure: the chat
summary of SYRD-328 had said the launcher ended at 20,201 lines, and it is
20,198. The graph's closure was `replace_presentation_window_command` and
`switchyard_pane_launcher_for`. **`switchyard_pane_launcher_for` stayed in the
launcher**: it is the pane launcher every window opens, rebound on 3 lines in
the suites and read through the launcher by four other modules
(`desktop_presentation`, `presentation_controller`, `presentation_reconnect`,
`role_sessions`), and the moved command reads it there too (rule 25).

`replace_presentation_window_command` (103 lines, baseline lines
18398–18500) moved unchanged into **`scripts/presentation_window_replacement.py`** (140 lines). Its
`layout_mode` default and the controller's layout are the leaf's objects,
from `scripts/layout_modes.py`.

**Boundaries.**
- **Seams (rule 24, every form scanned):** the suites rebind the window
  launch, the layout, the desktop account, the pane launcher, the layout path
  and the window title on the launcher, and `presentation_enabled` on the
  controller. The command reads every launcher name it uses through the
  launcher when it runs: the window scan (3 sites) and report (2), the worker
  listing (2), the pane launcher, the layout path, the layout, the window
  launch and title, and the desktop account (2). The launcher's one caller,
  the `replace-window` verb, keeps its name.
- **Rule 27:** the presentation controller stays the command's own import.
  Proof step 7 and the scope-aware scan (48 refactor modules) report zero.
- **Rules:** defaults unchanged (rule 6); nothing rebound moves (rule 20).
  The command is re-exported.
- **Not repaired:** the SYRD-322 defect is outside this slice and unchanged.

**Neighbour guards widened**, as stated in the design and as SYRD-321, 326
and 328 did. Each now counts the moved command's calls in
`presentation_window_replacement.py`, through the launcher, against the same baseline totals:
- `presentation_windows_boundary_test`: the window scan and report;
- `gui_window_launch_boundary_test`: the Konsole launch (its inline check
  became the same totals-across-files form);
- `desktop_detection_boundary_test`: the desktop account;
- `presentation_layout_files_boundary_test`: the layout path and the layout.

A bypass mutant of each is killed, and each tree's guards are green on it.

**Evidence.**
- **AST proof:** it holds, steps 1-7.
- **New boundary test:** `tests/presentation_window_replacement_boundary_test.py`
  has 39 checks. Every launcher name the command calls is patched, the
  windows and workers are lists the test owns, the signaller and euid are
  fakes and nothing waits. It covers:
  - the import, identity, the pane launcher staying, the leaf's layout
    default, the call sites, the seams and rule 27, checked first;
  - nothing to replace; a caller that is not root, refused before any
    signal;
  - SIGTERM to every window found, SIGKILL only to survivors, a failed signal
    reported, and a wait that ends as soon as the windows are gone;
  - a lost worker reported and never a target;
  - reopening through the controller, or from a fresh layout with the
    caller's or the launcher's pane launcher, and a failed reopen.

  19 of 19 mutations of the command and its boundary, and 4 of 4
  guard bypasses, are killed, serially and on the clean tree. Each kill's
  own error line was read (rule 23). A first run had four kills for the
  wrong reason: two bypassed seams reached real code before a check caught
  them, and two fixtures ran out of answers. The structural checks now run
  first, and those fixtures answer enough for their own check to decide.
  The kill of a dropped export shows an `AttributeError`, but that is the
  probe's stderr quoted inside the identity check's own assertion.
- **Green and identical to the baseline:** `desktop_approval_boundary_test`, `desktop_detection_boundary_test`, `desktop_policy_boundary_test`, `desktop_presentation_boundary_test`, `gui_window_launch_boundary_test`, `host_desktop_approval_test`, `legacy_presentation_boundary_test`, `legacy_presentation_launch_test`, `legacy_presentation_migration_test`, `operator_display_recovery_test`, `presentation_layout_files_boundary_test`, `presentation_reconnect_boundary_test`, `presentation_windows_boundary_test`, `syrd_66_cross_account_presentation_test`, `team_launcher_desktop_handoff_titles_test`, `team_launcher_legacy_desktop_policy_upgrade_test`, `team_launcher_presentation_titles_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | unprivileged presentation | 7 | 4 | |
  | presentation window recovery | 19 | 2 | |
  | presentation | 11 | 5 | |
  | desktop layout path | 8 | 4 | |
  | viewer | 21 | 5 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (+3, the export block) |

  `unprivileged presentation`'s two replace-window cases are among its
  baseline reds. With SYRD-322 the window scan finds no root-owned window
  (measured on `f722260`: `[]` for the case's fake root Konsole), so the
  command reports nothing to replace and returns 0. Those cases therefore
  give this slice no behavioural coverage; the new boundary test, with the
  scan patched, is what covers it.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), neither names the moved code, and neither is claimed as coverage.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `--help` output and exit codes are identical for all 36
  invocations, `replace-window` among them.
- **Staged release:** it contains the module and loads it from the release
  root, with `switchyard_pane_launcher_for` still the launcher's, and
  `replace-window --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`f722260`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 20,198 lines) | 1 (`presentation_window_replacement.py`, 140) |
| Where it sits | 1 region, lines 18398–18500 | one file |
| moved `def` in `team_launcher.py` | 1 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it). This
completes the desktop and presentation group apart from the launch itself.
`launch_project`'s closure is now 38 definitions /
987 lines. `project_window_title`,
`role_process_runner_for` and `_running_project_roles` joined it because their
other launcher caller moved out; the moved command still reads the first and
last through the launcher, so the launcher-internal closure overstates what
is `launch_project`'s alone. The function itself is 487 lines,
rebound on 39. Across the closure, 71
lines rebind: `launch_project` 39, `process_authority_board_compatibility` 6, `project_window_title` 6, `_process_snapshot` 4, `pane_pid_for_role` 4, `process_tree_command_names` 3, `_running_project_roles` 2, `worktree_ref` 2, `_konsole_command` 1, `_verify_pane_launcher_path` 1, `failed_role_command` 1, `pane_window_program` 1, `role_process_runner_for` 1.

This count unites an AST scan (assignment targets including tuples,
`setattr`, `patch`/`patch.object`, call keywords, dict keys) with a text scan
that also sees code a test embeds in a string. Against SYRD-328's
61: the three names that joined add 9, and
`pane_pid_for_role` gains one tuple-target restore
(`tests/tmux_session_argv_boundary_test.py:120`) the earlier scan missed.

Moving the whole closure would carry 71 rebinding
lines and every shared primitive with it. The recommended first child is
one cohesive, bounded responsibility: **live CLI and model detection and the
stale-provider-runtime drop**, 7 definitions / 157 lines
(`KNOWN_LIVE_CLI_NAMES`, `process_tree_argvs`, `_model_from_argv`, `live_cli_for_role`, `live_model_for_role`, `roles_with_stale_provider_runtime`, `_drop_roles_with_stale_provider_runtime`).
- **Callers:** `launch_project` calls `_drop_roles_with_stale_provider_runtime`;
  `sync_reload_config_to_live_sessions` calls `live_cli_for_role` and
  `live_model_for_role`.
- **Seams:** none of the seven is rebound, by either scan, or read outside
  the launcher.
- **What stays (rule 25):** the process and pane primitives they call.
  `pane_pid_for_role` is rebound 4x and read through
  the launcher by `tmux_session_argv` and `role_identity_cutover`;
  `_process_snapshot` is rebound 4x;
  `process_tree_command_names` is rebound 3x
  and read by `tmux_session_argv`; `tmux_pane_pid_args` stays with its only
  caller. The moved code would read them, and `role_process_runner_for`,
  through the launcher at call time.
- **Then:** the rest of `launch_project` (12n) is re-measured and split
  further.

### SYRD-330 (slice 12m): live CLI and model detection and the stale-provider-runtime drop

Re-measured on `2afb97e`, with the call graph regenerated on that tree.
Rebinding was scanned twice, and the scans agree: an AST scan (assignment
targets including tuples, `setattr`, `patch`/`patch.object`, call keywords,
dict keys) united with a text scan that also sees source a test embeds in a
string. The design was posted **before** any edit.

6 definitions moved unchanged into **`scripts/live_role_runtime.py`** (208 lines):
- `process_tree_argvs`, `_model_from_argv`, `live_cli_for_role`,
  `live_model_for_role`;
- `roles_with_stale_provider_runtime`, `_drop_roles_with_stale_provider_runtime`.

They are 156 lines, from 2 regions of the baseline: 2622–2681, 2715–2818.
`_layout_leaves` and `_running_project_roles`, which sit between them, stayed.

**One change from the recommended seven: `KNOWN_LIVE_CLI_NAMES` stayed.** It
is `set(SUPPORTED_CONFIG_CLI_NAMES)`, built when the launcher is imported, from
a launcher constant that 6 launcher lines name (its definition included) and
that `presentation_controller` and `role_runtime` also name. Moving the set would
need that shared constant or a top-level launcher import. It stays one object,
and `live_cli_for_role` reads it through the launcher when it runs.

**Boundaries.**
- **Seams (rule 24).** None of the 6 moved names is rebound, and nothing
  outside the launcher reads them; two suites call two of them as
  `team_launcher.<name>`, which the re-exports serve. The primitives the moved
  code calls stay (rule 25) and are read through the launcher when it runs:
  `pane_pid_for_role` (rebound on 4 baseline lines), `_process_snapshot` (4),
  `process_tree_command_names` (3), and `tmux_pane_pid_args` with its caller.
  So are `role_process_runner_for`, `_command_name`, `_role_cli_name`, the
  tmux kill argv, the session record's model, the four provider-state
  functions, and the calls between the moved functions themselves.
- **Call sites.** The launcher's three callers keep its names: `launch_project`
  (the drop), and `sync_reload_config_to_live_sessions` (both detectors).
- **Rule 27:** the moved code has no function-local imports. Proof step 7 and
  the scope-aware scan (49 refactor modules) report zero.
- **Accounts:** a stale role's session is still ended only through its own
  account's runner.
- **Rules:** defaults unchanged (rule 6); nothing rebound moves (rule 20).
  All 6 names are re-exported.

**Reader tables widened**, as named in the design. Each gained
`live_role_runtime` as a reader, and a bypass of each is killed:
- `tmux_session_argv_boundary_test` for `tmux_kill_session_args`;
- `session_records_boundary_test` for `_session_payload_model_for_role`.

The latter table checks only that the name appears; the new boundary test is
what pins that read to the launcher.

**Evidence.**
- **AST proof:** it holds, steps 1-7.
- **New boundary test:** `tests/live_role_runtime_boundary_test.py` has
  57 checks. The snapshot, pane pids, generations, runners and kill argv
  are its own fakes; no `ps` runs and no tmux is asked. The structural checks
  run first. It covers:
  - import, identity (the shared names staying), call sites, seams and
    rule 27;
  - the tree walk: no pane, a cycle, empty argv skipped, and copies;
  - the model argv forms;
  - one known CLI, two with the configured one breaking the tie, or none;
  - the argv model before the session record, and no tree walked without a
    pane;
  - which running roles are stale: no CLI, unreadable store, fresh, never
    recorded, older;
  - the drop: the notice first, each stale role ended by its own account's
    runner, a failure (a result with no code included) kept and unreconciled,
    and one sorted restart line.

  29 of 29 mutations of the moved code and its boundary, and 2 of 2
  reader-table bypasses, are killed, serially and on the clean tree. Each
  kill's own error line was read (rule 23). A first run had four kills for
  the wrong reason: two fixtures raised a KeyError, and two mutants were
  caught by the structural call count rather than by the behaviour they
  changed. The fixture now answers "never recorded", and those mutants now
  keep the call and change only the behaviour. Every kill names its property.
  The dropped export's kill shows an `AttributeError`, but that is the probe's
  stderr quoted inside the identity check's own assertion.
- **Green and identical to the baseline:** `first_run_login_inheritance_test`, `launch_without_model_probes_test`, `legacy_presentation_launch_test`, `pane_hooks_boundary_test`, `provider_runtime_state_boundary_test`, `provider_state_written_as_owner_test`, `role_command_boundary_test`, `role_identity_cutover_boundary_test`, `role_sessions_boundary_test`, `team_launcher_authority_before_deploy_test`, `team_launcher_hermes_session_isolation_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`.
- **Green on both trees, with the one added check each:**
  `tmux_session_argv_boundary_test` (27 checks ok → 28 checks ok) and
  `session_records_boundary_test` (19 checks ok → 20 checks ok).
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (−160, the launcher's net change) |

  `viewer`'s `test_reload_syncs_live_cli_and_model_from_process_argv_before_relaunch`
  is among its baseline reds, but it fails *after* the moved code has run.
  Through the suite's rebinding of `team_launcher._process_snapshot`, the
  reload reaches the sync and both detectors, and every assertion on the
  synced config passes: CLI `claude`, model `claude-sonnet-5`, read from the
  process argv. It then stops at the relaunch's `new-session` lookup (line
  1278), on both trees.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), neither names the moved code, and neither is claimed as coverage.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations. `scripts/team-launcher --help`, which carries the reload pane
  mode, is identical too.
- **Staged release:** it contains the module and loads it from the release
  root, with `KNOWN_LIVE_CLI_NAMES` and the primitives still the launcher's,
  and `start --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`2afb97e`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 20,096 lines) | 1 (`live_role_runtime.py`, 208) |
| Where it sits | 2 regions, lines 2622–2681, 2715–2818 | one file |
| moved `def` in `team_launcher.py` | 6 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it). This is
now `launch_project`'s closure: 27 definitions /
767 lines; the function itself is 487 lines,
rebound on 39. Across the closure, 62
lines rebind: `launch_project` 39, `process_authority_board_compatibility` 6, `project_window_title` 6, `role_process_runner_for` 3, `_running_project_roles` 2, `worktree_ref` 2, `_konsole_command` 1, `_verify_pane_launcher_path` 1, `failed_role_command` 1, `pane_window_program` 1. For `role_process_runner_for` that includes two lines of
this slice's boundary test: one real rebinding (the drop's fixture) and one
key in the test's own call-count table, which the dict-key form counts.

The graph sees only the launcher's own calls. `pane_pid_for_role`,
`_process_snapshot`, `process_tree_command_names`, `tmux_pane_pid_args` and
`KNOWN_LIVE_CLI_NAMES` left the closure: every path from `launch_project` to
them now runs through `live_role_runtime`, which calls them through the launcher.

- **Recommended next (12n): the reload's config sync,
  `sync_reload_config_to_live_sessions`** (50 lines, lines
  4970–5019 of this candidate's launcher).
  - **Cohesion:** it is the only consumer of this slice's detectors. It
    rewrites a role's CLI and model in the tenant config from what the
    role's live session runs, so it belongs in `live_role_runtime`.
  - **Seams:** it is not rebound, has no reader outside the launcher, and has
    one caller (`launch_project`).
  - **Read through the launcher:** the config reader and atomic writer
    (rebound 8 and 6), the role
    runner (3), the tmux has-session argv
    (2) and the session directory
    (2).
  - **Boundary:** it writes the tenant config, so the slice must pin that
    write's path and ownership.
- **After that (12o): the owner state directories.** `ensure_owner_state_dirs`
  and its three helpers, 4 defs / 43 lines, none rebound,
  one caller.

### SYRD-331 (slice 12n): the reload's config sync

Re-measured on `823f5f0`, with the call graph regenerated on that tree and
rebinding scanned in every form (AST and text, united). The design was posted
**before** any edit.

`sync_reload_config_to_live_sessions` (50 lines, baseline lines
4970–5019) moved unchanged into the **existing**
`scripts/live_role_runtime.py`, appended after the six SYRD-330 definitions
(the module goes from 208 to 262 lines).
- **Apart from one added import line, the module's earlier content is
  byte-identical**, docstring included, as the ticket asked (proof steps 1a
  and 3a, below).
- That line is a stdlib import, `from dataclasses import replace`. That is the very object the launcher holds (tested); nothing
  rebinds the launcher's `replace`. The two scan hits were a `"replace"` dict
  key and `patch.object(Path, "replace")`.
- The launcher's existing import from the module gained the one name.

**Boundaries.**
- **Seams (rule 24).** The function is not rebound and no other script reads
  it; `team_launcher_viewer_test` calls it as `team_launcher.<name>`. It reads
  through the launcher when it runs:
  - the two detectors, even though they share its module;
  - `_load_json` (rebound on 8 lines) and `_write_json_atomic` (6);
  - `role_process_runner_for`;
  - `tmux_has_session_args` (2);
  - `role_session_dir` (2).
- **Call sites.** `launch_project` (reload mode) keeps calling it by the
  launcher's own name.
- **Write security.** The tenant config is written only through the
  launcher's atomic writer, to the caller's path. It is written once, after
  the config and every role are read, and only when a role changed; a
  refusal from the writer reaches the caller. Each role's session is asked
  through that role's own account runner.
- **Rule 27:** the function has no local imports. Proof step 7 and the
  scope-aware scan (49 refactor modules) report zero.

**The proof tool learned existing modules.** `equiv2.py` assumed every target
module was new, so this slice uses `equiv3.py`, which adds four steps:
- **1a:** the module's baseline nodes are all kept unchanged, and it gains
  nothing but imports besides the moved definitions (here: `from dataclasses
  import replace`, printed);
- **2a:** the launcher's existing import from the module names exactly its
  baseline names plus the moved ones;
- **3a:** every comment the module held at the baseline is kept;
- comment conservation counts only what the module gained.

Before relying on them, I broke the tree five ways, serially, and each was
caught: an existing function edited, a non-import node added, an existing
comment dropped, an extra name in the launcher's import, and the moved name
missing from it.

**Guards that followed the moved code, with nothing weakened:**
- `live_role_runtime_boundary_test`'s launcher-call table counts the
  detectors' one call each across the launcher and the module, through the
  launcher, with the same totals. It also counts the sync's own call in
  `launch_project`.
- Its moved-call table gained the sync's calls (`role_process_runner_for`
  1 → 2, plus the reader, writer, session directory, has-session argv and the
  two detectors).
- `tmux_session_argv_boundary_test`'s reader table lists `live_role_runtime`
  for `tmux_has_session_args`.

**Evidence.**
- **AST proof:** it holds, steps 1–7 with 1a/2a/3a.
- **Boundary test (extended):** `tests/live_role_runtime_boundary_test.py` now
  has 76 checks. The sync's cases use small dataclasses for the config,
  so the real `replace` runs, plus a recording loader and writer, per-role
  runners and fake detectors, all feeding one event log. They pin:
  - no role list: no write;
  - junk entries, unknown, stopped, no-live-model and matching roles: nothing
    written, the config returned as is;
  - the model read before the CLI, from the role's session directory, and the
    CLI never asked without a live model;
  - each role's own runner, and its session asked through it;
  - a CLI change, with `live_commands` extended only when the new CLI is not
    already allowed;
  - a model change, and both together;
  - one write, to the caller's path, after every role is read, and the
    returned roles in the file's order;
  - the writer's refusal reaching the caller.
- **Mutations:** 18 of 18 mutations of the sync, the 29 SYRD-330 mutations
  re-run on this tree, and 1 reader-table bypass(es) are all killed, serially
  on the clean tree. Each kill's own error line was read (rule 23).
  - A first run had three kills for the wrong reason: two fixture KeyErrors,
    and a write-order mutant caught by the structural count.
  - The fake detectors now answer an unexpected role with a model and CLI no
    role has, so reading it shows as a change. The write-order mutant now
    moves the single write into the loop.
  - The two dropped-export kills show an `AttributeError`, but that is the
    probe's stderr quoted inside the identity check's own assertion.
- **Green and identical to the baseline:** `desktop_approval_boundary_test`, `first_run_login_inheritance_test`, `legacy_presentation_boundary_test`, `legacy_presentation_launch_test`, `project_desktop_boundary_test`, `provider_runtime_state_boundary_test`, `role_sessions_boundary_test`, `session_paths_boundary_test`, `session_records_boundary_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`, `upstream_report_boundary_test`.
- **Green on both trees, with the added checks:** `live_role_runtime_boundary_test` (57 checks ok → 76 checks ok); `tmux_session_argv_boundary_test` (28 checks ok → 29 checks ok).
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (+1, the export line) |

  **What the viewer suite covers:**
  - Its three direct cases call the sync, and pass on both trees: unchanged
    when live matches, when the role is not running, and when the model is
    unparseable.
  - `test_reload_syncs_live_cli_and_model_from_process_argv_before_relaunch`
    reaches the sync, and its synced-config assertions pass before it stops
    at the relaunch step (line 1278) on both trees.
  - That case covers the sync up to that line only, not the launch.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), neither names the moved code, and neither is claimed as coverage.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations. `scripts/team-launcher --help`, with the reload pane mode, is
  identical too.
- **Staged release:** it loads the module from the release root, with the sync
  and `replace` the launcher's objects, and `start --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`823f5f0`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 19,936 lines) | 1 (`live_role_runtime.py`, 262, with the detectors it consumes) |
| Where it sits | 1 region, lines 4970–5019, apart from its detectors | next to them |
| moved `def` in `team_launcher.py` | 1 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it).
`launch_project`'s closure is 26 definitions /
717 lines; the function itself is 487 lines,
rebound on 39. Across the closure, 63
lines rebind: `launch_project` 39, `process_authority_board_compatibility` 6, `project_window_title` 6, `role_process_runner_for` 4, `_running_project_roles` 2, `worktree_ref` 2, `_konsole_command` 1, `_verify_pane_launcher_path` 1, `failed_role_command` 1, `pane_window_program` 1.

`role_process_runner_for`'s 4 are: 1 baseline line (`role_sessions`), 2 real
fixture rebinds in `live_role_runtime_boundary_test`, and 1 key in that test's
own call-count table.

- **Recommended next (12o): the owner state directories.**
  `ensure_owner_state_dirs`, `install_owner_state_dir_args`,
  `_owner_state_roots` and `_is_owner_state_path`: 4 defs /
  43 lines, none rebound, none read outside the launcher, one
  caller (`launch_project`).
  - **Read through the launcher:** `current_user_name` (rebound on
    115 lines), `runtime_dir_for_uid` (15)
    and `_proc_failure_reason` (1).
  - **Security boundary:** they run `install -d -m 700 -o <owner> -g <owner>`
    through the caller's runner, only for paths inside the owner's home or
    runtime directory. They resolve that home from the real passwd database
    (`pwd.getpwnam`), so the slice's tests must replace that lookup and never
    reach a real home.
- **Then (12p):** the rest of `launch_project`.

### SYRD-332 (slice 12o): owner state directories

Re-measured on `e729f9a`, with the call graph regenerated on that tree and
rebinding scanned in every form (AST and text, united). The design was posted
**before** any edit.

4 definitions moved unchanged into **`scripts/owner_state_dirs.py`** (84 lines):
`install_owner_state_dir_args`, `_owner_state_roots`, `_is_owner_state_path`
and `ensure_owner_state_dirs`. They are 43 lines, from one region
of the baseline: 2792–2840.

**Boundaries.**
- **Seams (rule 24).** None of the four is rebound, and no other script reads
  them; `team_launcher_env_config_test` calls two as `team_launcher.<name>`.
  - `launch_project`, their one caller, keeps calling `ensure_owner_state_dirs`
    by the launcher's own name at its three sites.
  - The current user (rebound on 115 lines), the owner's runtime directory
    (15) and the failure reason (1) are read through the launcher when a
    function runs, and so are the calls between the four.
- **`pwd`.** The launcher's `pwd` name is rebound nowhere. Every one of the 35
  `getpwnam` rebinding lines patches the attribute on the shared `pwd` module
  (`team_launcher.pwd.getpwnam = …`, `patch.object(<mod>.pwd, "getpwnam")`).
  The module therefore takes its own `import pwd`, the very module the
  launcher holds, and every existing fake reaches it (tested by identity, and
  the new test installs its fake that very way).
- **Security boundary, unchanged:**
  - the argv is `install -d -m 700 -o <owner> -g <owner> <path>`;
  - it runs through the caller's runner, with nothing else passed;
  - it covers the session directory then the pane-state directory, each only
    when it resolves inside the owner's home or runtime directory;
  - nothing runs with no owner, or when the caller already is the owner;
  - a failed install stops the launch.
- **Rule 27:** no local imports. Proof step 7 and the scope-aware scan (50
  refactor modules) report zero.
- **Rules:** defaults unchanged (rule 6); nothing rebound moves (rule 20).
  All 4 names are re-exported.
- **Guards.** No guard counts these names' call sites or reads their text. The
  five suites that read `team_launcher.py` and match similar words concern
  other functions (`_ensure_owner_user_and_project_dir`, `install_owner`,
  `_owner_state_layout_output_path`).

**Evidence.**
- **AST proof:** it holds, steps 1–7.
- **New boundary test:** `tests/owner_state_dirs_boundary_test.py` has 42
  checks. The passwd lookup is its own fake, installed through
  `team_launcher.pwd.getpwnam` as the suites do; the runner records; every path
  is under a temporary root that is never written. It covers:
  - import, identity (`pwd` included), call sites, seams and rule 27, first;
  - the argv, and its refusal with no owner;
  - the roots: none with no owner (no lookup), none for an unknown account,
    home then runtime directory for the owner;
  - containment: the home itself, a child, the runtime directory, a sibling
    sharing the prefix, a `..` escape, the home's parent, outside;
  - nothing done without a different owner;
  - session directory then pane-state directory, only the contained ones;
  - a failed install stopping with path, owner and reason.

  22 of 22 mutations are killed, serially and on the clean tree.
  Each kill's own error line was read (rule 23). On a first run, three kills
  surfaced through a neighbouring check: a runtime-root mutant was caught by
  the structural call count, and two mutants by the failed-install case. That
  mutant now keeps its call, and the failed-install case runs after the
  owner-skip and order checks; each kill now names its property. The dropped
  export's kill shows an `AttributeError`, but that is the probe's stderr
  quoted inside the identity check's own assertion.
- **Green and identical to the baseline:** `legacy_presentation_launch_test`, `live_role_runtime_boundary_test`, `provider_state_written_as_owner_test`, `team_launcher_pane_commands_test`, `team_launcher_role_path_access_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | env config | 24 | 2 | |
  | pane paths | 9 | 2 | |
  | control repo | 7 | 3 | |
  | konsole | 14 | 2 | |
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | tenant control bridge | 20 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (−45, the launcher's net change) |

  Three `env config` cases reach the moved code, and all three pass on both
  trees:
  - the argv applied to a real temporary directory as the current user, with
    no other account touched;
  - the install skipped when the caller is the owner;
  - an explicit pane-state directory winning in `launch_project`.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), neither names the moved code, and neither is claimed as coverage.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with the
  functions and `pwd` the launcher's, and `start --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`e729f9a`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 19,885 lines) | 1 (`owner_state_dirs.py`, 84) |
| Where it sits | 1 region, lines 2792–2840 | one file |
| moved `def` in `team_launcher.py` | 4 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it).
`launch_project`'s closure is 22 definitions /
674 lines; the function itself is 487 lines,
rebound on 39. Across the closure, 63
lines rebind: `launch_project` 39, `process_authority_board_compatibility` 6, `project_window_title` 6, `role_process_runner_for` 4, `_running_project_roles` 2, `worktree_ref` 2, `_konsole_command` 1, `_verify_pane_launcher_path` 1, `failed_role_command` 1, `pane_window_program` 1.

- **Recommended next (12p): worktree preparation for a launch,
  `_prepare_project_worktrees_for_launch`** (19 lines).
  - **Seams:** it is not rebound and has one caller (`launch_project`).
  - **Where it goes:** it decides which roles' worktrees a launch prepares
    (all, only the stopped ones, or none refreshed). It belongs in the
    existing `scripts/project_worktrees.py`, beside `ensure_project_worktrees` and
    `WorktreeProvisionResult`, which it uses.
  - **How:** `ensure_project_worktrees` is rebound on 3
    lines, so the call stays through the launcher. That is an append to an
    existing module, which `equiv3` proves.
- **Then (12q): the board-authority preflight,
  `process_authority_board_compatibility`** (39 lines, rebound on 6). It is the
  mixed-version refusal before launch touches anything, and it keeps its
  function-local `UnixHTTPConnection` import (rule 27). After it, the rest of
  `launch_project`.

### SYRD-333 (slice 12p): the launch's worktree preparation

Re-measured on `a6a1d52`, with the call graph regenerated on that tree and
rebinding scanned in every form (AST and text, united). The design was posted
**before** any edit.

`_prepare_project_worktrees_for_launch` (19 lines, baseline lines
2668–2686) moved unchanged into the **existing**
`scripts/project_worktrees.py` (742 → 765 lines),
beside `ensure_project_worktrees` and `WorktreeProvisionResult`.
- **Apart from one added import line, the module's earlier content is
  byte-identical** (proof steps 1a and 3a).
- The module imported only `dataclass` from `dataclasses`. Widening that line
  would change a baseline node, so `from dataclasses import replace` is its
  own line. It gives the very object the launcher holds (tested).
- The launcher's existing import from the module gained the one name, in its
  sorted place.

**Boundaries.**
- **Seams (rule 24).** The function is not rebound and nothing outside the
  launcher reads it. `launch_project` calls it at one site, by the launcher's
  own name.
  - `ensure_project_worktrees` is rebound on 3 lines. It is called through the
    launcher at all three of the function's sites, even though it is defined
    in this module.
  - `WorktreeProvisionResult` is not rebound, but is built through the
    launcher at both sites, as SYRD-331 did for same-module names, so the
    baseline's global lookups stay patchable.
- **Behaviour, unchanged:**
  - nothing running: the caller's config is prepared, refreshed;
  - a control repository: a `replace` copy holding only the stopped roles, in
    config order, refreshed; with every role running, nothing is prepared;
  - no control repository: the caller's config, not refreshed;
  - running roles' failures are dropped; the caller's runner is handed on, and
    failures propagate.
- **Security:** the function makes no git, account or provider call itself; the
  owner-correct git boundary in `ensure_project_worktrees` and the lint's scope
  are unchanged.
- **Rule 27:** no local imports, and it uses the module's canonical `launcher`
  alias. Proof step 7 and the scope-aware scan (50 refactor modules) report
  zero.

**Guard followed the moved code.** `project_worktrees_boundary_test`'s
literal export list gained the name, and it is checked by identity in both
import orders.

**Evidence.**
- **AST proof (`equiv3`):** it holds, steps 1–7 with 1a/2a/3a.
- **Boundary test (extended):** `tests/project_worktrees_boundary_test.py` now
  has 24 checks. A fake `ensure_project_worktrees` patched on the
  launcher records the config, refresh flag and runner; no git, worktree or
  account is touched. It covers:
  - the call sites, seams, `replace` identity and rule 27, first;
  - each of the four branches, with the copy versus the caller's config and
    the role order;
  - the empty result;
  - the running roles' failures dropped;
  - the result built by the launcher's class, and a failure propagated.

  My first structural check said two `ensure_project_worktrees` sites; the
  baseline has three, which it caught. The count is now taken from the
  baseline. It also first flagged the return annotation as a bare read; the
  annotation is never evaluated, so the check now reads the body.
- **Mutations:** 17 of 17 are killed, serially and on the clean
  tree. Each kill's own error line was read (rule 23), and each names its
  property. The dropped export's kill shows an `AttributeError`, but that is
  the probe's stderr quoted inside the identity check's own assertion.
- **Green and identical to the baseline:** `first_run_setup_completion_test`, `legacy_presentation_launch_test`, `live_role_runtime_boundary_test`, `owner_state_dirs_boundary_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`, `worktree_inheritance_test`.
- **Green on both trees, with the added checks:** `project_worktrees_boundary_test` (8 checks ok → 24 checks ok).
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | control repo | 7 | 3 | |
  | env config | 24 | 2 | |
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (−20, the launcher's net change) |

  **Which existing cases really reach the moved function.** This was
  measured by tracing calls on the candidate, case by case, with the probe
  first shown to load the suites.
  - Passing on both trees, and reaching it through `launch_project`: `test_control_repository_bootstrap_failure_aborts_without_opening_window` (1x); `test_control_repository_launch_uses_role_worktrees_and_preserves_repository` (2x); `test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default` (1x).
  - Reaching it but red on both trees: `test_control_repository_launch_runs_bootstrap_as_configured_owner`, `test_launch_without_control_repository_does_not_owner_wrap_pgu_plan`.
  - `first_run_setup_completion`, which rebinds `ensure_project_worktrees`,
    drives `workflow_launcher.prepare_role` and never reaches this function.

- **Zero-case suites.** `tenant_control_bridge_e2e_test`, `team_launcher_desktop_policy_owner_test` and `team_launcher_declarative_workflow_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), and none is claimed as coverage -- including `declarative_workflow`, whose rebinding of `ensure_project_worktrees` never runs here.
- **Stubs:** No suite in this set hit a stub, on either tree.
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with the
  function, `replace` and `WorktreeProvisionResult` the launcher's objects, and
  `start --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`a6a1d52`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 19,840 lines), apart from the preparation it calls | 1 (`project_worktrees.py`, 765), beside it |
| moved `def` in `team_launcher.py` | 1 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it).
`launch_project`'s closure is 21 definitions /
655 lines; the function itself is 487 lines,
rebound on 39. Across the closure, 63
lines rebind: `launch_project` 39, `process_authority_board_compatibility` 6, `project_window_title` 6, `role_process_runner_for` 4, `_running_project_roles` 2, `worktree_ref` 2, `_konsole_command` 1, `_verify_pane_launcher_path` 1, `failed_role_command` 1, `pane_window_program` 1.

- **Recommended next (12q): the board-authority preflight,
  `process_authority_board_compatibility`** (39 lines, lines 2219–2257 of
  this candidate's launcher).
  - **What it is:** the mixed-version refusal before launch touches anything.
  - **Seams:** one call site, no outside reader, rebound on 6
    lines in `team_launcher_new_project_test.py`, `team_launcher_test_helpers.py`, `ticket_board_process_authority_test.py`.
  - **Imports:** it keeps its function-local
    `from scripts.ticket_board.write_client import UnixHTTPConnection`
    (rule 27). Its module-level globals are `json` and typing names; an
    automated scan also listed a lambda's parameters and the `except` name,
    which are its own.
- **Then (12r):** the rest of `launch_project`.

### SYRD-334 (slice 12q): the board-authority launch preflight

Re-measured on `f719692`, with the call graph regenerated on that tree and
rebinding scanned in every form (AST and text, united; both agree). The design
was posted **before** any edit.

`process_authority_board_compatibility` (39 lines, baseline lines
2219–2257) moved unchanged into a new module, **`scripts/board_authority_preflight.py`** (65 lines).
No existing module shares its coupling: its only globals are `json` and typing
names, and its one dependency is the ticket board's client, imported inside
the function.

**Boundaries.**
- **Seams (rule 24).** It is rebound on 6 lines, all on the launcher, in
  `team_launcher_new_project_test`, `team_launcher_test_helpers` and
  `ticket_board_process_authority_test`; no other script reads it.
  `launch_project` calls it at its one site by the launcher's own name, so
  every rebinding still reaches the launch (tested). An explicit re-export
  keeps it the very same object.
- **No launcher at all.** The function reads no launcher global, so the
  module never imports or names the launcher (tested), and nothing is routed
  through it.
  - `json` is the module's own import, the one module object; the launcher's
    `json` is rebound nowhere.
  - The names the function binds stay its own (rule 27): the parameters, the
    factory, connection, response, body and payload, the lambda's
    `socket_path`/`timeout`, `exc`, and the function-local
    `from scripts.ticket_board.write_client import UnixHTTPConnection`, which
    the lambda captures by its bare name.
- **Behaviour, unchanged:**
  - the legacy bypass opens nothing;
  - it connects to the project's board socket with a 3-second timeout, through
    an injected factory or the board client, and sends one
    `GET /api/runtime-assignments`;
  - the body is decoded with `errors="replace"`;
  - it refuses, each with its exact message, on a non-200 status, another
    project, legacy uid authority, missing assignments, or any exception;
  - the connection is closed however the probe ends.
- **In `launch_project`:** the preflight is still its third statement, right
  after mode validation and before any layout, worktree, state or tmux change.
- **Security/CLI:** no new socket, board or network behaviour; no CLI change.

**Evidence.**
- **AST proof (`equiv3`, new module):** it holds, steps 1–7. The scope-aware
  scan (51 refactor modules) reports zero.
- **New boundary test:** `tests/board_authority_preflight_boundary_test.py`
  has 25 checks. Every connection is a fake; no socket is opened. It
  covers:
  - the import (nothing of Switchyard's), identity (`json` too), the call site,
    and the board client staying a local import captured by the lambda;
  - the legacy bypass, the socket, timeout and request, and every refusal and
    its message, including a replaced byte and an unparsable body;
  - the connection closed after a failed request, a failed read and success;
  - the default connection built from the board client (patched, never
    connecting);
  - `launch_project` asking the launcher's rebound preflight and returning 1,
    with the message, before any runner is used.
- **Mutations:** 18 of 18 are killed, serially and on the clean
  tree. Each kill's own error line was read (rule 23), and each names its
  property. The unparsable-body case catches an escaping exception, so the
  mutant that narrows the `except` fails that check rather than crashing. The
  dropped export's kill shows an `AttributeError`, but that is the probe's
  stderr quoted inside the identity check's own assertion.
- **Green and identical to the baseline:** `legacy_presentation_launch_test`, `listener_board_url_test`, `notification_routing_unresolved_test`, `operator_display_recovery_test`, `privileged_helper_boundary_test`, `switchyard_publish_ref_test`, `team_launcher_attach_by_role_test`, `team_launcher_tmux_panes_test`, `ticket_board_process_authority_test`.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | new project | 18 | 1 | |
  | runtime registration wait | 11 | 1 | |
  | control repo | 7 | 3 | |
  | env config | 24 | 2 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (−38, the launcher's net change) |

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), neither names the moved code, and neither is claimed as coverage.
- **Stubs:** Only `runtime_registration_wait_test` hit one, identically on both trees: the refusing sudo stub blocked `sudo -u testing-agent -H tmux list-panes`.
- **Finding, pre-existing and outside this slice.** `runtime_registration_wait_test`'s fixture config names `board_socket: /run/testing-ticket-board/ticket-board.sock`, the live `testing` tenant's board (`/run/testing-ticket-board` is `boardsvc:testing-agent`, mode 0750). Running it makes `provider_runtime_state` (not the moved function) try to connect there, and it is refused only by those permissions (`[Errno 13] Permission denied`), identically on baseline and candidate. No connection was made and nothing was changed. Run as the tenant, root or `boardsvc`, it would reach the live board. I did not change that suite here; it needs its own ticket.
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with the
  function and `json` the launcher's and the board client not loaded, and
  `start --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`f719692`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 19,820 lines) | 1 (`board_authority_preflight.py`, 65) |
| Where it sits | 1 region, lines 2219–2257 | one file |
| moved `def` in `team_launcher.py` | 1 | 0 |

**Next slice, measured on this candidate** (graph regenerated on it).
`launch_project`'s closure is 20 definitions /
616 lines; the function itself is 487 lines,
rebound on 39. Across the closure, 57
lines rebind: `launch_project` 39, `project_window_title` 6, `role_process_runner_for` 4, `_running_project_roles` 2, `worktree_ref` 2, `_konsole_command` 1, `_verify_pane_launcher_path` 1, `failed_role_command` 1, `pane_window_program` 1.

What remains is mostly shared helpers that other modules read through the
launcher (`project_window_title`, `role_process_runner_for`, `worktree_ref`,
`pane_window_program`, `_running_project_roles`, `_env_truthy_any`) and
`launch_project` itself.

- **Recommended next (12r): Konsole layout-command quoting.**
  `_KONSOLE_SAFE_CHARACTER`, `_KONSOLE_SAFE_WORD`, `_konsole_quote`,
  `_konsole_command` and `failed_role_command`: 5 defs /
  50 lines, one cohesive rule for writing a Konsole layout's
  `Command`.
  - **Readers:** only `presentation_layout_files`, which builds those layouts
    and reads `_konsole_command` and `failed_role_command` through the launcher.
  - **Seams:** that suite rebinds each on 1 line, so they must stay reachable
    through the launcher.
  - **Where:** they belong appended to `scripts/presentation_layout_files.py`.
- **Then (12s):** the pane-launcher check `_verify_pane_launcher_path` (23 lines,
  rebound on 1), which keeps reading the shared `pane_window_program`
  through the launcher. After that, `launch_project` itself needs a
  phase-by-phase plan of its own.

### SYRD-335 (slice 12r): Konsole layout-command quoting

Re-measured on `1aa7e71`, with the call graph regenerated on that tree and
rebinding scanned in every form (AST and text, united; both agree). The design
was posted **before** any edit.

5 definitions (50 lines, baseline regions 2365–2401, 2672–2688) moved
unchanged into the **existing** `scripts/presentation_layout_files.py`
(256 → 323 lines), beside the layout writer that
is their one outside reader. They are `_KONSOLE_SAFE_CHARACTER`,
`_KONSOLE_SAFE_WORD`, `_konsole_quote`, `_konsole_command` and
`failed_role_command`.
- **Apart from two added import lines, the module's earlier content is
  byte-identical** (proof steps 1a and 3a). The lines are `import re` and
  `import shlex`, the very modules the launcher holds; nothing rebinds the
  launcher's `re` or `shlex`.
- The launcher's existing import block from the module gained the five names,
  in sorted place. (A first edit added a second block; I merged it into the
  existing one before any test ran.)

**Boundaries.**
- **Seams (rule 24).** `_konsole_command` and `failed_role_command` are rebound
  on 1 line each, in `presentation_layout_files_boundary_test`; nothing else is
  rebound.
  - The layout writer keeps reading both through the launcher.
  - Every call and pattern read between the moved definitions also goes
    through it: `failed_role_command` → `_konsole_command` → `_konsole_quote`
    → the two patterns.
  - The baseline totals hold across both files, and `launch_project` keeps
    its one call of `failed_role_command` by the launcher's own name.
- **Rule 27:** no local imports; parameters and comprehension variables stay
  local. Proof step 7 and the scope-aware scan (51 refactor modules) report
  zero.
- **Behaviour, unchanged**, as the golden table below proves:
  - control characters and DEL become a space;
  - a safe word is left bare;
  - anything else without an apostrophe is single-quoted;
  - with an apostrophe, every unsafe character is backslash-escaped;
  - arguments are joined by one space;
  - the failed pane is `sh -c`, with an optional POSIX-quoted title escape,
    ending in `exec sleep infinity`.

**Guard followed the moved code, strengthened.** In
`presentation_layout_files_boundary_test`:
- the literal export list gained the five names;
- `_konsole_command` and `failed_role_command` joined its "never read past the
  launcher" set;
- a new check counts the quoting calls and pattern reads through the launcher
  against the baseline totals.

**Evidence.**
- **AST proof (`equiv3`):** it holds, steps 1–7 with 1a/2a/3a.
- **Boundary test (extended):** `tests/presentation_layout_files_boundary_test.py`
  now has 82 checks.
  - **The golden table:** 19 quoted values, 4
    commands and 3 failed-pane commands, plus both
    patterns. It was generated by running the baseline launcher's own functions
    in a pristine `1aa7e71` checkout, never typed by hand. The moved
    functions reproduce every entry. The values cover spaces, the empty
    string, shell metacharacters, `'`, `"`, `\`, newline, tab, other controls,
    DEL, non-ASCII and a non-string.
  - Also: the seams, `re`/`shlex` identity and rule 27, first; and each
    launcher rebinding (quote, command, both patterns) reaching the moved
    code.
- **Mutations:** 18 of 18 are killed, serially and on the clean
  tree. Each kill's own error line was read (rule 23). One was first caught by
  the structural read count; its mutant now keeps the read, and every kill
  names its property. The dropped export's kill shows an `AttributeError`, but
  that is the probe's stderr quoted inside the identity check's own assertion.
- **Green and identical to the baseline:** `desktop_presentation_boundary_test`, `gui_window_launch_boundary_test`, `legacy_presentation_launch_test`, `legacy_presentation_migration_test`, `presentation_reconnect_boundary_test`, `team_launcher_presentation_titles_test`. Two caveats:
  - `team_launcher_presentation_titles_test`'s real-Konsole cases returned
    early behind the stubs, so they give no evidence here;
  - `legacy_presentation_launch_test` includes the case described in the
    breach below.
- **Green on both trees, with the added checks:** `presentation_layout_files_boundary_test` (42 checks ok → 82 checks ok).
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | unprivileged presentation | 7 | 4 | |
  | desktop layout path | 8 | 4 | |
  | konsole | 14 | 2 | |
  | switchyard commands | 10 | 2 | |
  | presentation window recovery | 19 | 2 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (−53, the launcher's net change) |

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), and neither is claimed as coverage.
- **Stubs:** Every suite ran behind refusing `konsole`, `dbus-daemon` and `dbus-send` stubs ahead of `/usr/bin` (besides the sudo/service/provider/tmux ones); no log shows a stub refusal. `team_launcher_presentation_titles_test`'s real-Konsole cases were stopped: its `dbus-daemon` is found through `shutil.which`, hit the stub, and each case returned early.
- **Constraint breach, disclosed.** The design promised no real GUI invocation. `legacy_presentation_launch_test`'s `_run_konsole` launches `konsole` with its own child environment, `PATH=/usr/bin:/bin`, so my PATH stub only fooled its `shutil.which` gate: `test_konsole_hands_the_helper_exactly_the_arguments_we_staged` started the real Konsole once per tree, on the offscreen Qt platform, with sandboxed HOME and XDG directories under a temporary root, and terminated it. No display, desktop, window, account or tenant was touched. Its passes are not counted as evidence here; the quoting evidence is the golden table taken from the baseline's own functions. I did not run that suite again, and earlier slices (which ran it with no stub) also launched that offscreen Konsole. A PATH stub cannot stop that case; it needs to be excluded per case or run under a mount namespace that hides the binary.
- **Excluded:** `runtime_registration_wait_test` was not run, because its
  fixture names the live `testing` board socket (SYRD-334). It does not
  reach this code.
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with the
  quoting, the patterns, `shlex` and `failed_role_command` the launcher's
  objects, and `start --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`1aa7e71`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 19,782 lines), apart from the layout writer that reads it | 1 (`presentation_layout_files.py`, 323), beside it |
| Where it sits | 2 regions, lines 2365–2401, 2672–2688 | one file |

**Next slice, measured on this candidate** (graph regenerated on it).
`launch_project`'s closure is 15 definitions /
566 lines; the function itself is 487 lines,
rebound on 39. Across the closure, 55
lines rebind: `launch_project` 39, `project_window_title` 6, `role_process_runner_for` 4, `_running_project_roles` 2, `worktree_ref` 2, `_verify_pane_launcher_path` 1, `pane_window_program` 1.

- **Recommended next (12s): the pane-launcher check, `_verify_pane_launcher_path`**
  (23 lines, lines 2638–2660 of this candidate's
  launcher).
  - **Seams:** one call site, in `launch_project`; rebound on 1 line in
    `desktop_access_test`.
  - **What it does:** it refuses a configured pane launcher that the owner
    cannot execute, and a release without the inert pane window beside it,
    both through the caller's runner.
  - **What stays:** it reads the shared `pane_window_program`, which four
    modules read through the launcher, so that stays and is read through the
    launcher.
- **Then (12t): `launch_project` itself.** The rest of its closure is shared
  helpers other modules read through the launcher, so the next step is a
  phase-by-phase plan for the function, not more closure moves.

### SYRD-337 (slice 12s): the pane-launcher executable preflight

Re-measured on `ad85b30`, with the call graph regenerated on that tree and
rebinding scanned in every form (AST and text, united; both agree). The design
was posted **before** any edit.

`_verify_pane_launcher_path` (23 lines, baseline lines 2638–2660)
moved unchanged into a new module, **`scripts/pane_launcher_preflight.py`** (52 lines). No
extracted module owned pane-launcher executability.

**Boundaries.**
- **Seams (rule 24).** It is rebound on 1 line: `desktop_access_test` patches
  it on the launcher to prove a launch refuses a missing desktop policy
  before this check. `launch_project` calls it at its one site by the
  launcher's own name, so that patch still reaches the launch. An explicit
  re-export keeps it the very same object.
- **`pane_window_program` stays in the launcher**, as the ticket requires,
  because four modules read it. The moved check reads it through the
  launcher when it runs. After the move the launcher defines it but no
  longer calls it, and no guard counts those calls.
- **Rule 27:** no local imports; the function's own names stay local. Proof
  step 7 and the scope-aware scan (52 refactor modules) report zero.
- **Behaviour, unchanged:**
  - with no configured launcher, the caller's own path object comes back,
    unprobed;
  - otherwise `test -x <launcher>` runs through the caller's runner, and a
    refusal names the owner when there is one;
  - then `test -x <inert window>` runs;
  - the first refusal stops the launch, with no shell fallback;
  - success answers the configured launcher object.

**Evidence.**
- **AST proof (`equiv3`, new module):** it holds, steps 1–7.
- **New boundary test:** `tests/pane_launcher_preflight_boundary_test.py` has
  26 checks. The runner answers `test -x` from a table, and the window
  lookup is patched on the launcher; nothing is probed or run. It covers:
  - the import, identity (the lookup staying), the call site, the seam and
    rule 27;
  - the shortcut's identity with no probe;
  - success;
  - both refusals, with and without an owner;
  - the probe order;
  - no second probe after the first refusal.
- **Mutations:** 14 of 14 are killed, serially and on the clean
  tree. Each kill's own error line was read (rule 23). Two were first caught
  for the wrong reason: a fixture KeyError, and the structural lookup count.
  The runner table now answers unknown paths, and the reorder mutant keeps its
  single lookup, so each kill now names its property. The dropped export's kill
  shows an `AttributeError`, but that is the probe's stderr quoted inside the
  identity check's own assertion.
- **Green and identical to the baseline:** `desktop_presentation_boundary_test`, `presentation_layout_files_boundary_test`, `presentation_reconnect_boundary_test`, `team_launcher_tmux_panes_test`.
- **`legacy_presentation_launch_test`, case by case:** 13 of 15 pass
  on both trees, identically. 2 cases are **excluded by name** because
  they launch the real Konsole with a pinned child PATH:
  `test_konsole_hands_the_helper_exactly_the_arguments_we_staged` and
  `test_a_title_konsole_cannot_carry_is_reported_as_what_arrives`. No real GUI
  ran in this slice.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | desktop access | 3 | 1 | |
  | env config | 24 | 2 | |
  | control repo | 7 | 3 | |
  | konsole | 14 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (−22, the launcher's net change) |

  `desktop_access_test`'s rebinding case, `test_launcher_prelaunch_and_persistence`,
  passes on both trees.

  **Which existing cases reach the moved check**, traced case by case on
  the candidate:
  - passing on both trees, through `launch_project`: `test_control_repository_bootstrap_failure_aborts_without_opening_window` (1x); `test_control_repository_launch_uses_role_worktrees_and_preserves_repository` (2x); `test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default` (1x);
  - reaching it but red on both trees: `test_control_repository_launch_runs_bootstrap_as_configured_owner`, `test_launch_without_control_repository_does_not_owner_wrap_pgu_plan`.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), and neither is claimed as coverage.
- **Stubs:** Every suite ran behind refusing `konsole`, `dbus-daemon` and `dbus-send` stubs besides the sudo/service/provider/tmux ones, and no log shows a stub refusal. Before running, I checked each suite for spawns with a pinned child PATH: only Python probe helpers and the bridge e2e (refused at namespace setup) have one, and none references a GUI program.
- **Not run:** `runtime_registration_wait_test`, which has a live board socket
  in its fixture (SYRD-336).
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with the
  check the launcher's and `pane_window_program` still only the launcher's,
  and `start --help` exits 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`ad85b30`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 19,729 lines) | 1 (`pane_launcher_preflight.py`, 52) |
| Where it sits | 1 region, lines 2638–2660 | one file |

#### The plan for `launch_project` itself (measured on SYRD-337's candidate)

**The closure is exhausted.** `launch_project`'s remaining closure is
12 definitions / 539 lines, and
6 of them are shared, read by other modules through the
launcher: `TENANT_CONTROL_CALLER_ENV`, `project_window_title`, `role_process_runner_for`, `worktree_ref`, `_env_truthy_any`, `_running_project_roles`. The rest are tiny (two
self-deploy switches, the tenant-control check, the viewer role filter,
`_env_truthy`). Across the closure, 53 lines rebind:
`launch_project` 39, `project_window_title` 6, `role_process_runner_for` 4, `_running_project_roles` 2, `worktree_ref` 2.

**So the next work is `launch_project` itself:** 487 lines
(lines 4799–5285), rebound on
39 lines, and 42 top-level statements. Those statements fall
into nine phases. For each, the table gives the locals it reads that were
defined earlier (parameters included), the locals it hands on to later
phases, and whether it can end the launch early. All of this was computed
from the AST of this candidate:

| Phase | What it does | Lines | Size | Reads | Hands on | Can return |
|---|---|---|---:|---|---|---|
| P1 | mode and board-authority preflight | 4825–4836 | 12 | `config`, `dry_run`, `mode`, `print_func` | `mode`, `reason` | yes |
| P2 | reload migration | 4837–4848 | 12 | `config`, `config_path`, `dry_run`, `mode`, `print_func` | `config` | no |
| P3 | runners, owner delegation and paths | 4849–4870 | 22 | `assign_layout_owner`, `config`, `config_path`, `layout_output`, `pane_state_dir`, `runner`, `script_path` | `delegate_role_sessions_to_owner`, `effective_pane_state_dir`, `output_path`, `pane_script_path`, `role_process_runner`, `should_assign_layout_owner`, `window_title`, `worktree_runner` | no |
| P4 | layout upgrade, desktop, pane-launcher check | 4871–4879 | 9 | `config`, `config_path`, `dry_run`, `mode`, `print_func`, `runner`, `script_path`, `worktree_runner` | `config`, `pane_script_path` | no |
| P5 | pre-launch preparation (checkout, runtime user, state dirs, running roles, stale runtimes, worktrees, hooks, isolation) | 4880–4958 | 79 | `allow_stale_launcher`, `config`, `config_path`, `dry_run`, `effective_pane_state_dir`, `mode`, `no_launcher_self_deploy`, `owner_home`, `pane_script_path`, `print_func`, `reason`, `runner`, `worktree_runner` | `config`, `failed_roles`, `reason`, `reconcile_home`, `role`, `running_roles`, `unreconciled_roles` | yes |
| P6 | layout, plan and dry run | 4959–5027 | 69 | `config`, `config_path`, `dry_run`, `failed_roles`, `force_reload`, `layout_environ`, `layout_mode`, `mode`, `output_path`, `pane_script_path`, `pane_state_dir`, `role`, `runner`, `should_assign_layout_owner`, `window_title` | `resolved_layout_mode`, `role` | yes |
| P7 | detached worker start | 5028–5076 | 49 | `allow_stale_launcher`, `config`, `config_path`, `delegate_role_sessions_to_owner`, `effective_pane_state_dir`, `failed_roles`, `force_reload`, `mode`, `pane_script_path`, `reason`, `role`, `runner` | `launch_started_at`, `launch_started_ns`, `record_worker_start_failure`, `result`, `role`, `use_runtime_presentation`, `worker_start_exit_code` | yes |
| P8 | opening the presentation (viewer / runtime presentation / Konsole) | 5077–5244 | 168 | `allow_stale_launcher`, `config`, `config_path`, `delegate_role_sessions_to_owner`, `effective_pane_state_dir`, `failed_roles`, `force_reload`, `konsole_process_launcher`, `layout_environ`, `layout_mode`, `layout_output`, `mode`, `output_path`, `pane_script_path`, `print_func`, `record_worker_start_failure`, `resolved_layout_mode`, `result`, `role`, `role_process_runner`, `runner`, `use_runtime_presentation`, `window_title` | `resolved_layout_mode`, `role` | yes |
| P9 | post-launch reports and records | 5248–5285 | 38 | `config`, `config_path`, `effective_pane_state_dir`, `failed_roles`, `launch_started_at`, `launch_started_ns`, `mode`, `print_func`, `reconcile_home`, `report_session_records`, `resolved_layout_mode`, `role`, `running_roles`, `session_record_poll`, `session_record_timeout`, `unreconciled_roles`, `worker_start_exit_code` | — | yes |

**Hazards the table exposes:**
- **Reused loop and comprehension names.** `role`, `reason` and `result`
  appear as handed on between phases. They are loop and comprehension
  variables reused across phases, and this whole-function scan does not
  separate scopes. Before any cut, a scope-aware check must confirm that no
  phase really reads a value an earlier phase left behind.
- **Shared exit-code state.** `record_worker_start_failure` (P7) is a closure
  that assigns `worker_start_exit_code` with `nonlocal`. P8 calls it, and P9
  returns the code. P7 and P8 therefore share one small mutable result, and
  whichever moves first must pass that result explicitly, not re-create it.
- **Patch sites.** `launch_project` is rebound on 39 lines,
  and its phases call many names the suites patch on the launcher
  (`materialize_layout`, `launch_konsole_window`, the `ensure_*` steps and
  others). Every phase helper must read those through the launcher when it
  runs, and `launch_project` must keep its name, signature and defaults.

**Recommended order.** Each step is one bounded child helper that
`launch_project` calls, and each returns a small named result:
1. **P3, runners, owner delegation and paths (22 lines).** It
   is pure setup with no early return: seven inputs, and eight values handed
   on (the two runners, the delegation flag, the pane-state and layout paths,
   the window title, the layout-owner flag and the pane script). It is the
   cleanest first child.
2. **P5, pre-launch preparation (79 lines).** The checkout
   freshness, runtime user, owner state directories, running roles,
   stale-runtime drop, worktrees, hooks, board skill and isolation gaps. It
   returns the prepared config, failed/running/unreconciled roles and the
   reconcile home, or an early exit code.
3. **P6, layout, plan and dry run (69 lines).**
4. **P7 and P8 together:** detached worker start (49 lines),
   then opening the presentation (168 lines). They share the
   exit-code recorder above. P8 is the largest phase and itself splits into
   three branches (viewer, runtime presentation, Konsole), each its own
   child.
5. **P9, post-launch reports and records (38 lines).**

**P1, P2 and P4** (12, 12 and
9 lines) are guards and one-line calls. They stay inline as
the orchestration's own steps. The end state is `launch_project` as an
orchestration that reads its phases in order.

### SYRD-339 (slice 12t): `launch_project` P3, runners, owner delegation and paths

The first phase moved out of `launch_project` itself, as the SYRD-337 plan
recommended. It is re-measured on `6b483e9`, and the design was posted
**before** any edit.

**Scope-aware data flow** (new tool `phaseflow.py`):
- **What it counts:** reads in `launch_project`'s own scope, plus names a
  nested scope reads but does not bind. Loop, comprehension, lambda,
  nested-def and `except` names are kept apart.
- **Cross-check:** every store must be a local of `launch_project` in
  `symtable`, and every nested free name one of its locals.
- **P3 = statements [4..13], lines 4849–4870, 10
  statements:**
  - inputs, all parameters: `assign_layout_owner`, `config`, `config_path`, `layout_output`, `pane_state_dir`, `runner`, `script_path`;
  - outputs, read later: `delegate_role_sessions_to_owner`, `effective_pane_state_dir`, `output_path`, `pane_script_path`, `role_process_runner`, `should_assign_layout_owner`, `window_title`, `worktree_runner`;
  - internal only: `owner_runner_anchor`;
  - launcher globals: `_control_repository_owned_roots`, `_owner_process_runner`, `_owner_project_git_runner`, `current_user_name`, `default_layout_output_path`, `default_pane_state_dir_for_user`, `project_window_title`.

**The move.** A new module, **`scripts/launch_phases.py`** (87 lines), the
home for the phases as later slices extract them.
- It holds `LaunchSetup`, a frozen dataclass of the eight outputs in the order
  P3 assigns them.
- It holds `_launch_runners_and_paths`: P3's ten statements unchanged,
  comments and order included. The only change is that the seven launcher
  globals are read as `launcher.X` when it runs. It returns `LaunchSetup`.
- At P3's old position (after the P2 reload migration, before P4's layout
  upgrade, desktop and pane-launcher checks), `launch_project` now makes one
  call with each input passed as the same-named local, then one unpacking line
  per output.
- `launch_project`'s name, signature, defaults and 39 rebinding lines are
  untouched. The launcher goes from 19,707 to 19,706 lines.

**The phase proof.** `equiv_phase.py`, new and independent of the edit's own
logic, checks:
1. the helper body is the baseline phase, statement for statement, with
   `launcher.X` read as `X`, and nothing read through the launcher is a phase
   input or local (rule 27);
2. the parameters are the measured inputs, and the result is built from the
   measured outputs;
3. the result is frozen, with the outputs as its fields in order;
4. `launch_project` is the baseline before and after the phase, with exactly
   the declared call and unpacking between;
5. every other launcher node is unchanged, and the comments are conserved.

It holds. To test the proof itself, I planted 8 faults, and it caught
every one: a missing output, wrong wiring, the call moved after P4, two phase
statements swapped, a changed condition, an extra argument, a dropped comment,
and a phase input read through the launcher.

**Guard followed the moved code.** `presentation_layout_files_boundary_test`
counts `default_layout_output_path` across the launcher and its moved callers.
`launch_phases.py` joined `MOVED_CALLERS`, with the same total of 3. Before
widening, the guard failed on the candidate exactly there.

**Evidence.**
- **New boundary test:** `tests/launch_phases_boundary_test.py` has 40
  checks. Every launcher lookup is a recording fake patched on the launcher.
  It covers:
  - identity, the call site, the seams and rule 27, first;
  - the frozen result's fields;
  - no owner, the owner being the caller, and another owner with a repository
    anchor, a pane-launcher anchor, or none;
  - explicit paths, and the layout-owner rule over five cases;
  - a configured pane script;
  - a failing owner-runner builder, stopping the phase;
  - `launch_project` handing the phase its arguments, and the next phase using
    the returned worktree runner;
  - a dry run carrying the output path, pane script, layout-owner flag and
    title into the layout and plan;
  - a refused board preflight still coming before the phase.

  The wiring of `role_process_runner`, `delegate_role_sessions_to_owner` and
  `effective_pane_state_dir` is first read when workers start, which no fake
  here reaches; for those three, it is the proof's step 4.
- **Mutations:** 20 of 20 are killed, serially and on the clean
  tree. Each kill's own error line was read (rule 23). Two were first caught
  through a neighbouring check. One case was renamed to run in order, and the
  ordering mutant now truly moves the call above the preflight; every kill
  names its property.
- **Green and identical to the baseline:** `desktop_presentation_boundary_test`, `owner_state_dirs_boundary_test`, `pane_launcher_preflight_boundary_test`, `presentation_layout_files_boundary_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`.
- **`legacy_presentation_launch_test`, case by case:** 13 of 15 pass
  on both trees, identically, with its two real-Konsole cases excluded by name
  (SYRD-338). No real GUI ran.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | env config | 24 | 2 | |
  | control repo | 7 | 3 | |
  | desktop access | 3 | 1 | |
  | konsole | 14 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (+4, the new import block above those lines, the launcher's net change) |

  **Existing cases that really run the phase,** traced on the candidate:
  - passing on both trees: `test_control_repository_bootstrap_failure_aborts_without_opening_window` (1x); `test_control_repository_launch_uses_role_worktrees_and_preserves_repository` (2x); `test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default` (1x); `test_launcher_prelaunch_and_persistence` (1x);
  - reaching it but red on both: `test_control_repository_launch_runs_bootstrap_as_configured_owner`, `test_launch_without_control_repository_does_not_owner_wrap_pgu_plan`.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), and neither is claimed as coverage.
- **Stubs:** Every suite ran behind refusing `konsole`, `dbus-daemon` and `dbus-send` stubs besides the sudo/service/provider/tmux ones, and no log shows a stub refusal. Before running, I checked each suite for spawns with a pinned child PATH: only Python probe helpers and the bridge e2e (refused at namespace setup) have one, and none references a GUI program.
- **Not run:** `runtime_registration_wait_test` (live board socket, SYRD-336).
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with both
  names the launcher's, and `start --help` exits 0.

**The plan, updated.** P3 is done. **Next is P5, pre-launch preparation**, as
measured on this candidate:
- **Span:** statements [16..20], lines 4879–4957,
  5 statements.
- **Inputs (13):** `allow_stale_launcher`, `config`, `config_path`, `dry_run`, `effective_pane_state_dir`, `mode`, `no_launcher_self_deploy`, `owner_home`, `pane_script_path`, `print_func`, `reason`, `runner`, `worktree_runner`.
- **Outputs (6):** `config`, `failed_roles`, `reason`, `reconcile_home`, `running_roles`, `unreconciled_roles`. They include a
  rebound `config`.
- **Internal only:** `isolation_gaps`, `worktree_roles`.
- **Launcher globals, with rebinding lines:** `current_user_name` 118, `prepare_project_desktop` 11, `_owner_home_for_auth` 8, `role_isolation_gaps` 5, `ensure_generated_project_pane_hooks` 3, `_running_project_roles` 2, `ensure_launcher_checkout_current` 2, `_drop_roles_with_stale_provider_runtime` 1, `ensure_owner_state_dirs` 1, `sync_reload_config_to_live_sessions` 1; and, rebound nowhere:
  `LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV`, `NO_LAUNCHER_SELF_DEPLOY_ENV`, `_env_truthy_any`, `_prepare_project_worktrees_for_launch`, `ensure_configured_runtime_user`, `ensure_generated_project_board_skill`, `fetch_project_worktree_ref`, `seed_default_session_dir_from_legacy_sources`.
- **It can end the launch,** so its result must carry an early exit code
  beside the prepared values.
- **Before cutting it, one flow-sensitive check:** `reason` shows as both an
  input and an output. P1 stores `reason`, and P5 stores and reads it too. The
  scope-aware tool above is not flow-sensitive, so the P5 slice must first
  prove, by reaching definitions, whether any path in P5 reads P1's value and
  whether any later phase reads P5's. That decides whether `reason` belongs in
  its interface at all.

After P5 come P6, P7 with P8 (sharing the exit-code recorder, and P8 splitting
into its three branches), then P9, as planned in SYRD-337.

### SYRD-340 (slice 12u): `launch_project` P5, pre-launch preparation

Re-measured on `d274fc5`. The design was posted **before** any edit.

**A flow-sensitive interface first.** A new tool, `phaseflow2.py`, runs a
definite-assignment analysis over the structured AST.
- **How it works:** a read is exposed unless the name is definitely assigned
  on every path to it; paths that `return` or `raise` do not flow on; nested
  scopes contribute only their free reads.
- **Validation:** 10 synthetic cases pass, and it reproduces SYRD-339's P3
  interface exactly.
- **A bug it found:** SYRD-339's `phaseflow.py` walked into a nested `def` when
  that def was itself a top-level statement (statement [28],
  `record_worker_start_failure`), so it read that def's own local `reason` as
  a later read.
- **What it settled for `reason`:** it is neither an input nor an output of
  P5. P1's `reason` is read only in P1. P5 assigns it at 4937, reads it at
  4938, and returns at 4939 on that path, so it never escapes. `role` and
  `result` are loop, comprehension or nested-def names, not cross-phase state.

**P5 = statements [16..20], lines 4879–4957, 5 statements:**
- inputs (12): `allow_stale_launcher`, `config`, `config_path`, `dry_run`, `effective_pane_state_dir`, `mode`, `no_launcher_self_deploy`, `owner_home`, `pane_script_path`, `print_func`, `runner`, `worktree_runner`;
- outputs (5): `config`, `failed_roles`, `reconcile_home`, `running_roles`, `unreconciled_roles`;
- internal: `isolation_gaps`, `reason`, `worktree_roles`;
- two early exits, both `return 1`: the control-repository bootstrap refusal
  and the isolation-gaps refusal.

**The move.** Appended to `scripts/launch_phases.py` (87 → 229 lines):
- **`LaunchPreparation`,** frozen: `exit_code` (`None` to go on), then the
  five outputs.
- **`_prepare_launch`:** P5's statements unchanged, comments included, with its
  18 launcher globals read as `launcher.X`. Each `return 1` becomes a result
  return carrying `exit_code=1` and the five outputs, and it ends with
  `exit_code=None`.
- **`sys` and `print`:** nothing rebinds `sys` or `team_launcher.print`, so
  `import sys` is the module's own and `print` stays the builtin; stderr and
  print behaviour are unchanged.
- **The existing module content:** the docstring gained a P5 paragraph, the
  TYPE_CHECKING import gained `RoleConfig`, and `import sys` was added. P3 and
  `LaunchSetup` are byte-identical.

**In `launch_project`,** at P5's old position, the five statements became:
- the call;
- `if launch_preparation.exit_code is not None: return launch_preparation.exit_code`;
- five unpacking lines.

The same code is returned at the same point, after the same side effects.
Nothing moved before the P1/P2/P4 gates. The launcher goes from 19,706 to 19,650
lines.

**The phase proof,** `equiv_phase2.py`, extended for early exits and an
existing module:
- each transformed return must read back exactly as its original `return E`;
- the continue-return must carry `exit_code=None`;
- the caller scaffolding must be exactly the call, the dispatch and the
  unpacking;
- the module's baseline nodes must survive, the docstring only gaining and
  imports only gaining names.

It holds. I planted 9 faults and it caught every one:
- a changed condition;
- two statements swapped;
- wrong wiring;
- a missing early return;
- an early return with another code;
- the dispatch after the unpacking;
- a return carrying `reason`;
- a changed P3 statement;
- a shortened docstring.

The scope-aware scan was run on `launch_phases.py` **explicitly**, as
corrected in SYRD-339: `scope scan, launch_phases.py explicitly: []`.

**Guards that followed P5's calls,** with unchanged totals, through the
launcher. Each failed on the candidate first, and a bypass of each is killed:
- `live_role_runtime_boundary_test`: the stale-runtime drop and the reload
  sync;
- `owner_state_dirs_boundary_test`: `ensure_owner_state_dirs`, 3;
- `project_worktrees_boundary_test`: the launch's worktree preparation;
- `project_desktop_boundary_test`: `prepare_project_desktop`, 6.

My pre-edit inventory named only the first three. I had cut each search to
two matching lines, so the `project_desktop` table on the third was missed.
The suite comparison caught it, and an AST sweep of every `LAUNCHER_CALLS`
table and inline comparison then confirmed these four are all.

**Evidence.**
- **Boundary test (extended):** `tests/launch_phases_boundary_test.py` now has
  84 checks. P5's 18 lookups are recording fakes; the self-deploy
  constants are sentinels, which proves they too are read through the launcher.
  The cases are:
  - dry run;
  - attach-or-start, in order, with each runner and the objects handed back;
  - the reconcile home, given or looked up;
  - the self-deploy switches;
  - reload and plain attach;
  - partial versus complete control-repository failure (stderr);
  - the isolation refusal (`print_func`);
  - a failing step;
  - `launch_project` returning P5's code before the layout, after P4's
    checks, and handing on the prepared config and failures.
- **Mutations:** 26 of 26 mutations of P5 and its wiring, and 4 of 4
  guard bypasses, are killed, serially and on the clean tree.
  - Each kill's own error line was read (rule 23).
  - A first run had five wrong kills. In two, a bypass reached the real
    preparation code; it was stopped only by a refusing fake runner, and no
    real process ran. Two were incidental crashes, and one was a mislabelled
    mutant.
  - P5's structural check now runs first, P3's dry-run case stands P5 in, and
    the wiring case has a complete config. Every kill now names its property,
    and none comes from real code.
- **Green and identical to the baseline:** `desktop_policy_generation_test`, `first_run_login_inheritance_test`, `launcher_checkout_boundary_test`, `live_role_runtime_boundary_test`, `owner_state_dirs_boundary_test`, `pane_hooks_boundary_test`, `presentation_layout_files_boundary_test`, `project_desktop_boundary_test`, `project_onboarding_boundary_test`, `project_worktrees_boundary_test`, `provider_auth_status_boundary_test`, `session_records_boundary_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`.
- **`legacy_presentation_launch_test`, case by case:** 13 of 15 pass
  on both trees, identically, with both real-Konsole cases excluded by name.
  No real GUI ran.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | switchyard new prompts | 11 | 3 | |
  | desktop access | 3 | 1 | |
  | env config | 24 | 2 | |
  | control repo | 7 | 3 | |
  | konsole | 14 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (+2, the two import names added above those lines, the launcher's net change) |

  **Existing cases that really run P5,** traced on the candidate:
  - passing on both trees: `test_control_repository_bootstrap_failure_aborts_without_opening_window` (1x); `test_control_repository_launch_uses_role_worktrees_and_preserves_repository` (2x); `test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default` (1x); `test_an_ordinary_launch_reconciles_running_roles_end_to_end` (2x). The bootstrap-refusal case exercises
    P5's early exit;
  - reaching it but red on both: `test_control_repository_launch_runs_bootstrap_as_configured_owner`, `test_launch_without_control_repository_does_not_owner_wrap_pgu_plan`.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), and neither is claimed as coverage.
- **Stubs:** Every suite ran behind refusing `konsole`, `dbus-daemon` and `dbus-send` stubs besides the sudo/service/provider/tmux ones, and no log shows a stub refusal. Before running, I checked each suite for spawns with a pinned child PATH: only Python probe helpers and the bridge e2e (refused at namespace setup) have one, and none references a GUI program.
- **Not run:** `runtime_registration_wait_test` (SYRD-336).
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with P5 and P3
  the launcher's objects, and `start --help` exits 0.

**Next: P6, layout, plan and dry run**, measured flow-sensitively on this
candidate:
- **Span:** statements [23..26], lines 4902–4970,
  4 statements.
- **Inputs (14):** `config`, `config_path`, `dry_run`, `failed_roles`, `force_reload`, `layout_environ`, `layout_mode`, `mode`, `output_path`, `pane_script_path`, `pane_state_dir`, `runner`, `should_assign_layout_owner`, `window_title`.
- **Outputs: none.** `plan` and `resolved_layout_mode` are internal:
  `resolved_layout_mode` is assigned only in the dry-run branch, which returns,
  and P8 re-assigns it before reading.
- **One early exit,** `return 0` on a dry run.
- **Rebound launcher globals:** `current_user_name` 119, `materialize_layout` 8, `failed_role_command` 2, `role_run_as_user` 2, `worktree_ref` 2, `LAYOUT_MODE_AUTO` 1, `ensure_layout_output_owner` 1, `pane_command` 1, `resolve_layout_mode` 1. `LAYOUT_MODE_AUTO` is rebound on 1
  line, which that slice must confirm.
- **The interface:** its result needs only the exit code. After P6 come P7
  with P8 (sharing the exit-code recorder, P8 splitting into its three
  branches), then P9.

### SYRD-341 (slice 12v): `launch_project` P6, layout, plan and dry run

Re-measured on `c83111a` with `phaseflow2.py`, on a regenerated
exact-tree graph. The design was posted **before** any edit.

**P6 = statements [23..26], lines 4902–4970, 4 statements:**
- inputs (14): `config`, `config_path`, `dry_run`, `failed_roles`, `force_reload`, `layout_environ`, `layout_mode`, `mode`, `output_path`, `pane_script_path`, `pane_state_dir`, `runner`, `should_assign_layout_owner`, `window_title`;
- outputs: **none**. `plan` and `resolved_layout_mode` are internal:
  `resolved_layout_mode` is assigned only on the dry-run path, which returns,
  and P8 re-assigns it before any read;
- one early exit, `return 0` on a dry run, after the layout is written and
  the plan printed. The dry run still writes the layout, as on the baseline.

**`LAYOUT_MODE_AUTO`, confirmed.** SYRD-340's note counted one rebind. An AST
scan of the baseline finds none; the one text-scan hit
(`presentation_window_recovery_test`, a call argument
`team_launcher.LAYOUT_MODE_AUTO, environ=...`) matched the tuple-target
pattern and is not a store. The only rebinds on the candidate are the new
test's own sentinels.

**The move.** Appended to `scripts/launch_phases.py` (229 → 326 lines):
- **`_write_layout_and_plan`,** returning `int | None`: P6 has no outputs, so
  there is no result class. Its statements are unchanged, comments included,
  with P6's launcher globals read as `launcher.X`, looked up when the phase
  runs. The dry run's `return 0` is kept verbatim, and the phase ends with
  `return None`.
- **`json` and `print`:** an AST and text scan of the baseline finds no
  rebind of `team_launcher.json`, and the three text hits for `print` are
  `print_func=rec.print,` arguments in `agent_cli_host_wide_test`, not
  stores. So `import json` is the module's own and `print` stays the builtin;
  the plan's output is unchanged.
- **The existing module content:** the docstring gained a P6 paragraph and
  `import json` was added. P3, P5 and their result classes are byte-identical.

**In `launch_project`,** at P6's old position, the four statements became the
call by the launcher's own name and
`if layout_exit is not None: return layout_exit`. The same code is returned
at the same point, after the same side effects. The launcher goes from 19,650
to 19,600 lines.

**The phase proof,** `equiv_phase3.py` (no result class; returns kept
verbatim; a final `return None`; dispatch `if <local> is not None`), holds.
I planted 10 faults and it caught every one, among them an exit 0
taken for going on, a missing dispatch, a wrong input, reordered statements,
changed output formatting, a changed P5 node and the effective pane-state
directory handed to the layout. The scope-aware scan was run on
`launch_phases.py` **explicitly**: `scope scan, launch_phases.py explicitly: []`.

**Guards that followed P6's calls,** found by an AST sweep of every
`LAUNCHER_CALLS`-style table and inline count (no truncated search), with
unchanged totals, through the launcher. Each failed on the candidate first,
and a bypass of each is killed:
- `desktop_detection_boundary_test`: `launch_phases.py` joined
  `MOVED_CALLERS` for `resolve_layout_mode`, total 3;
- `presentation_layout_files_boundary_test`: the Konsole check counts
  `failed_role_command` in both its module and `launch_phases`, total 2.

**Evidence.**
- **Boundary test (extended):** `tests/launch_phases_boundary_test.py` now has
  118 checks. P6's lookups are recording fakes and the layout-mode
  constants sentinels, which proves they too are read through the launcher.
  The cases are the layout written first with the caller's pane-state
  directory, the owner hand-off through the caller's runner, the plan's
  visible, detached and failed roles, viewer and explicit modes, the dry run's
  sorted two-space JSON on stdout answering 0, and `launch_project` returning
  P6's 0 but going on at `None`, with P5's exit still before P6.
- **Containment.** Wiring cases stand in the phases they are not about, and a
  fence makes the worker start (P7) raise, so a mutant that lets the launch go
  on is caught by that fence rather than by running real later code.
- **Mutations:** 25 of 25 mutations of P6 and its wiring, and 2 of 2
  guard bypasses, are killed, serially and on the clean tree; each kill's own
  error line was read (rule 23), and every one names its property.
- **Green on both trees, with the same output:** `desktop_detection_boundary_test`, `first_run_login_inheritance_test`, `live_role_runtime_boundary_test`, `owner_state_dirs_boundary_test`, `pane_launcher_preflight_boundary_test`, `presentation_layout_files_boundary_test`, `project_desktop_boundary_test`, `project_worktrees_boundary_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`.
- **`legacy_presentation_launch_test`, case by case:** 13 of 15 pass
  on both trees, identically, with both real-Konsole cases excluded by name.
  No real GUI ran.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | presentation window recovery | 19 | 2 | |
  | env config | 24 | 2 | |
  | control repo | 7 | 3 | |
  | konsole | 14 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | desktop access | 3 | 1 | |
  | switchyard commands | 10 | 2 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (+1, the one import name added above those lines) |

  **Existing cases that really run P6,** traced on the candidate:
  - passing on both trees: `test_control_repository_launch_uses_role_worktrees_and_preserves_repository` (2x); `test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default` (1x); `test_an_ordinary_launch_reconciles_running_roles_end_to_end` (2x);
  - reaching it but red on both: `test_control_repository_launch_runs_bootstrap_as_configured_owner`, `test_launch_without_control_repository_does_not_owner_wrap_pgu_plan`.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions. Each one's whole output is identical on both trees (refused at namespace setup before any case), and neither is claimed as coverage.
- **Stubs:** Every suite ran behind refusing `konsole`, `dbus-daemon` and `dbus-send` stubs besides the sudo/service/provider/tmux ones. Before running, I checked each suite for spawns with a pinned child PATH: only Python probe helpers and the bridge e2e (refused at namespace setup) have one, and none references a GUI program. `team_launcher_presentation_titles_test` prints ok on both trees, but its real-D-Bus case finds the refusing stubs and returns early, so it is not claimed as coverage.
- **Not run:** `runtime_registration_wait_test` (SYRD-336).
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with P6, P5
  and P3 the launcher's objects, and `start --help` exits 0.

**Next: P7 with P8, worker start and presentation,** measured
flow-sensitively on this candidate.
- **P7 alone:** lines 4921–4969, 7 statements, 10 inputs,
  outputs `launch_started_at`, `launch_started_ns`, `presentation_controller`, `record_worker_start_failure`, `use_runtime_presentation`, `worker_start_exit_code`; returns at 4968 (`return result`).
- **P8 alone:** lines 4970–5137, 3 statements, 21 inputs,
  output `resolved_layout_mode`; returns at 5005 (`return result`), 5137 (`return launch_result`).
- **Together:** lines 4921–5137, 10 statements.
  - Inputs (18): `allow_stale_launcher`, `config`, `config_path`, `delegate_role_sessions_to_owner`, `effective_pane_state_dir`, `failed_roles`, `force_reload`, `konsole_process_launcher`, `layout_environ`, `layout_mode`, `layout_output`, `mode`, `output_path`, `pane_script_path`, `print_func`, `role_process_runner`, `runner`, `window_title`.
  - Outputs (4): `launch_started_at`, `launch_started_ns`, `resolved_layout_mode`, `worker_start_exit_code`.
  - Returns at 4968 (`return result`), 5005 (`return result`), 5137 (`return launch_result`).
  - `record_worker_start_failure`, `presentation_controller` and
    `use_runtime_presentation` become internal.
- **Why together.** P7 defines the closure `record_worker_start_failure`,
  which P8 calls. It holds shared state that binding analysis does not see:
  - `nonlocal worker_start_exit_code` rebinds the caller's exit state;
  - `failed_roles[role.role] = reason` mutates the caller's dict, which P9
    reads.

  Splitting P7 from P8 would hand a closure across the call boundary. The
  bounded design moves both into one phase. That phase:
  - keeps the recorder as its own nested def;
  - mutates the same `failed_roles` object;
  - hands `worker_start_exit_code` back in a frozen result, with the three
    returns carried as `exit_code`.

  The recorder's shared-state writes must be pinned by a behaviour test,
  because a flow proof cannot see them.
- After P7 with P8 comes P9.

### SYRD-342 (slice 12w): `launch_project` P7+P8, worker start and presentation

Re-measured on `3ef1d42` with `phaseflow2.py`, on a regenerated
exact-tree graph. The design was posted **before** any edit.

**P7+P8 = statements [25..34], lines 4921–5137, 10 statements.**
- **Inputs (18):** `allow_stale_launcher`, `config`, `config_path`, `delegate_role_sessions_to_owner`, `effective_pane_state_dir`, `failed_roles`, `force_reload`, `konsole_process_launcher`, `layout_environ`, `layout_mode`, `layout_output`, `mode`, `output_path`, `pane_script_path`, `print_func`, `role_process_runner`, `runner`, `window_title`. `config` is positional; the other 17 are keyword-only.
- **Continuation outputs (4),** in assignment order: `worker_start_exit_code`,
  `launch_started_at`, `launch_started_ns`, `resolved_layout_mode`. P9 reads
  each: `resolved_layout_mode` only under `attach-or-start`, the clock only
  under `report_session_records`, and the exit code in its final `return`. P9
  also reads `failed_roles`, the same dict the phase writes into.
- **Terminal returns (3):** the detached start's `return result`, the viewer
  role's `return result` and the window's `return launch_result`. Each is
  guarded by `!= 0`. At the first, `resolved_layout_mode` does not exist yet.
- **Why one phase.** P7's nested `record_worker_start_failure` is called in
  P8, holds `nonlocal worker_start_exit_code`, and writes
  `failed_roles[role.role] = reason`. Binding analysis sees neither, so the
  closure stays nested in one phase and is not exported.

**The move.** Appended to `scripts/launch_phases.py` (326 → 597 lines):
- **`WorkerStartup`,** frozen, holding only the four outputs, with no
  defaults and no exit field.
- **`_start_workers_and_present(config, *, <17 inputs>) -> WorkerStartup | int`:**
  - the ten statements unchanged, comments and the recorder included;
  - `from scripts import presentation_controller` still its own local import;
  - the 21 launcher names (46 reads) read as `launcher.X` when the phase runs;
  - `time`, `sys` and `Path` the module's own. There are no rebinds of
    `team_launcher.time`, `.sys` or `.Path`; the Konsole suite's
    `team_launcher.time.sleep` patches mutate the shared module.
- **The terminal returns are verbatim.** They build nothing, so no stop
  evaluates an undefined output. The phase ends with the one added statement,
  `return WorkerStartup(...)`, reached only when all four are definitely
  assigned.
- **In `launch_project`:** the call by the launcher's own name, then
  `if not isinstance(worker_startup, WorkerStartup): return worker_startup`
  before any output is read, then four unpacking lines. A stop is the very
  object the phase returned. The launcher goes from 19,600 to 19,411 lines.
- **Two corrections to the posted design, both made before submission:**
  - The field order. The design said "assignment order" but listed the clock
    first; `worker_start_exit_code = 0` is assigned first. The proof caught it.
  - The seam count: 21 names, 18 functions and 3 constants, not 22.

**The phase proof,** `equiv_phase4.py`, holds:
- **What it normalises:** only the declared launcher names, the added import,
  and the final return.
- **What must match exactly:**
  - every return, verbatim;
  - the nested recorder, byte-equal with no normalisation;
  - the caller's call, with the dispatch before any output read;
  - the rest of `launch_project` and the launcher;
  - P3, P5 and P6 and their classes;
  - every comment.

I planted 14 faults and it caught every one:
- a wrong early exit;
- a terminal return that reads the undefined outputs;
- a stop made a fresh int;
- a missing output;
- a missing input;
- the `nonlocal` changed;
- the dict write changed;
- the first failure overwritten;
- the local controller read through the launcher;
- the call before P6;
- outputs read before the dispatch;
- a changed P6;
- a launcher name read bare.

The scope-aware scan was run on `launch_phases.py` **explicitly**: `scope scan, launch_phases.py explicitly: []`.

**Guards that followed the moved calls,** with the baseline totals. I found
them by an AST sweep of every test constant naming a phase global or quoting
the moved source, not by grep:
- `legacy_presentation_boundary_test`: `legacy_presentation_refusal`, 1;
- `gui_window_launch_boundary_test`: `launch_konsole_window`, 2;
- `presentation_reconnect_boundary_test`: `hand_presentation_back_to_the_caller`, 2;
- `role_pane_entry_boundary_test`: `run_detached_role`, 2, and
  `ensure_visible_role_session_for_viewer`, 3. These totals are pinned now; it
  used to check only "called by name";
- `tenant_resume_presentation_handoff_test`: its source-text check reads the
  bridged branch in `launch_phases.py`.

`owner_state_dirs` (3) and `desktop_detection` (3) already counted
`launch_phases`, and their totals are unchanged. Each widened guard failed on
the candidate first, and a bypass of each is killed.

**Evidence.**
- **Boundary test (extended):** `tests/launch_phases_boundary_test.py` now has
  209 checks. Recording fakes stand in for every lookup, for the local
  controller and for the clock.
  - **Detached roles:** delegated and direct. A non-runtime failure stops with
    the start's own object, before the layout mode is looked up.
  - **Runtime:** failures are recorded, keeping the first as an `int`, with
    the exact stderr, into the caller's own dict.
  - **Skips.**
  - **The viewer:** delegated without attaching, and direct; owner
    directories; the tmux session on the role runner; the runtime viewer's
    state path, `unstarted` and registration wait; empty gives 0 and
    all-failed gives 1.
  - **The bridged viewer:** one tab, and a warning only when the hand-back
    fails.
  - **Separate:** runtime, bridged, refused and Konsole, including the
    presentation's and Konsole's own failures.
  - **An error propagates.**
  - **The launch:** P5 and P6 stops never reach the phase; a phase stop never
    reaches P9; P9 gets the four values and the same dict.
- **Mutations:** 53 of 53 mutations of P7+P8 and their wiring, and 7 of
  7 guard bypasses, are killed, serially and on the clean tree. Each kill's
  own line names its property (rule 23). A first run left three survivors,
  and fixing them strengthened the suite:
  - a tolerant read before the dispatch, now caught by a structural check;
  - a `getattr` rule-27 form, replaced by the attribute form the scan targets;
  - a viewer owner-directories runner the viewer case did not check.

  Four kills were not property-specific, and were made so.
- **Green on both trees, with the same output:** `desktop_detection_boundary_test`, `desktop_presentation_boundary_test`, `gui_window_launch_boundary_test`, `legacy_presentation_boundary_test`, `live_role_runtime_boundary_test`, `owner_state_dirs_boundary_test`, `pane_launcher_preflight_boundary_test`, `presentation_layout_files_boundary_test`, `presentation_reconnect_boundary_test`, `project_desktop_boundary_test`, `project_worktrees_boundary_test`, `role_pane_entry_boundary_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`.
- **Run case by case on both trees,** with exclusions by name:
  - legacy_presentation_launch_test.py syrd 13/0 pristine 13/0 excluded=2 cases=15 IDENTICAL
  - tenant_resume_presentation_handoff_test.py syrd 27/0 pristine 27/0 excluded=2 cases=29 IDENTICAL
  - legacy_presentation_migration_test.py syrd 29/0 pristine 29/0 excluded=2 cases=31 IDENTICAL

  The exclusions:
  - `legacy_presentation_launch_test`: the two real-Konsole cases (SYRD-338).
  - `tenant_resume_presentation_handoff_test`: the two cases whose
    `publish_handoff` writes into the real home (SYRD-343).
  - `legacy_presentation_migration_test`: its two user-namespace cases, which
    mount tmpfs over `/etc/sudoers.d` inside the namespace.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | presentation window recovery | 19 | 2 | |
  | env config | 24 | 2 | |
  | control repo | 7 | 3 | |
  | konsole | 14 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | desktop access | 3 | 1 | |
  | switchyard commands | 10 | 2 | |
  | presentation | 11 | 5 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (+2, the two import names added above those lines) |

  **Existing cases that really run P7+P8,** traced on the candidate: passing
  on both, `test_control_repository_launch_uses_role_worktrees_and_preserves_repository` (2x); `test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default` (1x); `test_all_detached_undetected_viewer_launch_still_reports_detached_start_failure` (1x); `test_clean_launch_does_not_warn_about_shared_checkout_refresh` (1x); `test_cross_owner_viewer_uses_owner_tmux_for_fresh_and_existing_sessions` (2x); `test_default_layout_does_not_create_untracked_switchyard_in_shared_checkout` (2x); `test_full_launch_delegates_detached_role_start_to_owner_pane_subcommand` (1x); `test_launch_warns_before_resetting_dirty_shared_checkout_with_real_git` (1x); `test_project_scoped_viewer_ignores_legacy_global_viewer_session` (1x); `test_project_scoped_viewer_sessions_allow_two_projects_and_same_project_replacement` (3x); `test_root_materialized_owner_state_layout_is_owner_writable_on_next_launch` (2x); `test_undetected_viewer_launch_reports_failure_when_every_visible_role_failed` (1x); `test_viewer_layout_starts_role_sessions_and_additive_viewer` (1x); `test_viewer_window_title_uses_project_name_and_survives_attach_cycle_options` (1x); `test_plain_shared_checkout_git_runs_as_owner_when_launcher_user_differs` (1x); `test_plain_shared_checkout_git_stays_direct_when_already_owner` (1x); `test_full_launch_keeps_starting_workers_and_builds_recovery_slots_after_failures` (2x); red on
  both, `test_control_repository_launch_runs_bootstrap_as_configured_owner`, `test_launch_without_control_repository_does_not_owner_wrap_pgu_plan`, `test_reload_fetches_without_refreshing_shared_checkout`, `test_reload_syncs_live_cli_and_model_from_process_argv_before_relaunch`, `test_start_does_not_sync_live_cli_or_model`, `test_start_runs_research_detached_before_opening_visible_layout`.

- **Zero-case suites.** `tenant_control_bridge_e2e_test` and `team_launcher_desktop_policy_owner_test` have no `test_` functions; each one's whole output is identical on both trees (refused at namespace setup before any case), and neither is claimed as coverage. `team_launcher_presentation_titles_test` prints ok on both, identically, but its real-D-Bus case finds the refusing stubs and returns early, so it is not claimed either. `first_run_login_inheritance_test` passes identically (82 checks), but it writes its fixture layout into the real home (disclosed), so it was run only in the whole comparison and not claimed as clean.
- **Stubs and screening:** Every suite ran behind refusing `konsole`, `dbus-daemon`, `dbus-send`, `sudo`, `systemctl`, `loginctl`, `tmux` and provider-CLI stubs. Each was screened first for pinned child PATH, GUI, socket, tmux and — after the first incident — passwd-derived homes. The live-home snapshot was diffed around the whole comparison, the red per-case comparison and the case trace; the last two changed nothing.
- **Not run:**
  - `runtime_registration_wait_test` (SYRD-336);
  - `display_recovery_live_tmux_test`, which drives a real (isolated) tmux
    server, outside this ticket's boundary.
- **A live write, disclosed (SYRD-342 → SYRD-343).**
  - **What happened:** before screening it, I ran
    `tenant_resume_presentation_handoff_test` whole, twice. Two of its cases
    call the bridge's real `publish_handoff`, which resolves the caller's home
    through `pwd`, not `$HOME`. They wrote
    `~/.local/state/switchyard/projects/syrd/syrd-presentation-handoff.json`
    (a test viewer handoff) into this account's live project state at
    02:05:15.
  - **Resolution:** it was disclosed on the ticket at once, and I did not
    touch it. The Director preserved its bytes, removed only that unchanged
    artifact, and recorded the harness repair as SYRD-343. The file's earlier
    content is unknown.
  - **Since then:** every suite is screened for passwd-derived homes too, and
    a snapshot of `~/.local`, `~/.config`, `~/.cache` and top-level home files
    is diffed around every run. That diff found more, also disclosed on the
    ticket:
    - `first_run_login_inheritance_test` writes its fixture layout to
      `~/.local/state/switchyard/projects/testing/testing-team-layout.json`
      on every run. This goes back to earlier slices.
    - Five more `tenant_resume_presentation_handoff_test` cases reach the real
      home. One rewrote `~/.local/state/pgu-ticket-board/pane-sessions/ops.provider-state.json`
      (fixture records), and one published and then consumed a handoff in
      the live `syrd` state directory, leaving nothing.

    Neither suite was run again. The red per-case comparison and the case
    trace left the snapshot unchanged, apart from the running
    notify-listener's own log.
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with every
  phase the launcher's object, and `start --help` exits 0.

**Next: P9, the launch's report and records,** measured on this candidate:
- **Span:** statements [31..36], lines 4952–4989, 6 statements.
- **Inputs (16):** `config`, `config_path`, `effective_pane_state_dir`, `failed_roles`, `launch_started_at`, `launch_started_ns`, `mode`, `print_func`, `reconcile_home`, `report_session_records`, `resolved_layout_mode`, `running_roles`, `session_record_poll`, `session_record_timeout`, `unreconciled_roles`, `worker_start_exit_code`.
- **Outputs and returns:** no outputs. Its one return, `return worker_start_exit_code`,
  is `launch_project`'s last statement, so the phase can answer the launch's code
  and the caller return it.
- **Launcher globals:** `LAYOUT_MODE_VIEWER`, `_role_cli_name`, `len`, `provider_state_generation`, `record_provider_state_generation`, `report_launch_session_records`, `unsafe_presentation_report`, `unsafe_root_presentation_windows`.
- **What it reads:** `failed_roles` (the dict P7+P8 wrote) and
  `unreconciled_roles`, both read-only, so no shared-state hand-off remains.
- **After P9,** `launch_project` is its preflight and the phase calls.

### SYRD-344 (slice 12x): `launch_project` P9, the launch's report and records

Re-measured on `f021930` with `phaseflow2.py`, on a regenerated exact-tree
graph. The design was posted **before** any edit.

**P9 = statements [31..36], lines 4952–4989, 6 statements.** These are
`launch_project`'s last six.
- **Inputs (16):** `config`, `config_path`, `effective_pane_state_dir`, `failed_roles`, `launch_started_at`, `launch_started_ns`, `mode`, `print_func`, `reconcile_home`, `report_session_records`, `resolved_layout_mode`, `running_roles`, `session_record_poll`, `session_record_timeout`, `unreconciled_roles`, `worker_start_exit_code`. `config` is positional; the other 15 are keyword-only.
- **Outputs:** none. The phase ends in `return worker_start_exit_code`.
- **Read-only:** `failed_roles`, the dict P7+P8 wrote into, and
  `unreconciled_roles` are only read.

**The move.** Appended to `scripts/launch_phases.py` (597 → 665 lines):
- **`_report_launch(config, *, <15 inputs>) -> int`:** the six statements
  unchanged, comments included, after a call-time launcher import. The
  original `return worker_start_exit_code` is its last statement, so the
  code comes back as the very object.
- **No result class and no added statement.**
- **Launcher names:** 7 (one read each), as `launcher.X`:
  `unsafe_root_presentation_windows`, `unsafe_presentation_report`,
  `LAYOUT_MODE_VIEWER`, `_role_cli_name`, `provider_state_generation`,
  `record_provider_state_generation`, `report_launch_session_records`. My
  posted design said eight reads; it is seven.
- **`len` stays the builtin:** the full-form scan finds no rebind of it.
- **In `launch_project`:** the six statements became
  `return _report_launch(config, …)`, by the launcher's own name, at the old
  suffix. `launch_project` is now 162 lines of preflight and phase calls, and the
  launcher goes from 19,411 to 19,392 lines.

**The phase proof,** `equiv_phase5.py` (a tail phase: the helper body equals
the six statements exactly, its final return included; the caller's last
statement is exactly the same-named call's return; the rest of the launcher
and the module are unchanged), holds. I planted 16 faults and it caught
every one:
- a changed role filter;
- the failed skip lost;
- the unreconciled skip lost;
- the viewer, unsafe and mode branches changed;
- the other clock;
- a different directory;
- a converted code;
- a statement after the return;
- a missing input;
- the answer discarded;
- a wrong position;
- a changed P7+P8;
- `len` through the launcher;
- a launcher name read bare.

The scope-aware scan was run on `launch_phases.py` **explicitly**: `scope scan, launch_phases.py explicitly: []`.

**Guards that followed the moved calls,** with the baseline totals. Each
failed on the candidate first, and a bypass of each is killed:
- `presentation_windows_boundary_test`: `launch_phases.py` joined
  `MOVED_CALLERS` (`unsafe_root_presentation_windows` 5,
  `unsafe_presentation_report` 4);
- `session_records_boundary_test`: `report_launch_session_records`, 2.

**Evidence.**
- **Boundary test (extended):** `tests/launch_phases_boundary_test.py` now has
  241 checks. The cases are:
  - the unsafe-window report first, with no announcement;
  - the announcement's filter, order, singular and plural, and its silence
    for no roles, only detached or failed roles, a viewer, and a plain attach;
  - provider-state records in config order against the reconcile home,
    skipping failed, unreconciled and CLI-less roles, and none without a
    home or on a plain attach;
  - the session report's exact arguments per mode, and none when not asked
    for;
  - an error propagating;
  - the code coming back as the same object;
  - `launch_project` returning P9's answer with P7+P8's values and the same
    failures dict, and a P7+P8 stop never reaching P9.
- **Mutations:** 33 of 33 mutations of P9 and its wiring, and 2 of 2
  guard bypasses, are killed, serially and on the clean tree, each by its
  property (rule 23). Three first-run kills were made property-specific: one
  malformed mutant, one caught only structurally, and one killed through a
  neighbouring case.
- **Green on both trees, with the same output:** `desktop_detection_boundary_test`, `desktop_presentation_boundary_test`, `gui_window_launch_boundary_test`, `legacy_presentation_boundary_test`, `live_role_runtime_boundary_test`, `owner_state_dirs_boundary_test`, `pane_launcher_preflight_boundary_test`, `presentation_layout_files_boundary_test`, `presentation_reconnect_boundary_test`, `presentation_window_replacement_boundary_test`, `presentation_windows_boundary_test`, `project_desktop_boundary_test`, `project_worktrees_boundary_test`, `provider_runtime_state_boundary_test`, `role_pane_entry_boundary_test`, `session_records_boundary_test`, `team_launcher_stop_reload_test`, `team_launcher_tmux_panes_test`.
- **Case by case on both trees:** `legacy_presentation_launch_test` 13 of 15, 2 excluded by name; `legacy_presentation_migration_test` 29 of 31, 2 excluded by name.
- **Red on both trees, identical per case:**

  | Suite | Pass | Fail | Note |
  |---|---:|---:|---|
  | presentation window recovery | 19 | 2 | |
  | env config | 24 | 2 | |
  | control repo | 7 | 3 | |
  | konsole | 14 | 2 | |
  | unprivileged presentation | 7 | 4 | |
  | viewer | 21 | 5 | |
  | reload attach | 12 | 1 | |
  | resume detached | 8 | 1 | |
  | desktop access | 3 | 1 | |
  | switchyard commands | 10 | 2 | |
  | presentation | 11 | 5 | |
  | project artifacts | 10 | 6 | |
  | tmux invocation lint | — | — | identical output |
  | git ownership lint | — | — | identical output, apart from launcher line numbers (+1, the one import name added above those lines) |

  **Existing cases that really run P9,** traced on the candidate: passing
  on both, `test_control_repository_launch_uses_role_worktrees_and_preserves_repository` (2x); `test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default` (1x); `test_clean_launch_does_not_warn_about_shared_checkout_refresh` (1x); `test_cross_owner_viewer_uses_owner_tmux_for_fresh_and_existing_sessions` (2x); `test_default_layout_does_not_create_untracked_switchyard_in_shared_checkout` (2x); `test_full_launch_delegates_detached_role_start_to_owner_pane_subcommand` (1x); `test_launch_warns_before_resetting_dirty_shared_checkout_with_real_git` (1x); `test_project_scoped_viewer_ignores_legacy_global_viewer_session` (1x); `test_project_scoped_viewer_sessions_allow_two_projects_and_same_project_replacement` (3x); `test_root_materialized_owner_state_layout_is_owner_writable_on_next_launch` (2x); `test_viewer_layout_starts_role_sessions_and_additive_viewer` (1x); `test_viewer_window_title_uses_project_name_and_survives_attach_cycle_options` (1x); `test_plain_shared_checkout_git_runs_as_owner_when_launcher_user_differs` (1x); `test_plain_shared_checkout_git_stays_direct_when_already_owner` (1x); `test_full_launch_keeps_starting_workers_and_builds_recovery_slots_after_failures` (2x); red on both,
  `test_control_repository_launch_runs_bootstrap_as_configured_owner`, `test_launch_without_control_repository_does_not_owner_wrap_pgu_plan`, `test_reload_fetches_without_refreshing_shared_checkout`, `test_reload_syncs_live_cli_and_model_from_process_argv_before_relaunch`, `test_start_does_not_sync_live_cli_or_model`, `test_start_runs_research_detached_before_opening_visible_layout`.
- **Containment:**
  - Every suite ran behind refusing GUI, sudo, service, tmux and provider
    stubs.
  - Each was screened first for pinned child PATH, passwd-derived homes,
    sockets and GUI.
  - A snapshot of `~/.local`, `~/.config`, `~/.cache` and top-level home
    files was diffed around every run. **No live change,** apart from the
    running notify-listener's own log.
- **Not run:**
  - `tenant_resume_presentation_handoff_test` and
    `first_run_login_inheritance_test`, in any form (they escape `$HOME`;
    SYRD-343);
  - `runtime_registration_wait_test` (SYRD-336);
  - the two real-Konsole cases (SYRD-338);
  - `display_recovery_live_tmux_test`;
  - `team_launcher_new_project_test`, whose `switchyard new` flows are
    outside P9.
- **CLI:** `switchyard --help` output and exit codes are identical for all 36
  invocations, and so is `scripts/team-launcher --help`.
- **Staged release:** it loads the module from the release root, with every
  phase the launcher's object, and `start --help` exits 0.

**`launch_project` is done.** Its nine phases now live in `launch_phases.py`,
and what remains is its preflight and the phase calls. The largest remaining
launcher domains, by exclusive closure on this candidate:
- **Upgrade** (`upgrade_project_command`, 876 lines, with
  `finish_upgrade_command`): 101 definitions, 4,338 lines.
- **`switchyard new`** (`switchyard_new_command` and `new_project_command`):
  87 definitions, 2,347 lines.
- **CLI dispatch:** `switchyard_main`, 481 lines, and `main`, 220 lines.
- **GitHub identity:** 7 definitions, 569 lines.

**Next: 12y, GitHub identity.** It is the one that is small and
self-contained. The definitions are `github_identity_status`,
`set_owner_github_identity_command`, `clear_owner_github_identity_command`,
`write_plan_no_follow`, `selected_key_problems`, `_plan_with_selection` and
`GITHUB_IDENTITY_TIMEOUT_SECONDS`. They are called in only from
`upgrade_project_command` and `switchyard_main`, and read 11 launcher names.
After that, upgrade and `new` should each be split into their own bounded
phases, as `launch_project` was.

### SYRD-345 (slice 12y): GitHub identity

Re-measured on `0bc2761`, on a regenerated exact-tree graph. The design was
posted **before** any edit.

**What moved.** `closure2.py` measures an exclusive set of 7 definitions and 569
lines. They moved to `scripts/github_identity.py` (629 lines) in baseline
order, with comments, docstrings, signatures and defaults:
- `GITHUB_IDENTITY_TIMEOUT_SECONDS`;
- `github_identity_status`;
- `write_plan_no_follow`;
- `selected_key_problems`;
- `_plan_with_selection`;
- `clear_owner_github_identity_command`;
- `set_owner_github_identity_command`.

The canonical types stay in the launcher: `GithubIdentityStatus`,
`TrustedOwnerIdentity` and `PlanDocument`, the no-follow plan reader and the
remedy. The launcher re-exports all seven names, and goes from 19,392 to 18,818
lines. `upgrade_project_command` and `switchyard_main` call the same names
as before, and `scripts/pane_rebind.py` still reads
`launcher.write_plan_no_follow`.

**Seams,** from a full-form rebinding scan over `scripts/` and `tests/`:
- **Read as `launcher.X` when the code runs (9 names):** `uid_for_user`,
  `_walk_no_follow`, `_owner_command_env_args`, `GithubIdentityStatus`,
  `switchyard_privileged_provision_root`, `privileged_baseline_plan_path`,
  `read_plan_no_follow`, `trusted_owner_identity`, `github_identity_remedy`.
- **Read through the launcher by the repairs:** `github_identity_status` and
  `write_plan_no_follow`, because
  `owner_identity_preserved_across_upgrades_test` and
  `local_publication_identity_test` patch them there.
- **Direct:** the other helpers and the timeout, which nothing rebinds.
- **Standard-library modules:** `os`, `stat`, `errno`, `json`, `shlex` and
  `subprocess` are the module's own imports, the very objects the launcher
  holds, so the suites' `team_launcher.os.geteuid` patches still apply.
- **Defaults:** `runner=subprocess.run` and `print_func=print` are the same
  objects.
- **Scope:** the `project_provision` and `publication_boundary` imports stay
  local; the nested `_read_no_follow` and `_check` closures keep
  `expected_uid`, `problems` and `owner_user`.

**Proof.**
- **`equiv2.py`** (AST identity after reading `launcher.X` as `X`; the rest
  of the launcher; comments) holds.
- **`supp345.py`** covers what `equiv2`'s generic normalisation cannot see,
  and holds. It checks:
  - baseline order;
  - `launcher.X` read for exactly the 11 approved names;
  - function-level imports verbatim;
  - nested closures unchanged;
  - no approved name read bare at any site;
  - every moved name re-exported.
- **Planted faults:** I planted 11 faults and together they caught
  every one. Before the supplement gained its last two checks, a seam
  bypassed at one of four sites and a dropped re-export got through. The
  scope-aware scan was run on `github_identity.py` **explicitly**: `scope scan, github_identity.py explicitly: []`.

**Evidence.**
- **New boundary test:** `tests/github_identity_boundary_test.py`, 131
  checks, with owned fakes only. Files live in temporary directories the test
  creates; the no-follow walk, owner uid and environment, owner identity, plan
  authorities, pinned remote and runner are recording fakes. It covers:
  - `github_identity_status`:
    - the private half never opened;
    - every descriptor closed on every path;
    - modes, ownership, non-regular files, symlinks, missing files, bytes
      that are not text, and the managed block;
    - the fingerprint from stdin;
    - the bounded, non-interactive probe in the owner's environment;
    - the greeting, not the exit status, deciding;
    - timeouts and OS errors.
  - The plan writer: its mode, owner and symlink replacement, stale staged
    files, cleanup and descriptors.
  - Key selection: lstat only, and nothing opened or created.
  - The field-preserving selection and its no-op.
  - The set and clear repairs: every refusal, dry run, non-root run, the
    two-authority order and rollback, the owner-run ssh configuration
    commands, and verification.
- **Mutations:** 44 of 44 are killed, serially and on the
  clean tree, each by its property (rule 23). Among them:
  - the private half read;
  - O_NOFOLLOW dropped;
  - a descriptor leaked;
  - the fingerprint taken by path;
  - the exit status deciding;
  - the probe unbounded or interactive;
  - owner-authority bypasses;
  - a local tenant treated as GitHub;
  - lost rollbacks and no-ops;
  - a changed default;
  - a bypassed seam;
  - a hoisted import;
  - a shadowed closure binding.
- **Green on both trees, with the same output:** the pane-rebind,
  workflow-adoption, desktop-approval, provider-auth-status, provider-session,
  role-account-migration, presentation-windows, session-records and
  launch-phases boundary tests; the tmux lint is identical; the git-ownership
  lint's launcher line numbers shift by +9 (the import block).
- **Existing identity suites, case by case on both trees, identical:**
  - owner_github_identity_test.py syrd 9/0 pristine 9/0 excluded=1 cases=10 IDENTICAL
  - owner_identity_preserved_across_upgrades_test.py syrd 20/0 pristine 20/0 excluded=8 cases=28 IDENTICAL
  - local_publication_identity_test.py syrd 7/0 pristine 7/0 excluded=9 cases=16 IDENTICAL
  - team_launcher_switchyard_commands_test.py syrd 10/2 pristine 10/2 excluded=0 cases=12 IDENTICAL

  Statically, owner_github_identity_test.py runnable 9 of which reach a moved function (static): 6; owner_identity_preserved_across_upgrades_test.py runnable 20 of which reach a moved function (static): 17; local_publication_identity_test.py runnable 7 of which reach a moved function (static): 6. The exclusions, by name:
  - every case that drives the root upgrade (`run_upgrade`/`cutover._upgrade`);
  - the case that runs the real key-generation commands through bash (a real
    ssh-keygen);
  - the case that runs a real `sh` removal.
- **Containment:**
  - Every run was behind refusing sudo, systemctl, loginctl, tmux, konsole,
    dbus, provider, ssh and ssh-keygen stubs.
  - The suites' own helpers redirect root's provision, publish, sudoers,
    tenant-control and install roots to temporary roots.
  - A snapshot of `~/.local`, `~/.config`, `~/.cache` and top-level home
    files was diffed around every run. **No live change,** apart from the
    notify-listener's own log.
- **Not run:**
  - the privileged upgrade suites that only quote `set-owner-identity` in
    remedy text (`legacy_root_owned_provision_upgrade_test`,
    `publication_boundary_upgrade_test`,
    `publication_boundary_upgrade_privileged`);
  - `tenant_resume_presentation_handoff_test`,
    `first_run_login_inheritance_test`, `runtime_registration_wait_test`,
    the real-Konsole cases and `display_recovery_live_tmux_test`.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so
  is `scripts/team-launcher --help`. A staged release loads the module from
  its root with every name the launcher's, and `set-owner-identity --help`
  exits 0.

**Next: upgrade, in phases.** `upgrade_project_command` is 876 lines, 76
statements and 18 returns (exclusive closure: 101 definitions, 4,338 lines).
Measured flow-sensitively on this candidate, it falls into six phases:
- **U1, remote and pinning:** [0..12].
- **U2, manager, desktop and repatriation:** [13..25].
- **U3, cutover and project directory:** [26..42].
- **U4, the dry-run report:** [43], one 152-line statement.
- **U5, identity and accounts:** [44..59].
- **U6, finish:** [60..75].

The bounded next slice is **U4**: lines 13852–14003,
10 inputs (`config`, `config_path`, `deploy_ref`, `deploy_ref_chosen`, `dry_run`, `effective_source_repo`, `print_func`, `publish_remote`, `runner`, `tooling_root`), outputs
`publication_detail`, `trusted_release_root` and 5 early returns. It can move as
`launch_project`'s phases did, with a frozen result or an `int` stop. After
it, the remaining phases can move in turn, and then `switchyard new`
(87 definitions, 2,347 lines).

### SYRD-346 (slice 12z): upgrade U4, role tooling preview and privileged staging

Re-measured on `ce84e93` with `phaseflow2.py`, on a regenerated exact-tree
graph. The design was posted **before** any edit.

**U4 = `upgrade_project_command` statement [43], lines 13852–14003.** It is one
`if dry_run: … elif os.geteuid() == 0: …` of 152 lines, with five `return 1`
refusals.
- **Reads (10):** `config`, `config_path`, `deploy_ref`, `deploy_ref_chosen`, `dry_run`, `effective_source_repo`, `print_func`, `publish_remote`, `runner`, `tooling_root`.
- **Stores that later statements read:** `trusted_release_root` and `publication_detail`.
  - **A correction to the ticket:** `trusted_release_root` *is* read
    downstream, at [59] (14206–14207). It is the verified release handed to
    the identities transaction (SYRD-97).
  - `publication_detail` is read at [57].
  - U4 rewrites each only on some branches: not on a non-root apply, not on a
    dry run without a preview, and the detail not on a clean root apply. So
    neither is an output U4 always defines.

**The move.** A new `scripts/upgrade_phases.py` (222 lines):
- **`UpgradeToolingStaged`,** frozen: `trusted_release_root`, then
  `publication_detail`, with no defaults.
- **`_stage_upgrade_tooling(config, *, …) -> UpgradeToolingStaged | int`:**
  - it takes the 10 reads, plus both values as **carry** parameters: the
    caller's current values;
  - the statement, and its leading comment, are verbatim;
  - the 9 launcher names are read as `launcher.X` when the phase runs; `os`
    is the module's own (0 rebinds; the suites' `team_launcher.os.geteuid`
    patches reach it);
  - it ends with `return UpgradeToolingStaged(...)`. An unchanged branch hands
    back the very object it was given, and no default is invented.
- **In `upgrade_project_command`:**
  - the call, by the launcher's own name, at the old position;
  - `if not isinstance(tooling_staged, UpgradeToolingStaged): return tooling_staged`,
    before either value is read;
  - two unpacking lines.
- **Sizes:** the upgrade command goes from 876 to
  737 lines, and the launcher from 18,818 to 18,683.

**The phase proof,** `equiv_phase6.py`, holds. It is `equiv_phase4` with carry
scaffolding:
- the parameters are the reads plus exactly the outputs;
- no output is definitely assigned by the phase alone;
- each carry is passed same-named;
- a missing baseline module is taken as new.

I planted 12 faults and it caught every one:
- a wrong early return;
- a default in place of a carry;
- crossed fields;
- the stale-release, rollback and hook gates lost;
- a local or `os` launcher-qualified;
- the call moved earlier;
- a read before the dispatch;
- a carry not passed;
- a changed prior statement.

The scope-aware scan was run on `upgrade_phases.py` **explicitly**: `scope scan, upgrade_phases.py explicitly: []`.

**The guard that followed the moved call:**
`publication_boundary_upgrade_test::test_the_upgrade_step_removes_it_and_restarts_no_worker`
read `remove_tenant_publication_boundary` in the upgrade's own text. It now
requires the upgrade to call `_stage_upgrade_tooling` before
`cutover_role_identities_command`, and the phase to contain the removal. It
failed on the candidate first; it passes on both trees, run on its own, since
it is source-only; and removing both removal calls kills it.

**Evidence.**
- **New boundary test:** `tests/upgrade_phases_boundary_test.py`, 61
  checks, with recording fakes for every facility U4 uses and a patched
  effective uid. It covers:
  - the dry run's preview, in order, with preview problems short-circuiting
    the boundary preview, and the root taken only from a previewed release;
  - a non-root apply doing nothing and handing back both carries as the same
    objects;
  - the root apply's order (migration, release, staleness, rollback note,
    staging from the release, hooks, boundary), and each of the six refusal
    paths with its journal record, text and code;
  - an incomplete removal as a warning and the detail;
  - an exception propagating;
  - the upgrade's own call, dispatch and unpacking, compiled from the
    launcher's AST and run against a stand-in U4.
- **Mutations:** 32 of 32 are killed, serially, each by its
  property (rule 23).
- **Suites:** the github-identity, launch-phases, pane-hooks and
  role-account-migration boundary tests are green and identical on both
  trees. The tmux lint is identical; the git-ownership lint's line numbers
  shift by +4 (the import block).
- **Containment:** behind refusing stubs (sudo, systemctl, loginctl, tmux,
  konsole, dbus, provider, ssh, ssh-keygen), with a snapshot of `~/.local`,
  `~/.config`, `~/.cache` and top-level home files diffed around every run.
  **No live change,** apart from the notify-listener's own log.
- **Not run:**
  - the suites that drive the upgrade itself, in its root or dry-run forms
    (among them `claude_permission_hook_test`, `host_boundary_bootstrap_test`,
    `finish_upgrade_dry_run_parity_test`, `legacy_workflow_migration_test`,
    and the rest of `publication_boundary_upgrade_test`), per this ticket's
    containment;
  - SYRD-343's suites, SYRD-336, SYRD-338's cases and
    `display_recovery_live_tmux_test`.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so
  is `scripts/team-launcher --help`. A staged release loads both modules from
  its root.

**Next: U5, identity and accounts,** statements [47..62], lines
13873–14113, 16 statements.
- **Inputs (15):** `commit_git_dir`, `config`, `config_path`, `cutover`, `deploy_ref`, `desktop_choice`, `dry_run`, `effective_source_repo`, `print_func`, `publication_detail`, `publish_remote`, `runner`, `source_repo`, `tooling_root`, `trusted_release_root`. These include U4's
  two carried values.
- **Outputs:** `config` and `release_report_config`.
- **Returns:** 2.
- **Ahead of that slice:** the reaching definitions of `config`, reassigned
  across phases, must be checked the same way.

### SYRD-347 (slice 12aa): upgrade U5, identity and accounts

Re-measured on `788d2c0` with `phaseflow2.py`, on a regenerated exact-tree
graph. The design was posted **before** any edit.

**U5 = `upgrade_project_command` statements [47..62], lines 13873–14113, 16 statements.**
- **Reads (15):** `commit_git_dir`, `config`, `config_path`, `cutover`, `deploy_ref`, `desktop_choice`, `dry_run`, `effective_source_repo`, `print_func`, `publication_detail`, `publish_remote`, `runner`, `source_repo`, `tooling_root`, `trusted_release_root`.
- **Outputs:** `config` and `release_report_config`. Both are reassigned only
  after a real (non-dry) identities transaction.
  - `config` is also read, so it carries itself.
  - `release_report_config` is initialised at [26], before U4, and can differ
    from `config` by now, so its incoming value is carried rather than
    replaced.
- **`cutover`:** reassigned in U5 but never read afterwards (U6 recomputes
  `final_cutover`), so it stays U5's own.
- **Returns:** `1` when root cannot vouch for the migration, and the
  transaction's own code when it fails. The baseline reloads the config,
  synchronises the report config and recalculates the cutover before that
  check, and that order is kept.

**The move.** Appended to `scripts/upgrade_phases.py` (222 → 516 lines):
- **`UpgradeIdentitiesDone`,** frozen: `config`, then `release_report_config`.
- **`_upgrade_identities_and_accounts(config, *, <14 reads + release_report_config>)`:**
  - the 16 statements verbatim, with their leading comment;
  - 19 launcher names (23 reads) as `launcher.X` at call time;
  - `os` and `subprocess` the module's own; `getattr` and `str` builtins;
  - the `project_provision` and `publication_boundary` imports stay the
    function's own.
- **U4 is untouched.** The `TYPE_CHECKING` block gained `RoleAccountCutover`.
- **In the caller:** the call at the old position, the type dispatch before
  either value is read, then two unpackings. The upgrade command goes from
  737 to 514 lines, and the launcher from
  18,683 to 18,462.

**The phase proof,** `equiv_phase6.py`, holds. For this slice it was
generalised: the parameters are the reads *union* the outputs, and a
`TYPE_CHECKING` block may gain import lines. I planted 12 faults and it
caught every one:
- the config carry lost;
- a report-config default;
- the report carry not passed;
- a converted terminal object;
- the failure checked before the reload;
- the trusted-root precedence swapped;
- a local import hoisted or qualified;
- the migration refusal answering success;
- the call before U4;
- a read before the dispatch;
- a changed U4.

The scope-aware scan was run on `upgrade_phases.py` **explicitly**: `scope scan, upgrade_phases.py explicitly: []`.

**Guards that followed the moved calls,** at original strength and totals:
- `role_identity_cutover_boundary_test`: `cutover_role_identities_command`,
  1 site.
- `publication_boundary_upgrade_test::test_the_upgrade_step_removes_it_and_restarts_no_worker`,
  source-only: U4's call, which removes the boundary, must precede U5's
  call, which holds the transaction.
- `github_identity_boundary_test`: the upgrade's one `github_identity_status`
  call.
  - **A disclosure:** my pre-edit sweep listed this suite's hits, and I
    dismissed them as my own tests' fakes without reading the check. The
    suite comparison caught it.
  - It is widened like the others, and a bypass of it is killed. I then ran
    every other suite the sweep named, and all are green and identical.

**Evidence.**
- **Boundary test (extended):** `tests/upgrade_phases_boundary_test.py`,
  129 checks. There are owned recording fakes for every launcher lookup
  and for the five local imports, patched where they live, plus a patched
  effective uid. It covers:
  - identity: a local tenant with and without a stale plan, unknown,
    unresolved, dry run, root and non-root, and no configured owner;
  - the identity script's exact command, and its failure being non-fatal;
  - the read-back against the selected key;
  - pending identities before the artifacts verdict, which U4's detail makes
    incomplete;
  - missing accounts: a dry-run trusted path, root publishing from the
    recorded source, the refusal with its journal record, and the non-root
    instruction;
  - ready accounts: the transaction's exact arguments, the three
    trusted-root and source variants, and the reload and synchronisation
    order;
  - failure: the same code object, reloaded first and reported from the new
    config, recalculated cutover and trusted journal;
  - a complete cutover;
  - an error propagating;
  - the upgrade's own call, dispatch and unpacking, compiled from the
    launcher.
- **Mutations:** 35 of 35 are killed, serially, each by its
  property (rule 23).
- **Suites:** green and identical on both trees:
  - `role_identity_cutover`, `role_account_migration`, `github_identity`,
    `launch_phases`, `desktop_detection`, `desktop_policy`, `display_bridge`,
    `legacy_presentation`, `owner_state_dirs`, `pane_hooks`,
    `project_desktop`, `project_onboarding` and `worker_pool_command`;
  - the tmux lint;
  - the git-ownership lint, apart from a +2 line shift.
- **Containment:** behind refusing stubs, with a live snapshot around every
  run. **No live change,** apart from the notify-listener's own log.
- **Not run:**
  - every suite that drives the upgrade itself;
  - SYRD-343's suites, SYRD-336, SYRD-338 and
    `display_recovery_live_tmux_test`.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so
  is `scripts/team-launcher --help`. A staged release loads the module.

**Next: U6, finish,** the rest of the upgrade: lines
13896–14006, 16 statements.
- **Inputs (10):** `commit_git_dir`, `config`, `config_path`, `deploy_ref`, `desktop_choice`, `dry_run`, `effective_source_repo`, `print_func`, `release_report_config`, `runner`.
- **Outputs:** none. It returns `1`, and the upgrade's final `0`.
- **The shape:** a tail phase like `launch_project`'s P9. The upgrade would
  end in `return <phase>(...)`, leaving `upgrade_project_command` as its
  preamble plus phase calls.

### SYRD-348 (slice 12ab): upgrade U6, finish

Re-measured on `b188525` with `phaseflow2.py`, on a regenerated exact-tree
graph. The design was posted **before** any edit.

**U6 = `upgrade_project_command` statements [51..66], lines 13896–14006, 16 statements,** the function's suffix.
- **Inputs (10):** `commit_git_dir`, `config`, `config_path`, `deploy_ref`, `desktop_choice`, `dry_run`, `effective_source_repo`, `print_func`, `release_report_config`, `runner`.
- **Outputs:** none. It returns `1` when the release is blocked, after every
  report, and otherwise the final `0`, even with an incomplete cutover.
- **The cutover is fresh.** U6 asks for its own `final_cutover`; U5's
  `cutover` never reaches it.
- **Two configs.** The release is reported for the incoming
  `release_report_config`; everything else uses the current `config`.

**The move.** Appended to `scripts/upgrade_phases.py` (516 → 654 lines):
- **`_finish_upgrade(config, *, <9 inputs>) -> int`:**
  - the 16 statements verbatim, with their leading comment, ending in the
    upgrade's own returns;
  - 13 launcher names (14 reads) as `launcher.X` at call time;
  - `os` the module's own.
- **In `upgrade_project_command`:** the statements became
  `return _finish_upgrade(config, …)` at the old suffix. The upgrade command
  goes from 514 to 413 lines, and the
  launcher from 18,462 to 18,362.

**The phase proof,** `equiv_phase5.py` (the tail-phase proof from SYRD-344,
now taking the function as a parameter), holds. I planted 12 faults
and it caught every one:
- a wrong return;
- the release reported for the current config;
- no fresh cutover;
- an untrusted journal;
- a changed branch;
- the unsafe report moved;
- the outstanding report on a dry run;
- a changed U5;
- the caller not returning, or bypassing the launcher's name;
- a wrong input;
- `os` through the launcher.

The scope-aware scan was run on `upgrade_phases.py` **explicitly**: `scope scan, upgrade_phases.py explicitly: []`.

**Guards.** Every sweep hit's check was read this time, not just its name.
- **Affected:** `presentation_windows_boundary_test`. `upgrade_phases.py`
  joined `MOVED_CALLERS`, with the totals unchanged (scan 5, report 4). It
  failed on the candidate first, and a bypass is killed.
- **Read and unaffected, run anyway:** `launch_phases`,
  `presentation_window_replacement`, `role_identity_cutover` and U4/U5's own
  seam counts.

**Evidence.**
- **Boundary test (extended):** `tests/upgrade_phases_boundary_test.py`,
  172 checks, with recording fakes for every lookup and a patched
  effective uid. It covers:
  - the complete, root-run release in exact order: the director phase, a
    fresh cutover, the root, the report for the original report config, the
    status recorded for the current config, the trusted journal, the phase
    and outstanding reports, and unsafe windows;
  - the owner's own root check when not root;
  - root problems withholding the release: journalled blocked, then reports,
    then the blocked line and `1`;
  - a release blocked by its status, said after the unsafe report;
  - an incomplete cutover: withheld, the director told when, and still `0`;
  - every director state and its instruction, deployed or not, with the
    reason's fallback;
  - a dry run, with nothing outstanding;
  - an error propagating;
  - the upgrade's own U5 dispatch and U6 return, compiled from the launcher:
    a U5 stop never reaches U6, and U6's answer is the upgrade's.
- **Mutations:** 27 of 27 are killed, serially, each by its
  property (rule 23).
- **Suites:** green and identical on both trees:
  - `presentation_windows`, `presentation_window_replacement`,
    `launch_phases`, `role_identity_cutover`, `role_account_migration` and
    `github_identity`;
  - the publication source guard, run on its own;
  - the tmux lint;
  - the git-ownership lint, apart from a +1 line shift.
- **Containment:** behind refusing stubs, with a live snapshot around every
  run. **No live change,** apart from the notify-listener's own log.
- **Not run:**
  - the suites that drive the upgrade itself;
  - SYRD-343's suites, SYRD-336, SYRD-338 and
    `display_recovery_live_tmux_test`.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so
  is `scripts/team-launcher --help`. A staged release loads the module.

**What remains of `upgrade_project_command`** (413 lines) is
its preamble before U4. Measured flow-sensitively on this candidate, it is
three coherent responsibilities:
- **U1, desktop decision and source pinning:** [1..12], lines
  13521–13635. It has 10 inputs,
  5 outputs (`commit_git_dir`, `deploy_ref`, `deploy_ref_chosen`, `desktop_choice`, `source_repo`) and
  4 refusals.
- **U2, manager, desktop access, repatriation and a partial cutover:**
  [13..25], lines 13638–13779. It has
  8 inputs, 4 outputs
  (`config`, `cutover`, `effective_source_repo`, `source_repo`) and 5 refusals.
- **U3, generated artifacts and the upstream report:** [29..42], lines
  13784–13853, 14 statements.
  - It has 11 inputs, one output (`config`) and no returns.
  - The three initialisations at [26..28] (`release_report_config`,
    `trusted_release_root`, `publication_detail`) feed U4's carries and stay
    in the caller.

**Next: 12ac, U3.** It is the cleanest: no refusals, and one always-assigned
output. U2 and then U1 follow, each with a frozen result and an `int` stop, as
U4 and U5 did.

### SYRD-349 (slice 12ac): upgrade U3, generated artifacts and the upstream report

Re-measured on `0ac170e` with `phaseflow2.py`, on a regenerated exact-tree
graph. The design was posted **before** any edit.

**U3 = `upgrade_project_command` statements [29..42], lines 13784–13853, 14 statements.**
- **Inputs (11):** `commit_git_dir`, `config`, `config_path`, `dry_run`, `effective_source_repo`, `print_func`, `registry_dir`, `runner`, `source_repo`, `upstream_report_token_file`, `upstream_report_url`.
- **Output:** `config` alone. It is reloaded only when the layout changed and
  this is not a dry run, and is always reassigned by
  `record_upstream_report_link`, so it is definitely assigned.
- **Returns:** none.
- **The initialisations at [26..28] stay in the caller.** They come before
  U3, so `release_report_config` keeps the pre-U3 config, as in the baseline.

**The move.** Appended to `scripts/upgrade_phases.py` (654 → 754 lines):
- **`_refresh_upgrade_artifacts(config, *, <10 inputs>) -> ProjectConfig`:**
  - the 14 statements verbatim, plus `return config`, with no result class;
  - 13 launcher names as `launcher.X` at call time;
  - `isinstance` and `str` stay builtins.
- **In `upgrade_project_command`:** at the old position,
  `config = _refresh_upgrade_artifacts(config, …)`, by the launcher's own
  name. The upgrade command goes from 413 to
  356 lines, and the launcher from 18,362 to 18,306.

**The proof,** `equiv_phase7.py`, holds. It is `equiv_phase6` plus a bare mode
for one always-assigned output:
- the helper ends in `return <output>`;
- the output is definitely assigned by the phase;
- no class is invented;
- the caller is exactly `<output> = helper(...)`.

I planted 11 faults and it caught every one:
- the source distinction dropped;
- a dry-run reload;
- the owner fallback changed;
- the problem order changed;
- the report config becoming U3's config;
- a local or builtin launcher-qualified;
- the config not assigned back;
- a different value returned;
- a wrong input;
- a changed U6.

The scope-aware scan was run on `upgrade_phases.py` **explicitly**: `scope scan, upgrade_phases.py explicitly: []`.

**Guards.** Every sweep hit was read. The hits were export and defaults
tables (`agent_cli`, `launcher_checkout`, `project_onboarding`,
`upstream_report`), other modules' own seam tables, and a behavioural patch
(`desktop_access`). No guard counts U3's call sites, and every named suite
ran green and identical. My own boundary test pinned U4, U5 and U6 at
absolute statement indices. Those moved by 13 when U3 became one statement,
and they now pin [30], [34] and 39 statements.

**Evidence.**
- **Boundary test (extended):** `tests/upgrade_phases_boundary_test.py`,
  208 checks. It covers:
  - the order: warning, layout, runtime, onboarding, board skill, hooks,
    link, credential, then CLIs;
  - the stale warning and runtime artifacts using the effective source, and
    `None` when none was given;
  - the reload only on a real change, with every step told on a dry run;
  - the onboarding commit cache (given wins; the plan's is taken stripped,
    only when non-blank) and the owner fallback;
  - no project directory;
  - the linked config being what goes on;
  - the problems said in order, and not fatal;
  - an error propagating;
  - the caller's own statements compiled: U3's config goes on while the
    report config stays pre-U3, and nothing sits between U3 and U4.
- **Mutations:** 24 of 24 are killed, serially, each by its
  property (rule 23).
  - The first run left two survivors, both gaps in my own fixtures: the given
    and effective sources were the same path, and the dry-run case did not
    check the downstream steps' `dry_run`. Both were closed.
  - Two report-config kills were made property-specific.
- **Suites:** green and identical on both trees, 13 boundary suites:
  - `agent_cli`, `launcher_checkout`, `project_onboarding`, `upstream_report`,
    `desktop_detection`, `desktop_policy`, `display_bridge`;
  - `launch_phases`, `legacy_presentation`, `owner_state_dirs`,
    `project_desktop`, `worker_pool_command` and `upgrade_phases`.
  - `desktop_access_test` is identical case by case (3/1 on both).
  - The publication source guard passes on its own on both trees.
  - The tmux lint is identical; the git-ownership lint differs only by a +1
    line shift.
- **Containment:** behind refusing stubs, with a live snapshot around every
  run. **No live change,** apart from the notify-listener's own log.
- **Not run:**
  - the suites that drive the upgrade itself;
  - SYRD-343's suites, SYRD-336, SYRD-338 and
    `display_recovery_live_tmux_test`.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so
  is `scripts/team-launcher --help`. A staged release loads the module.

**Remaining preamble** (the upgrade command is now 356
lines):
- **U2, manager, desktop access, repatriation and a partial cutover:**
  [13..25], lines 13639–13780, 13
  statements. It has 8 inputs, outputs
  `config`, `cutover`, `effective_source_repo`, `source_repo`, and 5 refusals.
- **U1, desktop decision and source pinning:** [1..12], lines
  13539–13636, 12 statements. It has
  10 inputs, outputs `commit_git_dir`, `deploy_ref`, `deploy_ref_chosen`, `desktop_choice`, `source_repo`, and
  4 refusals.

**Next: 12ad, U2.** Its outputs must be checked for conditional assignment
(carries) as U4's and U5's were. Then U1.

### SYRD-350 (slice 12ad): upgrade U2, manager, desktop and repatriation

Re-measured on `05ea70a` with `phaseflow2.py`, on a regenerated exact-tree
graph. The design was posted **before** any edit.

**U2 = `upgrade_project_command` statements [13..25], lines 13639–13780, 13 statements.**
- **Inputs (8):** `config`, `config_path`, `deploy_ref`, `desktop_policy`, `dry_run`, `print_func`, `runner`, `source_repo`.
- **Outputs,** in store order `source_repo`, `effective_source_repo`, `config`, `cutover`:
  - `effective_source_repo` and `cutover` are definitely assigned;
  - `source_repo` and `config` are replaced only on some paths. Both are
    inputs, so they carry themselves, and the parameters are exactly the
    inputs.
- **Returns:** five `return 1` refusals: the unrepaired manager, the
  presentation not ready, the refused display bridge, interrupted state not
  restored, and repatriation problems. The partial cutover adds none.

**The move.** Appended to `scripts/upgrade_phases.py` (754 → 942 lines):
- **`UpgradeStateReady`,** frozen, holding the four outputs.
- **`_recover_upgrade_state(config, *, <7 inputs>) -> UpgradeStateReady | int`:**
  - the 13 statements verbatim, with their leading comment;
  - 21 launcher names (25 reads) as `launcher.X` at call time, including
    `MANAGER_WEDGED`, which the launcher imports from `board_services`;
  - `replace` is `dataclasses.replace`, now the module's own import; `Path`
    is the module's own; `sorted` and `str` are builtins;
  - `from scripts import desktop_access as _desktop` stays U2's own.
- **In `upgrade_project_command`:** the call at the old position, the type
  dispatch before any value is read, then four unpackings, and still then
  `release_report_config = config`. The upgrade command goes from
  356 to 228 lines, and the launcher from
  18,306 to 18,180.

**The proof,** `equiv_phase7.py`, holds. It was generalised here: the
parameters are the inputs plus only the outputs the phase does not always
assign, and every output must be bound at the final return. It still proves
SYRD-347's U5 and SYRD-349's U3 on their own trees. I planted 13
faults and it caught every one:
- a carry lost, for each of the two carries;
- a terminal changed or converted;
- restore after repatriation;
- a dry-run reload;
- a revert without live roles;
- the local import hoisted or qualified;
- `replace` through the launcher;
- the call before U3's position;
- a read before the dispatch;
- a changed U3.

The scope-aware scan was run on `upgrade_phases.py` **explicitly**: `scope scan, upgrade_phases.py explicitly: []`.

**Guards that followed the moved calls,** each failing on the candidate first
and then passing, and a bypass of each killed:
- `display_bridge_boundary_test`: `ensure_display_bridge`, 1;
- `project_desktop_boundary_test`: `configure_project_desktop`, 2, now
  counting `upgrade_phases` with `launch_phases`;
- `legacy_presentation_boundary_test`: `migrate_legacy_presentation`,
  `legacy_presentation_migration` and `presentation_controller_enabled`, 1
  each, likewise.

My own boundary test's absolute pins moved by −7 (U2's 13 statements became
6): U3 is at [22], U4 at [23], U5 at [27], and there are 32 statements.

**Evidence.**
- **Boundary test (extended):** `tests/upgrade_phases_boundary_test.py`,
  264 checks. It uses a real frozen dataclass config, so `replace` really
  runs, and patches the desktop policy validator where it lives. It covers:
  - a bare upgrade: the source resolved (a `..` path proves the effective
    source is normalised), a fresh cutover, the caller's own config, and no
    source meaning the checkout root;
  - the wedged manager repaired, or stopping with 1;
  - the desktop configured with the effective source's helper, migrated,
    and carried as the configured config on a dry run;
  - dry-run policies planned by hand: headless or a file, validated for the
    tenant, into a replaced config;
  - an unready presentation;
  - the display bridge only when the window crosses accounts, with its root
    check; a refusal;
  - restore before repatriation; repatriation problems; a dry-run or real
    repatriation with its reload;
  - a partial cutover reported, journalled and reverted with live roles
    (reload and a fresh cutover), and waiting without them;
  - an error propagating;
  - the caller's own statements compiled.
- **Mutations:** 36 of 36 are killed, serially, each by its
  property (rule 23).
  - Two first-run survivors were fixture gaps: a source path that resolving
    does not change. That is fixed with a `..` path.
  - Seven kills that came only from seam counts were rewritten to keep the
    call and break the behaviour.
- **Suites:** green and identical on both trees:
  - `desktop_detection`, `desktop_policy`, `display_bridge`, `launch_phases`,
    `legacy_presentation`, `live_role_runtime`, `owner_state_dirs`,
    `project_desktop`, `role_identity_cutover` and `worker_pool_command`;
  - `desktop_access_test`, identical case by case;
  - the publication source guard;
  - the tmux lint;
  - the git-ownership lint, apart from a +2 line shift.
- **Containment:** behind refusing stubs, with a live snapshot around every
  run. **No live change,** apart from the notify-listener's own log.
- **Not run:**
  - the suites that drive the upgrade itself;
  - SYRD-343's suites, SYRD-336, SYRD-338 and
    `display_recovery_live_tmux_test`.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so
  is `scripts/team-launcher --help`. A staged release loads the module.

**Next: 12ae, U1, desktop decision and source pinning,** the last preamble
phase: statements [1..12], lines 13541–13638, 12 statements.
- **Inputs (10):** `commit_git_dir`, `config`, `deploy_ref`, `desktop_policy`, `dry_run`, `print_func`, `publish_remote`, `runner`, `source_repo`, `tooling_root`.
- **Outputs:** `commit_git_dir`, `deploy_ref`, `deploy_ref_chosen`, `desktop_choice`, `source_repo`, all definitely assigned, so a frozen
  result with no carries.
- **Refusals:** 4.
- **After U1,** `upgrade_project_command` is its docstring plus six phase
  calls and the three initialisations U4 needs.

### SYRD-351 (slice 12ae): upgrade U1, desktop decision and source pinning

Re-measured on `7dcb5d4` with `phaseflow2.py`, on a regenerated exact-tree
graph. The design was posted **before** any edit.

**U1 = `upgrade_project_command` statements [1..12], lines 13541–13638, 12 statements.**
- **Inputs (10):** `commit_git_dir`, `config`, `deploy_ref`, `desktop_policy`, `dry_run`, `print_func`, `publish_remote`, `runner`, `source_repo`, `tooling_root`.
- **Outputs,** in store order `desktop_choice`, `deploy_ref_chosen`,
  `source_repo`, `commit_git_dir`, `deploy_ref`. All five are definitely
  assigned on the path that goes on, so there are no carries; the three that
  are also inputs are parameters because they are read first.
- **Returns (4):** `1` for desktop problems (line 13551), `refused`
  verbatim for a stale recovered pin (line 13587, guarded by `is not None`, so
  a refusal of 0 is still a refusal), `1` for remote problems (line 13611), and
  `1` for a source that cannot be kept (line 13638).

**The move.** Appended to `scripts/upgrade_phases.py` (942 → 1094 lines, under the
1,250 soft limit):
- **`UpgradeSourcePinned`,** frozen, holding the five outputs in store order.
- **`_pin_upgrade_source(config, *, <9 inputs>) -> UpgradeSourcePinned | int`:**
  - the 12 statements verbatim, with the SYRD-232 leading comment and every
    inline comment;
  - `pinned_explicitly` and `deploy_ref_chosen` computed from the incoming
    parameters, which are the caller's originals;
  - 6 launcher names (6 reads) as `launcher.X` at call time:
    `upgrade_desktop_policy_decision`, `resolve_pinned_upgrade_source`,
    `_recovered_pin_behind_host`, `record_publication_remote`,
    `record_upgrade_source` and `restore_publication_remote`;
  - `os` is the module's own import, the one module object the suites'
    `team_launcher.os.geteuid` patches reach; `bool` is the builtin.
- **In `upgrade_project_command`:** the docstring stays; then the call, the
  type dispatch before any value is read, five unpackings in field order,
  and U2. The upgrade command goes from 228 to 142 lines (27
  statements), and the launcher from 18,180 to 18,096.

**The proof,** `equiv_phase7.py`, holds. Two changes to the tool:
- `bool` joined its builtins;
- the carry check no longer insists that at least one output needs a carry.
  That was a claim about U4's design, not a safety property. Parameter
  equality still forbids an unneeded carry, and every output must still be
  bound at the final return. The tool still proves SYRD-350's U2 on its own
  tree.

I planted 22 faults and it caught every one:
- the refusal tested for truthiness, or its code replaced;
- explicitness taken from the resolved pin;
- a recovered ref that is not a choice;
- a stale check on an explicit pin;
- a warning on a dry run;
- the remote recorded after the durable pin, or its strip check dropped;
- durability when nothing was given;
- a restore without a change;
- `os`, `bool` or a launcher name read the wrong way;
- a result field lost or reordered;
- a comment dropped;
- a changed U2;
- a caller input changed;
- a read before the dispatch;
- the unpacking reordered;
- the dispatch after U2;
- the caller's docstring changed.

The scope-aware scan was run on `upgrade_phases.py` **explicitly**: `explicit upgrade_phases.py findings: []`.
The patch and rebind sweep, AST and text, found no test that rebinds any of
the six names on the launcher for this path. The one keyword patch,
`legacy_workflow_migration_test:685`, drives `finish_upgrade_command`, the
other caller of `resolve_pinned_upgrade_source`.

**The guard that followed the moved call:**
`desktop_policy_boundary_test`, `upgrade_desktop_policy_decision`, 1.
- It counts the launcher (by its own name) and `upgrade_phases` (through
  `launcher.`), and the total stays 1.
- The baseline guard fails on the candidate first: `AssertionError: the launcher calls upgrade_desktop_policy_decision at its 1 baseline site, by its own name`.
- A bypass, and the call's removal, are each killed.

My own boundary test's absolute pins moved by −5 (U1's 12 statements became
7): U2 is at [8], U3 at [17], U4 at [18], U5 at [22], and there are 27
statements.

**Evidence.**
- **Boundary test (extended):** `tests/upgrade_phases_boundary_test.py`, 264
  → 325 checks. U1's facilities and the effective uid are the test's own
  recording fakes. It covers:
  - a desktop refusal, real and dry-run, with nothing asked after it;
  - a bare upgrade going on with the resolver's own objects;
  - each pinned argument alone being explicit, and an empty ref still given;
  - a recovered pin said, then checked against the resolved pin with the
    caller's tooling root;
  - a stale refusal of 0, 1 and 3 returned as the very object, and None
    going on;
  - the non-root warning only when explicit, real and not root, with the uid
    asked only then;
  - the remote: empty or whitespace skipped, given recorded as given,
    problems refusing before durability;
  - the durable pin of the resolved values; the remote put back only if
    changed, and the restoration said only if non-empty;
  - an error propagating;
  - the caller's own statements compiled: refusals of 0 and 1 returned
    before any read or U2, and U2 given U1's source and ref.
- **Mutations:** 30 of 30 are killed, serially, each by its
  property (rule 23), with a clean restore.
  - Two kills that came only from the `bool` count were rewritten to keep
    one bare call and break the behaviour.
  - The export-drop kill is the import-order assertion; its message embeds
    the probe's `AttributeError`.
- **Suites:** green and identical on both trees:
  - `desktop_detection`, `desktop_policy`, `display_bridge`, `launch_phases`,
    `legacy_presentation`, `live_role_runtime`, `owner_state_dirs`,
    `project_desktop`, `role_identity_cutover`, `worker_pool_command`,
    `github_identity` and `role_account_migration`;
  - `desktop_access_test`, identical case by case;
  - the publication source guard;
  - the tmux lint;
  - the git-ownership lint, apart from a +2 line shift.
- **Containment:** behind refusing stubs, with a live snapshot around the
  run. **No live change.**
- **Not run:**
  - the suites that drive the upgrade itself with a patched root uid
    (`team_launcher_pinned_release_resume_test`,
    `team_launcher_legacy_desktop_policy_upgrade_test` and the like), which
    I read as the ticket's excluded root upgrade-driving suites;
  - SYRD-343's suites, `runtime_registration_wait_test`, the real-Konsole
    cases and `display_recovery_live_tmux_test`.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so
  is `scripts/team-launcher --help`. A staged release loads the module.

**After U1,** `upgrade_project_command` is its docstring, six phase calls
with their dispatches and unpackings, `release_report_config = config`, and
U4's two initialisations. **SYRD-272 is not complete.** The launcher is still
18,096 lines.

**Fresh candidates for the next responsibility,** measured on this candidate.
Each would get its own flow plan; none is proposed as one unbounded
extraction.
- `finish_upgrade_command` (169 lines, 28 statements, 13883–14051): the director's half of the upgrade, next
  to `upgrade_phases`.
- `refresh_generated_project_runtime_artifacts` (276 lines, 45 statements, 3031–3306): called by U3 and twice by the
  role-identity cutover.
- `switchyard_new_command` (584 lines, 73 statements, 10263–10846): provisioning's orchestration, a phase plan
  like the upgrade's, with `new_project_command` (251 lines, 30 statements, 6923–7173) beside it.
- `switchyard_main` (481 lines, 54 statements, 17381–17861) and `main` (220 lines, 17 statements, 17873–18092): the CLI dispatch.
- `switchyard_repair_boundary_command` (185 lines, 33 statements, 9351–9535),
  `switchyard_resume_provision_command` (196 lines, 24 statements, 9579–9774) and
  `add_project_role_command` (164 lines, 23 statements, 14676–14839).

By domain (`domains.py`, top-level definitions with their leading lines):
- release selection, install and upgrade: 3,378 lines, 97 definitions;
- provisioning (new/register/teardown/owner accounts): 3,303 lines, 121 definitions;
- general helpers (unclassified): 2,473 lines, 203 definitions;
- project config and registry: 2,388 lines, 65 definitions;
- privileged boundary, tenant control and repair: 2,291 lines, 68 definitions;
- CLI parsers and dispatch: 1,039 lines, 11 definitions;
- workflow declaration and rebind: 566 lines, 16 definitions;
- desktop, presentation windows and display bridge: 507 lines, 35 definitions;
- and 8 smaller domains.

### SYRD-352 (slice 13a): the director's upgrade completion, `finish_upgrade_command`

Measured on `3328f0e`, on a regenerated exact-tree graph. The design was
posted **before** any edit.

**The function:** lines 13883–14051, 169 lines, 28 statements after
its docstring.
- One caller: `switchyard_main`'s `finish-upgrade` dispatch, by bare name.
- No nested scopes, lambdas, comprehensions or function-level imports, and
  no leading comment block.
- **Free names:**
  - 19 launcher names, 21 reads. 15 are defined in the launcher; 4 are
    imported into it and patched there by suites:
    `install_handed_off_workflow`, `migrate_declarative_director_onboarding`,
    `director_onboarding_state` and `role_account_cutover`.
  - `os` and `subprocess`, stdlib;
  - `Path`, `Any` and `Callable`, annotations only;
  - `ProjectConfig`, type only;
  - `print`, the builtin default.

**The move.** New `scripts/director_upgrade.py`, 204 lines. The
function is unchanged apart from the 21 `launcher.` qualifications and one
call-time `from scripts import team_launcher as launcher`.
- `_finish_upgrade_preview` stays in the launcher, as a seam.
- `os` and `subprocess` are the module's own imports and the same module
  objects: `team_launcher.os.geteuid` patches still reach it, and the
  `runner` default is `subprocess.run` itself. `print` is the builtin.
- `ProjectConfig` is imported only under `TYPE_CHECKING`, and nothing of
  Switchyard's is imported at the top.
- The launcher imports `finish_upgrade_command` from the module directly
  after the `upgrade_phases` import, and is otherwise unchanged. It goes
  from 18,096 to 17,926 lines.
- `upgrade_phases.py` is untouched.

**The proof.**
- `equiv2.py` holds: the moved AST is identical modulo `launcher.`; the
  remaining launcher nodes are the baseline's minus the def plus one import;
  comments are conserved; every baseline top-level name is still bound.
- A supplement (`supp352.py`) holds. It checks:
  - exactly the 19 approved names through the launcher at their counts, none
    bare, and the one call-time import first;
  - the top imports, with `ProjectConfig` only under `TYPE_CHECKING`;
  - `os.geteuid` read off the module's own `os`;
  - the signature's AST, and at runtime the default objects themselves;
  - one re-export, in place, and one object in both import orders;
  - every other script byte-identical.
- I planted 18 faults and the two caught every one, including a seam
  read bare at one of two sites, `os` or `print_func` through the launcher,
  a late or top-level launcher import, a runtime `ProjectConfig` import, the
  re-export dropped or moved, an edited preview seam, an edited dispatch, and
  an edited `upgrade_phases`.

The scope-aware scan was run on `director_upgrade.py` **explicitly**:
`scope scan, director_upgrade.py explicitly: []`. The global scan (54 refactor modules) finds nothing either.

**Reader inventory.** `scan_all`, rooted at the repo, AST and text.
- Suites patch `install_handed_off_workflow`, `migrate_declarative_director_onboarding`, `director_onboarding_state`,
  `record_upgrade_phase`, `record_release_phase_from_status`, `control_role_name` and
  `resolve_pinned_upgrade_source` on the launcher. Every one is read through it, so every
  patch still reaches.
- The one source guard affected: `workflow_adoption_boundary_test` counts
  `install_handed_off_workflow` == 2 bare calls in the launcher.
  - It now counts the launcher's bare calls plus `director_upgrade`'s
    through `launcher.`, and the total stays 2.
  - The baseline guard fails on the candidate first: `AssertionError: finish-upgrade installs a handed-off workflow only by the launcher's own (patchable) name: ['install_handed_off_workflow']`.
  - A bypass, and the call's removal, are each killed.

**Evidence.**
- **New boundary test:** `tests/finish_upgrade_boundary_test.py`, 74 checks.
  It uses owned recording fakes and a patched effective uid, and covers:
  - root refused before anything is read;
  - the pin reported;
  - a named source or ref skipping the staged fallback, while a cache alone
    does not;
  - the staged release root reported, or the no-release message;
  - the control role and account checked before a dry run, with the owner
    exception;
  - the preview's own answer returned as the very object, with no writes;
  - the workflow refusing, reloading, or absent;
  - the observed onboarding state journalled from the fresh config, and a
    failed onboarding answering 1;
  - an incomplete cutover 0, and root problems 1;
  - the release reported for the normalised source (a `..` path), observed,
    divergence said before the blocked check;
  - a blocked release 1, with the reminder only when installed;
  - errors propagating;
  - import identity in both orders;
  - the CLI dispatch reaching the moved command.
- **Mutations:** 34 of 34 are killed, serially, each by its
  property (rule 23), with a clean restore.
  - The re-export-drop kill is the import-order assertion; its message
    embeds the probe's `AttributeError`.
  - A first-run kill of "the pin resolved before root is refused" came from
    an `IndexError`. The case now finds the resolve entry, and the kill is
    the order assertion.
- **Suites:** green and identical on both trees:
  - `workflow_adoption`, `project_onboarding`, `role_identity_cutover`,
    `upgrade_phases`, `desktop_detection`, `desktop_policy`,
    `display_bridge`, `launch_phases`, `legacy_presentation`,
    `live_role_runtime`, `owner_state_dirs`, `project_desktop`,
    `worker_pool_command`, `github_identity` and `role_account_migration`;
  - two behavioural suites, screened first:
    - `finish_upgrade_dry_run_parity_test` and
      `finish_upgrade_pinned_release_test`;
    - they are unprivileged; their tenant fixture points root's provision
      root at its temporary directory before use; the shared helpers
      redirect the install, publish, sudoers and tenant-control roots at
      import; board openers and runners are fakes;
  - `desktop_access_test`, identical case by case;
  - the publication source guard;
  - the tmux lint;
  - the git-ownership lint, apart from a +1 line shift.
- **Containment:** behind refusing stubs, with live snapshots around the runs.
  **No live change.**
- **Not run:**
  - `release_phase_journal_test`, which patches the uid to 0;
  - `legacy_workflow_migration_test`, a migration suite;
  - the root upgrade-driving suites, SYRD-343's suites,
    `runtime_registration_wait_test`, the real-Konsole cases and
    `display_recovery_live_tmux_test`.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so
  is `scripts/team-launcher --help`. A staged release loads the module.

**Next, for a Director decision: the generated runtime artifact refresh.**
`refresh_generated_project_runtime_artifacts`: lines 3032–3307, 276 lines, 45 statements.
- **Callers:** `scripts/role_identity_cutover.py:802`, `scripts/role_identity_cutover.py:918`, `scripts/upgrade_phases.py:703`. All are outside the launcher, and all
  read it through `launcher.` or `team_launcher.`.
- **Readers:**
  - patch sites: `tests/upgrade_phases_boundary_test.py:1100`;
  - source guards naming it: `tests/upgrade_phases_boundary_test.py`.
- **Free names:**
  - 23 launcher definitions (39 reads);
  - 8 names the launcher imports:
    `Any`, `Callable`, `Path`, `ProjectBoardProvision`, `os`, `privileged_provision_dir`, `replace`, `subprocess`;
  - builtins;
  - the nested `_for_current_identities` and an `except` binding.
- **Its closure** (the definitions only it reaches): 18 defs,
  805 lines. It is the privileged baseline and plan domain:
  `_privileged_baseline_plan`, `authoritative_refresh_plan`,
  `reconstruct_privileged_baseline`, `legacy_owner_from_host_records`,
  `repair_legacy_provision_ownership`, and so on. None of these has a caller
  outside the closure, in the launcher or in any other `scripts/` module
  (tests not counted).
- **The closure reads 21 launcher names** it would keep reading
  through the launcher: `LauncherUpgradeResult`, `ProjectConfig`, `_new_project_worktree_base`, `_project_board_provision_from_json`, `_regenerated_field_divergence`, `_repo_root`, `_validated_role_names`, `_walk_no_follow`, `ensure_privileged_provision_dir`, `expected_privileged_uid`, `install_privileged_artifacts`, `plan_with_tenant_checkout`, `plan_workflow_from_root`, `privileged_artifact_mode`, `privileged_baseline_plan_path`, `publish_tenant_artifact`, `read_tenant_document_no_follow`, `render_privileged_artifacts`, `role_account_name`, `switchyard_privileged_provision_root`, `switchyard_registry_dir`.
- **A bounded plan,** each step its own slice with its own flow and reader
  evidence:
  - move the function alone, if the privileged-baseline helpers stay in the
    launcher as seams;
  - or move the closure as one module (about 805 lines),
    which is one coherent responsibility.

**SYRD-272 is not complete.** The launcher is still 17,926 lines.

By domain (`domains.py`):
- provisioning (new/register/teardown/owner accounts): 3,303 lines, 121 definitions;
- release selection, install and upgrade: 3,207 lines, 96 definitions;
- general helpers (unclassified): 2,474 lines, 204 definitions;
- project config and registry: 2,388 lines, 65 definitions;
- privileged boundary, tenant control and repair: 2,291 lines, 68 definitions;
- CLI parsers and dispatch: 1,039 lines, 11 definitions;
- and 10 smaller domains.

### SYRD-353 (slice 13b): root's runtime baseline and plan helpers

Measured on `e921d47`, on a regenerated exact-tree graph. The design was
posted **before** any edit.

**The closure, corrected.** The "18 defs, 805 lines" reported in SYRD-352
counted `refresh_generated_project_runtime_artifacts` itself. Without it the
closure is 17 definitions, 529 lines. Every one is reached only from the
refresh or from another member, and no other `scripts/` module names any of
them.

**Moved: 13 definitions,** the responsibility the ticket names, in baseline order:
- owner evidence: `_provision_owner`, `SYSTEMD_SYSTEM_UNIT_DIR` (with its
  `#:` comment), `_root_controlled_record`, `_unit_environment`,
  `legacy_owner_from_host_records`;
- the no-follow ownership repair: `repair_legacy_provision_ownership`;
- the baseline: `reconstruct_privileged_baseline`, `_privileged_baseline_plan`;
- current identities: `plan_for_current_identities`,
  `_tenant_runs_on_project_account`;
- the projection: `authoritative_refresh_plan`;
- privacy: `close_privileged_artifacts`,
  `privileged_provision_privacy_problems`.

**Left with the refresh,** each belonging to a different facility:
- `_plan_replacements` (11 lines), the CLI values;
- `_tenant_copy_is_current` (8), publishing;
- `installed_controller` (11), the control grant;
- `_path_containment_error` (10), the renderer.

Shared helpers with callers outside the closure stay in the launcher as seams:
`_validated_role_names`, `_regenerated_field_divergence`,
`plan_workflow_from_root`, `build_plan`, `resolve_control_user`, the provision
and registry roots, the no-follow walk, and so on.

**The move.** New `scripts/privileged_runtime_plan.py`, 585 lines.
- **Seams:** 24 launcher names, 30 reads, are read at call time. They include
  the 6 names the moved definitions use of each other, so every name resolves
  on the launcher as at the baseline. This is required, not cautious:
  - `root_workflow_record_test` patches `team_launcher._provision_owner`;
  - two suites set `team_launcher.SYSTEMD_SYSTEM_UNIT_DIR` to a sandbox. A
    module-local read there would reach the real `/etc/systemd/system`.
- **Own imports:** `errno`, `json`, `os`, `pwd`, `shlex`, `stat`, `Path`,
  `dataclasses.replace` and the `typing` names, the very objects the launcher
  has, so `pwd`/`os` patches still reach.
- **Type only:** `ProjectConfig` and `ProjectBoardProvision`, under
  `TYPE_CHECKING`.
- **The launcher** imports the 13 names after the `director_upgrade` import.
  The refresh still calls 8 of them by name, so
  launcher patches still reach it. The launcher goes from 17,926 to 17,423 lines.
  Every existing module is unchanged.

**The proof.**
- `equiv2.py` holds.
- A supplement (`supp353.py`) holds. It checks:
  - per function, the approved seams at exactly the counts the BASELINE
    function loads them, computed from the baseline's own AST, none bare,
    with one call-time import first;
  - the own imports, and each is the launcher's object;
  - the two `TYPE_CHECKING` names;
  - every signature;
  - the unit-directory constant's statement and value;
  - one re-export, in place, and one object per name in both import orders;
  - every other script byte-identical;
  - baseline definition order.
- I planted 22 faults and every one was caught.
  - The first run missed a reordering, and that is why the order check was
    added.
  - A "late import" fault that only moved a comment was replaced with a real
    one.

The scope-aware scan was run on the module **explicitly**: `scope scan, privileged_runtime_plan.py explicitly: []`. The global
scan (55 refactor modules) finds nothing.

**Readers.** `scan_all` is rooted at the repo, and a control name
(`current_user_name`, 128 hits) shows the scan is live.
- The three patch sites above.
- Direct callers through the launcher in five suites.
- No source guard reads these names, so no guard changed.

**Evidence.**
- **New boundary test:** `tests/privileged_runtime_plan_boundary_test.py`,
  110 checks. Temporary roots only; the passwd database, directory
  ownership and every chown are the test's own answers; the only host file
  read is `/etc/passwd`. It covers:
  - the launcher's patches reaching the moved code;
  - the provision owner: a link not followed, root-owned, no account;
  - root-controlled records: link, non-regular, not root's, writable by
    others;
  - unit environment parsing;
  - the legacy owner established from agreeing records, and each of 14
    refusals;
  - the ownership repair: not root-owned, a dry run chowning nothing, applied
    to the directory then the named files, and link, hard-link, path and `..`
    refusals, including a linked directory;
  - reconstruction: only validated tenant values, root's facts and the
    normalised source, worktrees, a declared workflow, each objection, the
    builder's refusal, divergence with the operator-cache exception, an
    established owner, and passwd problems;
  - root's stored copy with the operator's values, a corrupt copy refusing,
    and reconstruction from the document as first read;
  - project-account identities;
  - the projection's accepted and refused entries, and worktrees;
  - privacy mode and owner; closing only named regular files.
- **Mutations:** 46 run serially, restored clean; 45 killed, each
  by its property.
  - One survivor is equivalent: "a boolean port accepted". A boolean is 0 or
    1, so it already fails the 1024–65535 check, and no input can tell the
    two apart.
  - The re-export-drop kill is the import-order assertion.
- **Suites:** identical on both trees:
  - the 16 boundary suites and both lints; the git-ownership lint's two
    findings are the baseline's, shifted −503;
  - per case: `desktop_access_test`; `root_workflow_record_test` (its 4
    rendering cases); `syrd_87_process_authority_projection_test` (10 cases,
    which drive `plan_for_current_identities`, the projection and the
    identity check);
  - the publication source guard.
- **Containment:** live snapshots around every run. **No live change.**
- **Not run:**
  - `adopt_workflow_test` (injected root and a migration);
  - `legacy_root_owned_provision_upgrade_test` and
    `privileged_plan_read_no_follow_test` (root-only);
  - `privileged_artifact_privacy_test` (bind-mounts over `/etc/passwd`);
  - `workflow_pane_rebind_test` (a real tmux server);
  - `root_workflow_record_test`'s privileged cases (namespace root);
  - the ticket's excluded suites.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so is
  `scripts/team-launcher --help`. A staged release loads the module.

**Next, for a Director decision: the refresh command itself.**
`refresh_generated_project_runtime_artifacts`, lines 3047–3322, 276 lines, 45 statements,
with its 4 private helpers (40 lines).
- It calls 8 of the moved names through the
  launcher: `_privileged_baseline_plan`, `_provision_owner`, `authoritative_refresh_plan`, `close_privileged_artifacts`, `legacy_owner_from_host_records`, `plan_for_current_identities`, `privileged_provision_privacy_problems`, `repair_legacy_provision_ownership`.
- Its callers are U3 in `upgrade_phases` and the role-identity cutover (two
  sites), all through the launcher.

**SYRD-272 is not complete.** The launcher is still 17,423 lines.

By domain (`domains.py`):
- release selection, install and upgrade: 3,207 lines, 96 definitions;
- provisioning (new/register/teardown/owner accounts): 3,158 lines, 118 definitions;
- general helpers (unclassified): 2,474 lines, 204 definitions;
- project config and registry: 2,388 lines, 65 definitions;
- privileged boundary, tenant control and repair: 1,998 lines, 62 definitions;
- CLI parsers and dispatch: 1,039 lines, 11 definitions;
- and 10 smaller domains.

### SYRD-354 (slice 13c): the generated runtime artifact refresh

Measured on `d1116a8`, on a regenerated exact-tree graph. The design was
posted **before** any edit.

**Moved: 5 definitions, unchanged and in baseline order,** with their comments:
- `_plan_replacements` (11 lines);
- `_tenant_copy_is_current` (8);
- `installed_controller` (11);
- `_path_containment_error` (10), whose local renderer-exception import is
  kept;
- `refresh_generated_project_runtime_artifacts` (276 lines, 45 statements),
  with its nested `_for_current_identities`. The nested helper's annotations
  stay strings.

`publish_tenant_artifact`, which sat between them, is shared and stays.

**The move.** New `scripts/runtime_artifact_refresh.py`, 365 lines.
- **Seams:** 24 launcher names, 39 reads, are read through a call-time
  import:
  - the 19 shared facilities: the tenant document reader, plan parser,
    renderer, checkout, publisher, installer, root's provision roots, and
    SYRD-353's baseline, projection, ownership and privacy helpers;
  - `LauncherUpgradeResult`, 13 constructions, so every result is the one
    public class;
  - `resolve_control_user`;
  - the four moved helpers the refresh calls. This keeps every name
    resolving on the launcher as at the baseline.
- **The nested helper** reads `plan_for_current_identities` through the
  command's own call-time import.
- **Own imports:** `os`, `subprocess`, `replace`, `Path`, `Any` and
  `Callable`, the launcher's very objects. The defaults are `subprocess.run`
  and `print` themselves.
- **Type only:** `ProjectConfig`, `ProjectBoardProvision` and the
  `LauncherUpgradeResult` annotation, under `TYPE_CHECKING`.
- **The launcher** imports the 5 names after the `privileged_runtime_plan`
  import. U3 and the role-identity cutover's two sites still call the refresh
  through the launcher. The launcher goes from 17,423 to 17,104 lines. Every other
  existing module is unchanged.

**The proof.**
- `equiv2.py` holds.
- The supplement (`supp354.py`, from SYRD-353's) holds. It checks:
  - per function, the approved seams at the baseline's own free-load counts,
    none bare;
  - the own imports and their identity;
  - the `TYPE_CHECKING` names;
  - signatures, and the runner and print defaults as the very objects;
  - the nested helper, unchanged apart from its one seam;
  - the local import verbatim;
  - the re-export position, and both import orders;
  - every other script byte-identical;
  - baseline order.
- Its "none bare" check now also ignores the RETURN annotation's names. It
  had flagged `-> LauncherUpgradeResult`. A bare runtime use is still caught
  by the per-name count, as the planted "one result built bare" fault shows.
- I planted 22 faults and every one was caught.

The scope-aware scan was run on the module **explicitly**: `scope scan, runtime_artifact_refresh.py explicitly: []`. The global
scan (56 refactor modules) finds nothing.

**The guard.** `privileged_runtime_plan_boundary_test` (SYRD-353's own) found
the refresh in the launcher's source and required its 8 calls to the baseline
helpers by bare name.
- On the candidate it first fails, with a `StopIteration`: the function is no
  longer in the launcher.
- It now finds the refresh in the new module and requires the same 8 names
  through `launcher.`, none bare, and a bypass of one is killed.
- `upgrade_phases_boundary_test`'s U3 seam table and its patch of the refresh
  on the launcher are unaffected.

**Evidence.**
- **New boundary test:** `tests/runtime_artifact_refresh_boundary_test.py`,
  62 checks. Every launcher facility is a recording fake installed
  before the command runs; the effective uid is patched; tenant copies are
  files in temporary directories. It covers:
  - the document read first, absolute and never resolved (a linked checkout
    and a relative path), with missing and bad refusals;
  - operator values normalised, an empty cache refused, and the parse
    refusal;
  - the tenant render: the controller from the grant, the operator's source,
    the checkout, current identities, and no linger;
  - the containment refusal named by field;
  - the legacy root-owned repair: only as root without a stored baseline and
    for a root-owned directory; its refusals; dry run versus apply; the notes
    in every later outcome;
  - tenant copies: current, a linked copy republished, a dry run, a refused
    publication;
  - non-root wording and the stage instruction;
  - root judging a copy of the document as found, the projection's refused
    entries said, replacements and checkout, and the final message with
    added roles;
  - each root outcome: baseline refusal, privacy-only repair, current, dry
    run with would-repair, install `OSError` with and without tenant changes;
  - other errors propagate.
- **Mutations:** 42 of 42 are killed, serially, each by its
  property, with a clean restore.
  - One first-run survivor was a fixture gap: the ownership note carried into
    later outcomes. A case now covers it.
  - The re-export-drop kill is the import-order assertion.
- **Suites:** identical on both trees:
  - the 17 boundary suites (SYRD-353's widened one included) and both lints;
    the git-ownership lint's findings are shifted −319;
  - per case: `desktop_access_test`, `root_workflow_record_test` and
    `syrd_87_process_authority_projection_test`;
  - the publication source guard;
  - `team_launcher_plan_migration_test`'s 3 plain cases and its two
    UNPRIVILEGED drivers (`unprivileged_migration`,
    `unprivileged_commit_store_repair`). These run the real refresh in
    temporary roots. They were called from a scratch driver, never its
    `main()` or namespace children: 5/5 on both trees.
- **Containment:** live snapshots around every run. **No live change.**
- **Not run:**
  - `legacy_root_owned_provision_upgrade_test` and
    `privileged_plan_read_no_follow_test` (root-only);
  - `privileged_artifact_privacy_test` (bind-mounts over `/etc/passwd`);
  - the namespace-root halves of `team_launcher_plan_migration_test` and
    `team_launcher_privileged_artifacts_test`;
  - the ticket's excluded suites.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so is
  `scripts/team-launcher --help`. A staged release loads the module.

**Next, for a Director decision: adding a role to a tenant.**
`add_project_role_command` and its 9 dedicated helpers, 424 lines:
`_is_recognized_generated_project_layout`, `_vcs_close_role_from_plan_data`, `_project_plan_for_added_role`, `_next_visible_role_slot`, `_add_role_payload`, `_update_project_design_artifact_for_role`, `_write_added_role_config`, `_write_updated_project_plan_artifacts`, `_apply_add_role_board_sql`, `add_project_role_command`.
- Its launcher closure is 15 defs, 478 lines. Five of them are also named
  by other modules or suites and would stay seams: `local_account_exists`,
  `role_account_name`, `_layout_leaves`, `role_control_accounts` and
  `_layout_slot_count`.
- **Other bounded candidates:**
  - the deploy-ref and release-status helpers around `tenant_release_status`
    (171 lines dedicated; `tenant_release_status` itself is
    named by another module and 6 suites);
  - `switchyard_repair_boundary_command` (185 lines, no dedicated helpers);
  - `set_project_role_runtime_command` (162 lines with 2 helpers).
- The provisioning domain (`new_project_command` and `switchyard_new_command`,
  88 defs, 2,349 lines) and the CLI dispatch (`switchyard_main`, 481 lines;
  `main`, 220) are the largest remaining, and each needs a phase plan of its
  own.

**SYRD-272 is not complete.** The launcher is still 17,104 lines.

By domain (`domains.py`):
- release selection, install and upgrade: 3,207 lines, 96 definitions;
- provisioning (new/register/teardown/owner accounts): 3,158 lines, 118 definitions;
- general helpers (unclassified): 2,433 lines, 201 definitions;
- project config and registry: 2,110 lines, 64 definitions;
- privileged boundary, tenant control and repair: 1,998 lines, 62 definitions;
- CLI parsers and dispatch: 1,039 lines, 11 definitions;
- and 10 smaller domains.

### SYRD-355 (slice 14a): adding a role to a tenant

Measured on `16629f8`, on a regenerated exact-tree graph. The design was
posted **before** any edit.

**Moved: 10 functions, 424 lines, unchanged and in baseline order,** into a new
`scripts/project_role_add.py` (498 lines):
- `_is_recognized_generated_project_layout`, `_vcs_close_role_from_plan_data`,
  `_project_plan_for_added_role`, `_next_visible_role_slot`,
  `_add_role_payload`, `_update_project_design_artifact_for_role`,
  `_write_added_role_config`, `_write_updated_project_plan_artifacts`,
  `_apply_add_role_board_sql`, `add_project_role_command`;
- the VCS close-role command's own three helpers, interleaved with them, stay;
- the local `role_account_commands` import is kept verbatim.

**The shared account leaf.** `add_project_role_command` binds
`account_exists=local_account_exists` when the def runs, so the default cannot
be looked up through the launcher at call time.
- `local_account_exists` (10 lines, a pure passwd lookup) moved unchanged to
  the existing leaf `scripts/host_accounts.py` (23 → 41 lines). That is
  where `home_dir_for_user` went for the same reason in SYRD-288.
- The launcher's import from the leaf gained the name, and the new module
  imports it at its top. The default is therefore the one object in every
  import order.
- Every launcher caller, `worker_pool`, the role-identity cutover and the 18
  test patch sites still reach it through the launcher. The default was never
  reachable by a launcher patch.

**The file-origin normalization.** The pane-script fallback
`Path(__file__)...with_name(TEAM_LAUNCHER_NAME)` now reads
`Path(launcher.__file__)`, the launcher's own file at call time, as declared.
A test sets the launcher's `__file__` somewhere else and sees the fallback
follow it.

**Seams:** 74 call-time reads through the launcher. They cover the plan
loaders and builder, the renderers, the owner runner, the worktree and pane
helpers, the terminal selector, both constants, `__file__`, and each moved
helper another moved function calls.
- **Own imports:** `json`, `sys`, `subprocess`, `replace`, `Path`, `Any` and
  `Callable`, the launcher's very objects.
- **Builtins:** `print` and `input` stay bare.
- **Type only:** `ProjectConfig` and `ProjectBoardProvision`, under
  `TYPE_CHECKING`.
- **The launcher** goes from 17,104 to 16,660 lines.

**The proof.**
- `equiv2.py` over both moves: the ten and the leaf function are
  AST-identical modulo `launcher.`. Its launcher-node step does not model a
  move into an EXISTING module, and it reports the baseline's own
  `host_accounts` import as missing, by construction.
- The supplement (`supp355.py`) checks that step exactly (H3): the launcher
  is the baseline minus the 11 defs, with the `host_accounts` import widened
  by exactly one name and one new import block in place. It also checks:
  - H1: the leaf's baseline nodes unchanged, its docstring only extended, and
    exactly `local_account_exists` appended;
  - H2: the leaf is still a leaf;
  - per-function seams against the baseline's free loads;
  - `__file__` read only as `launcher.__file__`;
  - the default objects: `account_exists` is the leaf's and the launcher's;
    runner, print and input;
  - the local import, the re-export, both import orders, baseline order, and
    every other script byte-identical.
- I planted 21 faults and every one was caught.

The scope-aware scan was run on both modules **explicitly**: `scope scan, project_role_add.py and host_accounts.py explicitly: []`.

**Readers.** `scan_all` is rooted at the repo, and the control has 128 hits.
- `add_project_role_command` is patched on the launcher by the CLI suite, and
  driven through it by two suites.
- `_project_plan_for_added_role` is driven through it by
  `role_onboarding_prompt_test`.
- No source guard names these functions.

**Evidence.**
- **New boundary test:** `tests/project_role_add_boundary_test.py`, 80 checks.
  Recording fakes on the launcher and temporary files. It covers:
  - the leaf's passwd answers;
  - the CLI chosen at a terminal, the codex default, a named CLI, and stdin
    deciding;
  - validation and the SQL preflight before any write, including an auditor;
  - a new role's full order, as the owner, started detached with its own
    state directory;
  - recovery without a rewrite;
  - the missing-account handoff with the whole role-control set;
  - errors stopping where they happen;
  - the launcher-file fallback and its precedence;
  - slots, payloads and the close role;
  - the design artifact;
  - plan artifacts and board SQL;
  - the role plan from the recorded plan;
  - a recognized layout;
  - every config-writing rule.
- **Mutations:** 44 of 44 are killed, each by its property, with a
  clean restore.
  - Two first-run crash kills were made assertions.
  - The re-export drop is the import-order assertion.
- **Suites:** identical on both trees:
  - the boundary suites (including `upstream_report_boundary_test`, the
    leaf's guard) and both lints; the git-ownership lint is shifted −11;
  - per case: `team_launcher_new_project_test`'s six add-role cases, the CLI
    suite's two add-role dispatch cases, `role_onboarding_prompt_test`'s
    declarative-tenant refusal, `desktop_access_test`,
    `root_workflow_record_test` and `syrd_87`;
  - `team_launcher_add_role_vcs_test` is RED ON THE BASELINE: team_launcher_add_role_vcs_test.py syrd 6/2 pristine 6/2 excluded=0 cases=8 IDENTICAL. The same
    two cases fail on both trees, with identical logs, and it is not fixed
    here.
- **Containment:** live snapshots around every run. **No live change.**
- **Not run:**
  - the upgrade and migration suites that patch `local_account_exists`
    (their patches reach the launcher attribute unchanged);
  - the ticket's excluded suites.
- **CLI:** `switchyard --help` is identical for all 36 invocations (including
  `add-role`), and so is `scripts/team-launcher --help`. A staged release loads
  the module.

**Next, for a Director decision: the VCS close-role command.**
`set_project_vcs_close_role_command` and its dedicated helpers
`_project_plan_for_vcs_close_role`, `_write_vcs_close_role_artifacts` and
`_apply_vcs_close_role_board_sql`: 106 lines.
- Its launcher closure is 11 defs, 186 lines. The other seven are the
  plan-data helpers it now shares with `project_role_add`
  (`_configured_implementer_roles`, `_configured_audit_roles`,
  `_regenerated_control_user`, `_loaded_plan_field`,
  `_owner_home_from_plan_data`, `_commit_git_dir_from_plan_data`,
  `_install_and_restart_board_unit`), and they stay seams.
- A later slice could give those shared plan-data helpers a module of their
  own.
- **Other bounded candidates:**
  - `set_project_role_runtime_command` (162 lines, 2 helpers);
  - `switchyard_repair_boundary_command` (185 lines);
  - the deploy-ref helpers around `tenant_release_status`.

**SYRD-272 is not complete.** The launcher is still 16,660 lines.

By domain (`domains.py`):
- release selection, install and upgrade: 3,207 lines, 96 definitions;
- provisioning (new/register/teardown/owner accounts): 3,146 lines, 117 definitions;
- general helpers (unclassified): 2,413 lines, 201 definitions;
- privileged boundary, tenant control and repair: 1,998 lines, 62 definitions;
- project config and registry: 1,744 lines, 59 definitions;
- CLI parsers and dispatch: 1,039 lines, 11 definitions;
- and 10 smaller domains.

### SYRD-356 (slice 14b): the VCS close-role command

Measured on `5b97e18`, on a regenerated exact-tree graph. The design was
posted **before** any edit.

**Moved: 4 functions, 106 lines, unchanged and in baseline order,** into a new
`scripts/project_vcs_close_role.py` (151 lines):
- `_project_plan_for_vcs_close_role`;
- `_write_vcs_close_role_artifacts`;
- `_apply_vcs_close_role_board_sql`;
- `set_project_vcs_close_role_command`.

`_install_and_restart_board_unit`, interleaved with them, is shared and stays.

**Callers:**
- the launcher's `main` and `switchyard_main`, by bare name;
- `tests/team_launcher_test_helpers.py`, which imports the name from the
  launcher.
All keep the same object through the re-export.

**Seams:** 29 call-time reads. They cover:
- the seven facilities shared with `project_role_add`
  (`_configured_implementer_roles`, `_configured_audit_roles`,
  `_regenerated_control_user`, `_loaded_plan_field` ×6,
  `_owner_home_from_plan_data`, `_commit_git_dir_from_plan_data`,
  `_install_and_restart_board_unit`);
- the plan-data readers, the builder and `ROLE_RE`, the renderers, the owner
  file and JSON writers, `_proc_failure_reason`, `current_user_name`;
- the three helpers the command calls.

Also:
- **Own imports:** `subprocess`, `Path`, `Any` and `Callable`, the launcher's
  very objects.
- **Type only:** `ProjectConfig` and `ProjectBoardProvision`.
- **Rule 27:** no comprehension variable is qualified.
- **The launcher** goes from 16,660 to 16,552 lines.

**The proof.**
- `equiv2.py` holds.
- The supplement (`supp356.py`) holds: per-function seams against the
  baseline's free loads, the own imports and their identity, signatures and
  default objects, the re-export in place, both import orders, baseline
  order, and every other script byte-identical.
- I planted 17 faults and every one was caught.

The scope-aware scan was run on the module **explicitly**: `scope scan, project_vcs_close_role.py explicitly: []`.

**Readers.** No rebind of any of the four names anywhere, with a live control
(130 hits), and no source guard.

**Evidence.**
- **New boundary test:** `tests/project_vcs_close_role_boundary_test.py`,
  29 checks. Recording fakes on the launcher; temporary files only. It
  covers:
  - the plan from recorded data: the normalised role, the configured roles,
    the designer, the owner fallbacks and the tenant's board root;
  - each refusal before anything is built;
  - the artifacts beside the configuration for the owner, in order;
  - psql as postgres and its refusal;
  - the command's order and the rendered plan's own closer;
  - each failure stopping where it happens.
- **Mutations:** 26 of 26 are killed, each by its property, with a
  clean restore.
  - One first-run kill came from an uncaught refusal. That case now
    asserts.
  - The re-export drop is the import-order assertion.
- **Suites:** identical on both trees:
  - the boundary suites and both lints; the git-ownership lint is shifted +6;
  - `team_launcher_add_role_vcs_test` per case: team_launcher_add_role_vcs_test.py syrd 6/2 pristine 6/2 excluded=0 cases=8 IDENTICAL. Its three
    set-vcs-close-role cases pass on both. The two failing cases are the
    baseline's known add-role failures, unchanged and not fixed here;
  - the per-case selections carried from SYRD-355.
- **Containment:** live snapshots around every run. **No live change.**
- **Not run:** the ticket's excluded suites.
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so is
  `scripts/team-launcher --help`. A staged release loads the module.

**Next, for a Director decision: the shared role-plan data helpers,**
80 lines: `_configured_implementer_roles`, `_configured_audit_roles`, `_regenerated_control_user`, `_loaded_plan_field`, `_owner_home_from_plan_data`, `_commit_git_dir_from_plan_data`, `_install_and_restart_board_unit`.
- After this slice none of them has a launcher caller except each other.
- They are used only by `project_role_add` and `project_vcs_close_role`,
  through the launcher.
- They are a coherent "regenerate a provisioned tenant's plan" module of
  their own.
- **The alternative:** the role-runtime command `set_project_role_runtime_command`
  (3 defs, 162 lines, with `_owner_catalog_args` and `_role_named`).

**SYRD-272 is not complete.** The launcher is still 16,552 lines.

By domain (`domains.py`):
- release selection, install and upgrade: 3,207 lines, 96 definitions;
- provisioning (new/register/teardown/owner accounts): 3,146 lines, 117 definitions;
- general helpers (unclassified): 2,419 lines, 202 definitions;
- privileged boundary, tenant control and repair: 1,998 lines, 62 definitions;
- project config and registry: 1,744 lines, 59 definitions;
- CLI parsers and dispatch: 1,039 lines, 11 definitions;
- and 10 smaller domains.

### SYRD-357 (slice 14c): the shared role-plan data and board-unit helpers

Measured on `424064e`, on a regenerated exact-tree graph. The design was
posted **before** any edit.

**Moved: 7 functions, 80 lines, unchanged and in baseline order,** into a new
`scripts/project_role_plan_support.py` (142 lines):
- `_configured_implementer_roles`, `_configured_audit_roles`,
  `_regenerated_control_user`, `_loaded_plan_field`,
  `_owner_home_from_plan_data`, `_commit_git_dir_from_plan_data` and
  `_install_and_restart_board_unit`;
- `_plan_data_from_config`, interleaved with them, is not part of the group
  and stays.

**Callers.**
- The launcher had none outside the group.
- `project_role_add` and `project_vcs_close_role` read all seven through the
  launcher and are byte-identical.
- The launcher imports the module BEFORE those two. They need not, since they
  read at call time, but the order is asserted.

**Seams:** 12 call-time reads:
- `NEW_PROJECT_RESERVED_ROLE_NAMES`, `_dedupe_role_names`,
  `current_user_name` ×2;
- the imported `resolve_control_user`, `invoking_human`,
  `_owner_home_for_auth` and `commit_git_dir_env_for_project`;
- the inter-helper calls, `_loaded_plan_field` ×3 and
  `_owner_home_from_plan_data`.

The eager owner-home and cache fallbacks, and the grant-derived controller,
are exactly as they were. The launcher goes from 16,552 to 16,467 lines.

**The proof.**
- `equiv2.py` holds.
- The supplement (`supp357.py`) holds. It adds checks that both consumer
  modules are byte-identical, and that the only defaults are the role
  helpers' `None` keywords.
- I planted 17 faults and every one was caught, including a lazy
  fallback, a dropped invoking human, the re-export placed after the
  consumers, and an edited consumer.

The scope-aware scan was run on the module **explicitly**: `scope scan, project_role_plan_support.py explicitly: []`.

**The guard.** `project_vcs_close_role_boundary_test` (SYRD-356's own)
required the launcher to DEFINE all seven. It first fails on the candidate:
`AssertionError: the launcher defines none of them, and keeps every shared facility`. It now requires each to be bound at the launcher's top level,
defined or re-exported, so still patchable there. Same seven names.

**Evidence.**
- **New boundary test:** `tests/project_role_plan_support_boundary_test.py`,
  37 checks. Recording fakes on the launcher. It covers:
  - implementer lists: recorded, normalised, deduped, malformed,
    configured with reserved names filtered, extra only if absent, empty
    recorded;
  - audit lists through the launcher's de-duplicator, and the fallback;
  - `_loaded_plan_field` for missing, `None`, `""`, `False`, `0` and `[]`;
  - the grant-derived controller for the invoking human and the recorded,
    configured or current owner;
  - the eager owner-home and cache fallbacks, computed even when a value is
    recorded;
  - the unit's install, reload and restart, stopping at each failure;
  - the consumers' reads and the import order.
- **Mutations:** 25 of 25 are killed, each by its property, with a
  clean restore. The re-export drop is the import-order assertion.
- **Suites:** identical on both trees:
  - the boundary suites (both consumers' included) and both lints; the
    git-ownership lint is shifted +9;
  - the per-case selections;
  - `team_launcher_add_role_vcs_test`: team_launcher_add_role_vcs_test.py syrd 6/2 pristine 6/2 excluded=0 cases=8 IDENTICAL. These are the two known
    baseline add-role failures, unchanged.
- **Containment:** live snapshots around every run. **No live change.**
- **CLI:** `switchyard --help` is identical for all 36 invocations, and so is
  `scripts/team-launcher --help`. A staged release loads the module.

**Next, for a Director decision: the role-runtime command.**
`set_project_role_runtime_command` (150 lines) and its dedicated
`_role_named` (5 lines). `_role_named` is a different function from the
same-named locals in `workflow_manage` and `onboarding_readiness`.
- Its launcher closure is 3 defs, 162 lines. `_owner_catalog_args` (7) is
  also read by `model_validation` through the launcher, and stays a seam.
- It reads `_owner_command_env_args`, `_role_cli_name` and `_runtime_field`
  from the launcher.

**SYRD-272 is not complete.** The launcher is still 16,467 lines.

By domain (`domains.py`):
- release selection, install and upgrade: 3,207 lines, 96 definitions;
- provisioning (new/register/teardown/owner accounts): 3,124 lines, 115 definitions;
- general helpers (unclassified): 2,424 lines, 202 definitions;
- privileged boundary, tenant control and repair: 1,998 lines, 62 definitions;
- project config and registry: 1,704 lines, 57 definitions;
- CLI parsers and dispatch: 1,039 lines, 11 definitions;
- and 10 smaller domains.
