"""SessionStart hook: log the payload, and give additionalContext naming the source (as the pane hook gives the role prompt)."""
import json, sys, time
log = sys.argv[1]
data = json.load(sys.stdin)
with open(log, "a") as f:
    f.write(json.dumps({"t": time.time(), **data}) + "\n")
if data.get("hook_event_name") == "SessionStart":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": f"ROLE-PROMPT-MARKER source={data.get('source')}: You are Router."}}))
