"""Every touch of Hermes gateway internals lives here, so an upstream change breaks one small
file (and the fake in tests mirrors exactly this surface). Nothing here creates a session."""
from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, List, Optional, Tuple


def _thread_meta(source) -> Optional[dict]:
    thread_id = getattr(source, "thread_id", None)
    return {"thread_id": thread_id} if thread_id else None


class GatewayBridge:
    def __init__(self, gateway: Any, session_store: Any = None):
        self.gw = gateway
        self.store = session_store or getattr(gateway, "session_store", None)

    def session_key(self, source) -> str:
        return self.gw._session_key_for_source(source)

    def authorized(self, source) -> bool:
        check = getattr(self.gw, "_is_user_authorized_for_source", None)
        return bool(check(source)) if callable(check) else True

    async def transcript(self, source) -> List[dict]:
        """Current session messages, read-only; [] when there is no session yet."""
        entry = self.store.lookup_by_session_key(self.session_key(source))
        if entry is None:
            return []
        return await asyncio.to_thread(self.store.load_transcript, entry.session_id)

    def origin_of(self, session_id: str) -> Tuple[Optional[str], Any]:
        entry = self.store.lookup_by_session_id(session_id) if self.store else None
        return (entry.session_key, entry.origin) if entry else (None, None)

    async def reset(self, event) -> None:
        """Exactly what /new runs (minus its confirm prompt and banner)."""
        await self.gw._handle_reset_command(dataclasses.replace(event, text="/new"))

    async def send(self, source, text: str) -> bool:
        adapter = self.gw._delivery_adapter_for(source)
        result = await adapter.send(source.chat_id, text, metadata=_thread_meta(source))
        return bool(getattr(result, "success", True))

    # --- choice prompt: reuse the clarify registry so the platform's own buttons resolve it.
    def register_choice(self, prompt_id: str, owner: str, question: str, choices: List[str]):
        from tools import clarify_gateway
        return clarify_gateway.register(prompt_id, owner, question, list(choices))

    def forget_choice(self, owner: str) -> None:
        from tools import clarify_gateway
        clarify_gateway.clear_session(owner)

    async def send_choice(self, source, question: str, choices: List[str], prompt_id: str, owner: str) -> bool:
        adapter = self.gw._delivery_adapter_for(source)
        result = await adapter.send_clarify(chat_id=source.chat_id, question=question, choices=list(choices),
                                            clarify_id=prompt_id, session_key=owner, metadata=_thread_meta(source))
        return bool(getattr(result, "success", False))

    async def release(self, key: str, events: list) -> None:
        """Feed held events back into the normal pipeline in order. If the session slot is busy
        (we are inside another message's dispatch) they join its FIFO; otherwise the first one
        starts a turn and the rest queue behind it."""
        if not events:
            return
        adapter = self.gw._intake_adapter_for(events[0].source)
        rest = events
        if key not in getattr(adapter, "_active_sessions", {}):
            await adapter.handle_message(events[0])
            rest = events[1:]
        for event in rest:
            self.gw._queue_or_replace_pending_event(key, event)
