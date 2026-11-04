"""Every touch of Hermes gateway internals lives here, so an upstream change breaks one small
file (and the fake in tests mirrors exactly this surface). Nothing here creates a session."""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import time
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

    async def session_meta(self, session_id: str) -> Tuple[Optional[str], Optional[float]]:
        """(title, started_at epoch seconds) of a session, from Hermes's state DB."""
        row = self.gw._session_db.get_session(session_id)
        if inspect.isawaitable(row):
            row = await row
        row = row or {}
        return row.get("title"), row.get("started_at")

    async def discord_channel(self, source) -> Tuple[Any, int]:
        """(discord.py channel, bot user id) for a Discord source."""
        client = self.gw._delivery_adapter_for(source)._client
        channel = client.get_channel(int(source.chat_id)) or await client.fetch_channel(int(source.chat_id))
        return channel, client.user.id

    async def reply_anchor_ids(self, source, message_id: str, lookback: int) -> List[str]:
        """For a reply chat: ids that tell which conversation a Discord message belongs to, best first.
        A Hermes message: the message it replies to, then the messages before it (newest first)."""
        channel, bot_id = await self.discord_channel(source)
        msg = await channel.fetch_message(int(message_id))
        ids = []
        ref = getattr(getattr(msg, "reference", None), "message_id", None)
        if ref:
            ids.append(str(ref))
        if getattr(msg.author, "id", None) == bot_id:
            async for earlier in channel.history(limit=lookback, before=msg):
                ids.append(str(earlier.id))
                ref = getattr(getattr(earlier, "reference", None), "message_id", None)
                if ref and getattr(earlier.author, "id", None) == bot_id:
                    ids.append(str(ref))
        return ids

    async def replay_discord(self, source, message_ids: List[str]) -> int:
        """Re-dispatch Discord messages by id through core's missed-message recovery path (no dedupe
        claim), in order. Used for messages that were held when the gateway restarted."""
        adapter = self.gw._delivery_adapter_for(source)
        channel, _bot_id = await self.discord_channel(source)
        sent = 0
        for mid in message_ids:
            message = await channel.fetch_message(int(mid))
            if await adapter._dispatch_recovered_message(message):
                sent += 1
        return sent

    def agent_question_pending(self, source) -> bool:
        """True while the agent itself is waiting on a clarify question in this session. A typed
        message is then the answer to that question and must reach core untouched; holding it
        behind the topic check would deadlock (the waiting turn never finishes, so held messages
        queue behind it forever)."""
        from tools import clarify_gateway
        return clarify_gateway.has_pending(self.session_key(source))

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

    # --- act-then-check undo (the private gateway calls /stop, /resume and /undo make).
    def busy(self, key: str) -> bool:
        """True while Hermes is still working on a turn in this chat."""
        return self.gw._running_agents.get(key) is not None

    def current_session_id(self, source) -> Optional[str]:
        entry = self.store.lookup_by_session_key(self.session_key(source))
        return entry.session_id if entry else None

    async def stop_turn(self, key: str, source, settle_seconds: float = 15.0) -> None:
        """Interrupt the running turn (as /stop does) and wait until its transcript stops changing:
        the worker thread can still write for a moment after the interrupt returns."""
        from gateway.run import _INTERRUPT_REASON_STOP
        if self.gw._running_agents.get(key) is not None:
            await self.gw._interrupt_and_clear_session(key, source, interrupt_reason=_INTERRUPT_REASON_STOP,
                                                       invalidation_reason="topic_router_undo")
        deadline, last = time.monotonic() + settle_seconds, None
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            size = len(await self.transcript(source))
            if size == last and self.gw._running_agents.get(key) is None:
                return
            last = size

    async def switch_back(self, key: str, old_sid: str, new_sid: str) -> bool:
        """Point the chat back at its previous session (as /resume does), only if it is still on new_sid."""
        st = self.gw.async_session_store
        self.gw._release_running_agent_state(key)
        entry = await st.switch_session(key, old_sid, expected_session_id=new_sid, preserve_prompt_pin=False)
        if not entry:
            return False
        self.gw._clear_conversation_scope(key, reason="resume")
        await st.set_model_override(key, None)
        self.gw._evict_cached_agent(key)
        return True

    async def discard_session(self, session_id: str) -> None:
        """Drop a session the router opened by mistake (routes first, so nothing re-creates it)."""
        await asyncio.to_thread(self.store.remove_by_session_id, session_id)
        result = self.gw._session_db.delete_session(session_id)
        if inspect.isawaitable(result):
            await result

    async def rewind_last(self, key: str, session_id: str, turns: int = 1) -> Optional[str]:
        """Remove the last ``turns`` user turns and everything after them (as /undo N does); returns
        the oldest removed turn's text."""
        result = await self.gw.async_session_store.rewind_session(session_id, turns)
        if not result:
            return None
        entry = self.store.lookup_by_session_key(key)
        if entry is not None:
            entry.last_prompt_tokens = 0
        self.gw._evict_cached_agent(key)
        return result.get("target_text") or ""

    async def delete_bot_messages_after(self, source, after_id) -> int:
        """Delete the bot's own Discord messages after a message (divider, partial replies, the check prompt)."""
        import discord
        channel, bot_id = await self.discord_channel(source)
        n = 0
        async for msg in channel.history(limit=100, after=discord.Object(id=int(after_id)), oldest_first=True):
            if msg.author.id == bot_id:
                try:
                    await msg.delete()
                    n += 1
                except Exception:
                    pass
        return n
