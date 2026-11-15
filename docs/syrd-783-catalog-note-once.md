# SYRD-783: `switchyard new` says once why its model lists are only recorded ones

`switchyard new` asks every role's model before the agent account exists, so it can only offer Switchyard's recorded
catalog. Each list said so in a long sentence: "UNVERIFIED: recorded … (catalog v…) and not checked against this
account; the test13-agent account does not exist yet, so its own list could not be read; this choice is confirmed
against it after the account is created". The sentence came once per role, and again for Router and a Librarian, so
a six-role project printed it a dozen times. The User found it buried the questions.

## Now

- **Said once:** before the first model question, the role plan prints one line,
  `runtime_catalog.recorded_catalog_note(account)`:

  > switchyard: model lists come from Switchyard's recorded catalog (v3, 2026-11-15); test13-agent does not exist
  > yet, so each choice is confirmed against it after the account is created

- **Each list:** marked `(recorded)` and nothing more. The role plan passes `runtime_catalog.SAID_ONCE` as
  `unverified_because`, and `Catalog.note` returns `recorded` for it. Router's and a Librarian's lists get the same
  mark, through the same value.
- **Still honest (SYRD-250):** a recorded list is still marked, never presented as the account's own (`enumerable`
  stays false), and each choice is still confirmed against the account once it exists
  (`confirm_unknown_models_with_owner`, unchanged).
- **An account that exists:** nothing changes. There is no run note and no mark, and its lists say what they always
  said.
- **Other prompts:** `set-role-runtime` asks one role's model, so it says its note once already. `add-role` asks no
  model question. The post-creation confirmation offers the account's own list.

## Tests

`tests/catalog_note_once_test.py` runs the real role plan with Router and a Librarian chosen:

- **With no account:** the note comes exactly once, in one line, before the first model question. No line carries the
  long sentence, and all seven model lists (five roles, Router and the Librarian) are each marked `(recorded)`.
- **On a real pty, with the real `input`:** the same, once.
- **With an account that exists:** unchanged.
- **The notes themselves:** `SAID_ONCE` gives `recorded` and the list is not enumerable; any other reason is still
  said in full; a live list is unchanged.
- **The confirmation after creation:** `owner_model_catalog_test`'s cases still pass. It asks again, and still
  refuses a model the account does not offer.

`role_plan_prompt_boundary_test`'s golden was re-recorded for its two no-account cases, and its seam count names the
catalog note.
