"""quiet-background: keep subagent/background results out of the chat.

The gateway injects async-delegation and background-process completions to the model as an
internal turn (the raw block is never posted to the chat), but the model's REPLY to that turn
is posted. This hook nudges that reply to be a short summary, or [SILENT] when nothing changed
(the gateway allows bare silence only on internal turns).
"""

_MARKERS = (
    "[INTERNAL NOTIFICATION",            # gateway footer on every internal wake
    "[ASYNC DELEGATION",                 # single + batch subagent completions
    "background subagent delegations completed",
    "[IMPORTANT: Background process",
    "[Background process ",              # heartbeat
)

GUIDANCE = (
    "[Display policy] This turn is an internal background notification, not a message from the user. "
    "The user cannot see it and does not want subagent/background results relayed. Do NOT paste or "
    "restate raw results, logs, transcripts or per-task breakdowns. If it changes nothing the user is "
    "waiting on, reply with exactly [SILENT]. Otherwise give at most 2-3 short sentences with the "
    "conclusion or the next action (one short line for a failure that needs their attention)."
)


def _text(msg):
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):  # multimodal content parts
        return " ".join(str(p.get("text", "")) for p in msg if isinstance(p, dict))
    return ""


def _on_pre_llm_call(user_message=None, parent_session_id="", **_kw):
    if parent_session_id:  # a subagent's own turn: leave it alone
        return None
    text = _text(user_message)
    if text and any(m in text for m in _MARKERS):
        return {"context": GUIDANCE}
    return None


def register(ctx):
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
