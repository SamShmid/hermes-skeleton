"""approval-expiry-notice: when Discord approval buttons expire but Hermes is still waiting,
post a fresh message telling the user how to answer by typing. Also: when a new message arrives
while Hermes is blocked on an approval, say it was queued (core queues it as the next turn) and
what is still waiting, so nobody is left in the dark.

Discord kills button views after at most 15 minutes, while the approval itself waits much
longer (approvals.timeout). The expired embed edit doesn't notify anyone, so this sends a
new message via the Discord REST API once the buttons are gone and the approval is still open.
"""
import json
import logging
import os
import threading
import urllib.request

logger = logging.getLogger("plugins.approval_expiry_notice")

_DEFAULT_BUTTON_TIMEOUT = 300
_timers: dict = {}
_queued: dict = {}  # session key -> messages queued behind the open approval
_lock = threading.Lock()


def _button_timeout() -> int:
    try:
        from hermes_cli.config import read_raw_config
        raw = ((read_raw_config() or {}).get("approvals") or {}).get("discord_prompt_timeout")
        return max(30, min(900, int(raw))) if raw not in (None, "") else _DEFAULT_BUTTON_TIMEOUT
    except Exception:
        return _DEFAULT_BUTTON_TIMEOUT


def discord_channel(session_key: str):
    """Channel id from a gateway session key like agent:main:discord:group:<chan>:<user>."""
    parts = (session_key or "").split(":")
    if "discord" not in parts:
        return None
    rest = parts[parts.index("discord") + 1:]
    if len(rest) >= 2 and rest[1].isdigit():
        return rest[1]
    return None


def notice_text(command: str) -> str:
    cmd = " ".join((command or "").split())
    if len(cmd) > 120:
        cmd = cmd[:117] + "..."
    return (
        "⏳ The approval buttons expired, but I'm still waiting and nothing has run yet.\n"
        f"Command: `{cmd}`\n"
        "Reply with **approve** (once), **session**, **always**, or **deny**. 👍 / 👎 also work."
    )


def queued_text(command: str, n: int) -> str:
    cmd = " ".join((command or "").split())
    if len(cmd) > 120:
        cmd = cmd[:117] + "..."
    waiting = f"\nWaiting on: `{cmd}`" if cmd else ""
    return (
        f"📥 Queued your message ({n} waiting). I'm still waiting on your OK for a command before I can "
        f"continue.{waiting}\n"
        "Reply **approve**, **session** or **deny** (or tap the buttons above); your message runs right after."
    )


async def _on_dispatch(event=None, gateway=None, **_kw):
    """Announce (never change) a message that core will queue behind an open approval."""
    source = getattr(event, "source", None)
    if (event is None or gateway is None or source is None or getattr(event, "internal", False)
            or getattr(source, "is_bot", False)):
        return None
    try:
        from tools.approval import get_pending_gateway_approval, has_blocking_approval
        key = gateway._session_key_for_source(source)
        text = (event.text or "").strip()
        if not text or event.get_command() or not has_blocking_approval(key):
            return None
        if not gateway._is_user_authorized_for_source(source):
            return None
        if text.lower() in gateway._plaintext_approval_words():
            return None  # that is the answer itself
        pending = get_pending_gateway_approval(key) or {}
        with _lock:
            n = _queued[key] = _queued.get(key, 0) + 1
        adapter = gateway._delivery_adapter_for(source)
        await adapter.send(str(source.chat_id), queued_text(pending.get("command", ""), n),
                           metadata=gateway._thread_metadata_for_source(source))
        logger.info("approval-expiry-notice: queued notice session=%s n=%d", key, n)
    except Exception as exc:
        logger.warning("approval-expiry-notice: queued notice failed: %s", exc)
    return None


def _still_pending(session_key: str) -> bool:
    try:
        from tools.approval import has_blocking_approval
        return bool(has_blocking_approval(session_key))
    except Exception:
        return False


def _post(channel_id: str, text: str) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("approval-expiry-notice: DISCORD_BOT_TOKEN not set; cannot post notice")
        return
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode(),
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "hermes-approval-expiry-notice (local, 0.1)"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15):
        pass


def _fire(session_key: str, channel_id: str, command: str) -> None:
    with _lock:
        _timers.pop(session_key, None)
    if not _still_pending(session_key):
        return
    try:
        _post(channel_id, notice_text(command))
        logger.info("approval-expiry-notice: posted typed-answer notice channel=%s", channel_id)
    except Exception as exc:
        logger.warning("approval-expiry-notice: failed to post notice: %s", exc)


def _on_request(session_key="", command="", **_kw):
    channel_id = discord_channel(session_key)
    if not channel_id:
        return None
    timer = threading.Timer(_button_timeout() + 5, _fire, args=(session_key, channel_id, command))
    timer.daemon = True
    with _lock:
        old = _timers.pop(session_key, None)
        _timers[session_key] = timer
    if old is not None:
        old.cancel()
    timer.start()
    return None


def _on_response(session_key="", **_kw):
    with _lock:
        _queued.pop(session_key, None)
        timer = _timers.pop(session_key, None)
    if timer is not None:
        timer.cancel()
    return None


def register(ctx):
    ctx.register_hook("pre_approval_request", _on_request)
    ctx.register_hook("post_approval_response", _on_response)
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
