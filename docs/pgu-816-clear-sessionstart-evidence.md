# PGU-816 Clear SessionStart Evidence

Captured in disposable homes and isolated tmux servers on 2026-08-31. No live
team pane was reinstalled or restarted.

## Claude Code 2.1.251

Disposable pane: `/home/agent/pgu-816-probes/claude-1788200867-22044`

After submitting `/clear` and then a follow-up prompt, Claude Code emitted a new
`SessionStart` payload with:

```json
{
  "hook_event_name": "SessionStart",
  "source": "clear",
  "session_id": "a55580a5-20be-4604-b115-c29137499347"
}
```

The initial startup payload used `"source": "startup"`.

## Codex 0.151.0

Disposable pane: `/home/agent/pgu-816-probes/codex-1788200573-19509`

After submitting `/clear` and then a follow-up prompt, Codex emitted a new
`SessionStart` payload with:

```json
{
  "hook_event_name": "SessionStart",
  "source": "clear",
  "session_id": "01a0590f-a5e0-7cf3-88a4-f474289231d2"
}
```

The initial startup payload used `"source": "startup"`.

End-to-end active-work proof:
`/home/agent/pgu-816-probes/codex-e2e-1788200640-31296` used a scratch board
with PGU-816 assigned to ops. After `/clear`, Codex displayed the hook context:

```text
Your session was just cleared. You have ACTIVE work: PGU-816 -- A cleared pane gets active-work injection
```

When asked for the active ticket id from hook context, the real pane replied
`PGU-816`.

## Agy 1.1.22

Disposable pane: `/home/agent/pgu-816-probes/agy-1788201210-17463`

Agy emitted an initial `SessionStart` payload, but it did not include a
`source`/`reason` equivalent:

```json
{
  "conversationId": "6b3c637b-c190-4a43-8f49-0dc0f3694137",
  "modelName": "gemini-3.7-flash-high"
}
```

Submitting `/clear` did not emit a second `SessionStart`; only the follow-up
`PreInvocation`, `PostInvocation`, and `Stop` hooks fired. The PGU-816 `clear`
source injection therefore does not cover agy 1.1.22 today.

## Hermes 0.15.2

Disposable pane: `/home/agent/pgu-816-probes/hermes-1788201329-25576`

The Hermes hook tester emits `on_session_start` payloads without a
`source`/`reason` equivalent:

```json
{
  "hook_event_name": "on_session_start",
  "session_id": "test-session"
}
```

The interactive chat probe showed `/clear` opens the fresh-session flow, but did
not capture a clear-source hook from chat. Hermes also does not honor the
Claude/Codex `hookSpecificOutput.additionalContext` shape, so the
`hermes.on_session_start` label is only useful for idle/session recording. It is
not counted as active-work injection support.

Round 2 uses two Hermes-specific mechanisms:

- Identity/body/scope/constraints: `hermes.pre_llm_call` emits a flat
  `{"context": "..."}` payload. Hermes 0.15.2 consumes that shape before each
  model call.
- No previous-ticket inheritance: Hermes has no dependable reset flag, so the
  launcher starts Hermes fresh for each ticket. The old session record is moved
  to the `.superseded` sidecar before the next fresh session starts.

End-to-end active-work and no-inheritance proof:
`/home/agent/pgu-816-probes/hermes-round2-e2e-1788202840-527198` used a scratch
board and two real Hermes invocations inside isolated tmux panes. First, the
worker received old-ticket context:

```text
OLD:PGU-815; OLD_BODY:OLD-BODY-PGU-815; OLD_SCOPE:OLD-SCOPE-PGU-815; OLD_CONSTRAINT:OLD-CONSTRAINT-PGU-815
```

The launcher clear path then superseded the old session record:

```text
old=20260831_150040_e15c46
superseded=20260831_150040_e15c46
new=20260831_150055_33c760
```

The next fresh Hermes pane received the next ticket's injected context and did
not report any prior-ticket context:

```text
CURRENT:PGU-816; BODY:NEXT-BODY-PGU-816; SCOPE:NEXT-SCOPE-PGU-816; CONSTRAINT:NEXT-CONSTRAINT-PGU-816
PREVIOUS:NONE
```

The wrapper log also captured the exact flat hook contexts emitted to Hermes:

```text
You have ACTIVE work: PGU-815 -- Old Hermes ticket (in_progress, assigned to you).
  OLD-BODY-PGU-815
  OLD-SCOPE-PGU-815
  OLD-CONSTRAINT-PGU-815
You have ACTIVE work: PGU-816 -- Next Hermes ticket (in_progress, assigned to you).
  NEXT-BODY-PGU-816
  NEXT-SCOPE-PGU-816
  NEXT-CONSTRAINT-PGU-816
```
