"""Topic router: decide continue / new / ask for each new message in an enabled chat."""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .bridge import GatewayBridge

logger = logging.getLogger("hermes_plugins.topic_router")

DIVIDER = "=========== NEW SESSION ==========="
QUESTION = "Continue the current conversation, or start a new session?"
CHOICES = ("Continue", "New session", "Cancel")
HINT = "\n(You can also reply `continue`, `new` or `cancel` at any time.)"
SKIP = {"action": "skip", "reason": "topic-router"}
_ANSWERS = {"continue": "continue", "1": "continue", "new": "new", "new session": "new", "2": "new",
            "cancel": "cancel", "3": "cancel"}


def parse_answer(text: Any) -> Optional[str]:
    t = str(text or "").strip().lower().rstrip(".!")
    t = t.split(". ", 1)[1] if t[:2] in ("1.", "2.", "3.") and ". " in t else t
    return _ANSWERS.get(t)


def conversation(transcript: List[dict]) -> List[dict]:
    """User/assistant text turns, in full (no cap). Tool rows and empty tool-call stubs drop out."""
    out = []
    for msg in transcript or []:
        role, content = msg.get("role"), msg.get("content")
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") if p.get("type") == "text" else f"[{p.get('type', 'attachment')}]"
                                for p in content if isinstance(p, dict))
        if isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content})
    return out


@dataclass
class Pending:
    prompt_id: str
    owner: str  # synthetic clarify key, never a real session key
    source: Any
    choice: Any  # clarify entry (has .event / .response)
    held: List[Any] = field(default_factory=list)


class Router:
    def __init__(self, decider: Any, channels: Dict[str, List[str]], *, decide_timeout: float = 120.0,
                 spawn: Optional[Callable] = None, bridge_factory: Callable = GatewayBridge,
                 poll_seconds: float = 0.5):
        self.decider = decider
        self.channels = {str(p).lower(): {str(c) for c in (ids or [])} for p, ids in (channels or {}).items()}
        self.decide_timeout = decide_timeout
        self.spawn = spawn or asyncio.ensure_future
        self.bridge_factory = bridge_factory
        self.poll_seconds = poll_seconds
        self.bridge: Optional[GatewayBridge] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.pending: Dict[str, Pending] = {}
        self.bypass: set = set()
        self.quiet_resets: set = set()
        self.locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ helpers
    def enabled(self, source) -> bool:
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
        return str(getattr(source, "chat_id", "")) in self.channels.get(str(platform or "").lower(), ())

    @staticmethod
    def _token(event):
        return (getattr(event.source, "chat_id", None), event.message_id or id(event))

    @staticmethod
    def _scope(source) -> str:
        platform = getattr(getattr(source, "platform", None), "value", source.platform)
        return f"{platform}:{source.chat_id}"

    async def _decide(self, source, history: List[dict], text: str) -> Optional[bool]:
        started, error, verdict = time.monotonic(), "", None
        try:
            result = self.decider.decide(history, text)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, self.decide_timeout)
            verdict = result if result is True or result is False else None
        except Exception as exc:  # error / timeout = unsure, never a guess
            error = type(exc).__name__
        logger.info("topic_router decision scope=%s verdict=%s latency_ms=%d decider=%s history_msgs=%d error=%s",
                    self._scope(source), {True: "continue", False: "new", None: "unsure"}[verdict],
                    (time.monotonic() - started) * 1000, type(self.decider).__name__, len(history), error or "-")
        return verdict

    async def _start_new(self, source, event) -> bool:
        key = self.bridge.session_key(source)
        self.quiet_resets.add(key)
        try:
            await self.bridge.reset(event)
        except Exception:
            logger.warning("topic_router reset failed scope=%s", self._scope(source), exc_info=True)
            return False
        finally:
            self.quiet_resets.discard(key)
        await self.bridge.send(source, DIVIDER)
        return True

    async def _ask(self, key: str, source, event) -> None:
        prompt_id = f"topic-router-{uuid.uuid4().hex[:12]}"
        owner = f"topic-router:{key}"
        choice = self.bridge.register_choice(prompt_id, owner, QUESTION, list(CHOICES))
        pending = self.pending[key] = Pending(prompt_id, owner, source, choice, [event])
        if not await self.bridge.send_choice(source, QUESTION + HINT, list(CHOICES), prompt_id, owner):
            await self.bridge.send(source, QUESTION + HINT)
        self.spawn(self._watch(key, pending))

    async def _watch(self, key: str, pending: Pending) -> None:
        """Wait (forever) for a button press on this prompt."""
        while self.pending.get(key) is pending:
            if pending.choice.event.is_set():
                answer = parse_answer(pending.choice.response)
                if answer:
                    async with self.locks.setdefault(key, asyncio.Lock()):
                        await self.resolve(key, answer, via="button")
                return  # anything else: typed replies still work
            await asyncio.sleep(self.poll_seconds)

    async def resolve(self, key: str, answer: str, via: str) -> None:
        pending = self.pending.pop(key, None)
        if pending is None:
            return  # already answered (first answer wins)
        self.bridge.forget_choice(pending.owner)
        logger.info("topic_router answer scope=%s choice=%s via=%s held=%d",
                    self._scope(pending.source), answer, via, len(pending.held))
        first, rest = pending.held[0], pending.held[1:]
        if answer == "cancel":
            await self.bridge.send(pending.source, "Okay, I dropped that message.")
            release = rest  # later messages get routed normally
        else:
            if answer == "new" and not await self._start_new(pending.source, first):
                await self.bridge.send(pending.source, "Couldn't start a new session; continuing the current one.")
            self.bypass.add(self._token(first))
            release = [first] + rest
        await self.bridge.release(key, release)

    # ------------------------------------------------------------------ hooks
    async def on_dispatch(self, event=None, gateway=None, session_store=None, **_kwargs):
        source = getattr(event, "source", None)
        if (event is None or gateway is None or source is None or getattr(event, "internal", False)
                or getattr(source, "is_bot", False) or not self.enabled(source)):
            return None
        if self.bridge is None or self.bridge.gw is not gateway:
            self.bridge = self.bridge_factory(gateway, session_store)
        self.loop = asyncio.get_running_loop()
        if not self.bridge.authorized(source):
            return None  # core rejects it; never spend a decision on it
        token = self._token(event)
        if token in self.bypass:
            self.bypass.discard(token)
            return {"action": "allow"}
        command = event.get_command()
        if command == "clear":  # CLI-only in core; make it the real /new (divider via on_reset)
            return {"action": "rewrite", "text": ("/new " + event.get_command_args()).strip()}
        if command:
            return None  # /new and /reset post the divider from on_reset after they really reset
        text = (event.text or "").strip()
        if not text or text == DIVIDER:
            return None
        key = self.bridge.session_key(source)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            pending = self.pending.get(key)
            if pending is not None:
                answer = parse_answer(text)
                if answer:
                    await self.resolve(key, answer, via="text")
                else:
                    pending.held.append(event)  # keep order; routed after the answer
                return SKIP
            try:
                history = conversation(await self.bridge.transcript(source))
            except Exception:
                logger.warning("topic_router transcript read failed scope=%s", self._scope(source), exc_info=True)
                history = None
            if history == []:
                return None  # first message of a session: nothing to compare against
            verdict = None if history is None else await self._decide(source, history, text)
            if verdict is True:
                return None
            if verdict is False and await self._start_new(source, event):
                return None
            await self._ask(key, source, event)
            return SKIP

    def on_reset(self, session_id=None, **_kwargs):
        """Divider for /new (and /clear) in an enabled chat, posted once the reset really happened."""
        if self.bridge is None or self.loop is None or not session_id:
            return
        try:
            key, origin = self.bridge.origin_of(session_id)
        except Exception:
            return
        if origin is None or key in self.quiet_resets or not self.enabled(origin):
            return
        asyncio.run_coroutine_threadsafe(self.bridge.send(origin, DIVIDER), self.loop)
