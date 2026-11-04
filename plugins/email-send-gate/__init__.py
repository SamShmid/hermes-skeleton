"""Gate every Gmail send, calendar RSVP and guest invite behind the human approval prompt.

The rule key is unique per call, so "allow for session" / "always" never carries over to the next email.
"""
import uuid

SEND_TOOL = "gmail_send_draft"


def approval_message(args: dict) -> str:
    cc = f", cc {args.get('cc')}" if args.get("cc") else ""
    return (f"Send email from Google account '{args.get('account', '?')}' to {args.get('to', '?')}{cc}. "
            f"Subject: {args.get('subject', '?')}")


def calendar_message(tool: str, args: dict):
    """Approval text for calendar calls that email other people; None when nobody is notified."""
    acct = args.get("account", "?")
    if tool.endswith("calendar_rsvp"):
        return (f"RSVP '{args.get('response', '?')}' from Google account '{acct}' to "
                f"'{args.get('title') or args.get('event_id', '?')}' (the organizer is notified)")
    if tool.endswith("calendar_create_event") and str(args.get("guests") or "").strip():
        return (f"Create '{args.get('title', '?')}' at {args.get('start', '?')} on '{acct}' and invite "
                f"{args.get('guests')} (Google emails them)")
    return None


def _on_pre_tool_call(tool_name="", args=None, **_kw):
    args = args if isinstance(args, dict) else {}
    tool = str(tool_name)
    if tool.endswith(SEND_TOOL):
        return {"action": "approve", "message": approval_message(args),
                "rule_key": f"gmail_send:{uuid.uuid4().hex}"}
    msg = calendar_message(tool, args)
    if msg:
        return {"action": "approve", "message": msg, "rule_key": f"calendar_notify:{uuid.uuid4().hex}"}
    return None


def register(ctx):
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
