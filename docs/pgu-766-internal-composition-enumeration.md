# PGU-766 Internal Composition Enumeration

Measured against current `origin/main` as of 2026-08-29.

## Result

I do not recommend building the trusted internal-action primitive yet. The three original composition failures are fixed on current main, and the current measurement found no remaining broken caller/operation pair where an HTTP-granted action reaches `route()`, `file_bug()`, or `create_ticket()` and is then refused solely because the nested primitive re-authorizes the original caller.

The remaining issues are narrower:

| Class | Broken pair or gap | Current status | Recommendation |
| --- | --- | --- | --- |
| Historical internal composition | `ops/main/app/perf/research/audit -> file_bug -> route()` | Fixed by making `file_bug` create the linked ticket directly instead of routing afterward. | No primitive needed for this case. |
| Historical internal composition | `director -> create_ticket(parent_id=...) -> file_bug()` | Fixed by keeping director/user parented creates on `create_ticket` plus `edit_fields(parent_id)`. | No primitive needed for this case. |
| Historical internal composition | `user -> create_ticket(assignee=...) -> route()` | Fixed by passing assignee into `create_ticket`, avoiding a nested director-only route. | No primitive needed for this case. |
| Field/action semantic mismatch | `any caller -> edit_fields(commit_hash=...)` | Still rejected before the SQL function API because commit hashes are only valid through `submit_to_audit` or `mark_done`. | Keep as bespoke policy; do not make generic `edit_fields` a commit path. Track/fix under PGU-770. |
| Direct HTTP/SQL RBAC mismatch | `user -> await_role`, `user -> clear_awaiting_role` | HTTP allowed `user`, SQL denied it. | Fixed in PGU-766 by removing `user` from the HTTP allowed sets. |
| Client lagging server | `write_client.create_ticket(parent_id=...)` missing | Server accepts `parent_id`; client could not express it. | Fixed in PGU-766. |
| Client lagging server | `write_client.edit_fields(...)` missing | Server exposes `edit_fields`; client could not express it. | Fixed in PGU-766. |

## Operation Surface

| Operation | HTTP allowed roles | Actor-gated primitive(s) reached | Broken caller/operation pairs after PGU-766 |
| --- | --- | --- | --- |
| `create_ticket` | `director`, `user` | `create_ticket`; optionally `edit_fields(parent_id)`, `edit_fields(needs_*)`, `add_comment` | None. |
| `file_bug` | implementers, `audit` | `file_bug`; optionally `edit_fields(needs_user_signoff/regression/attachments)`, `add_comment` | None. |
| `release_draft` | draft roles, `director`, `user` | `release_draft` | None. |
| `route` | `director` | `route` | None. |
| `force_move`, `override_move` | `director` | `force_move` | None. |
| `start_work` | implementers | `start_work` | None. |
| `submit_to_inspection` | implementers | `submit_to_inspection` | None. |
| `submit_to_audit` | implementers | `submit_to_audit` | None. |
| `implementer_kick_back` | implementers | `implementer_kick_back` | None. |
| `request_commit_exempt` | implementers | `request_commit_exempt` | None. |
| `start_task`, `complete_task` | implementers, `director`, `audit`, `inspector` | `start_task`, `complete_task` | None. |
| `await_role`, `clear_awaiting_role` | all caller roles except `user` | `set_awaiting_role`, `clear_awaiting_role` | None after removing the false `user` grant. |
| `inspector_sign_off`, `inspector_kick_back` | `inspector` | `inspector_sign_off`, `inspector_kick_back` | None. |
| `audit_sign_off`, `audit_kick_back` | `audit` | `audit_sign_off`, `audit_kick_back` | None. |
| `director_dat_sign_off`, `director_dat_kick_back` | `director` | DAT sign-off/kick-back primitives | None. |
| `user_sign_off`, `user_reopen` | `user` | `user_sign_off`, `user_reopen` | None. |
| `mark_done`, `defer`, `cancel` | `director` | `mark_done`, `defer`, `cancel` | None. |
| `set_manually_controlled`, `set_blockers` | `director` | `set_manually_controlled`, `set_blockers` | None. |
| `add_comment` | all caller roles | `add_comment` | None. |
| `edit_fields` | all caller roles | `edit_fields` | No actor-gate mismatch; `commit_hash` remains a semantic mismatch. |
| `crop_attachment` | `director`, `user` | attachment crop plus `edit_fields`-style persistence | None. |
| `merge` | `director` | `merge_tickets` | None. |

## Recommendation

Close this ticket without adding a trusted internal-action primitive. A primitive is still a valid design if future actions need genuine privileged composition, but current main no longer has a live route/file_bug/create composition failure. The safer course is to keep the external safety net, fix each measured disagreement at the smallest boundary, and add targeted tests when a new cross-layer mismatch appears.
