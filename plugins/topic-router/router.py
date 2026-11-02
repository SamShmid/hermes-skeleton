"""Topic router: decide continue / new / ask for each new message in an enabled chat."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .bridge import GatewayBridge
from .threadify import Threadifier

logger = logging.getLogger("hermes_plugins.topic_router")

DIVIDER = "=========== NEW SESSION ==========="
QUESTION = "Continue the current conversation, or start a new session?"
CHOICES = ("Continue", "New session", "Cancel")
HINT = "\n(You can also reply `continue`, `new` or `cancel` at any time.)"
SKIP = {"action": "skip", "reason": "topic-router"}
_ANSWERS = {"continue": "continue", "1": "continue", "new": "new", "new session": "new", "2": "new",
            "cancel": "cancel", "3": "cancel",
            # confirm mode (yes/no): "yes" = same topic, "no" = new topic
            "yes": "continue", "y": "continue", "same": "continue", "yes, same topic": "continue",
            "no": "new", "n": "new", "no, new topic": "new",
            "same chat": "continue", "new chat": "new"}
# Asked BEFORE Hermes replies (Oct 27: the owner wants no reply at all until they pick, whenever the model leans new).
ASK_QUESTION = "Same chat or new chat?"
CONFIRM_CHOICES = ("Same chat", "New chat", "Cancel")
ASK_HINT = "\nHermes waits for your answer. (You can also reply `same`, `new` or `cancel`.)"
CONFIRM_GUESS = {True: "same topic", False: "new topic", None: "not sure"}
LABEL_CONTEXT_TURNS = 6
LABEL_CONTEXT_CHARS = 1500
DROPPED = "Okay, I dropped that message."
NO_RESET = "Couldn't start a new session; continuing the current one."
# act-then-check mode: the router acts on a confident verdict, then asks if it was right; "No" undoes it.
CHECK_CHOICES = ("Same chat", "New chat")
_CHECK_ANSWERS = {"same chat": True, "yes": True, "new chat": False, "no, undo": False, "no": False, "undo": False}
UNDO_SAME = "↩️ Undone: back in the same conversation."
UNDO_NEW = "↩️ Undone: moving that message to a new conversation."
UNDO_FAILED = "↩️ Couldn't undo"
# Waiting on the owner: never hold messages silently, never forever, never only in memory.
ANSWER_TIMEOUT = 600.0
QUEUED = ("📥 Queued your message ({n} waiting). I'm still waiting on your answer above: tap a button or "
          "reply `yes` / `no` / `cancel`. If you don't answer, in about {mins} min I'll go with my guess "
          "and send them all through.")
TIMED_OUT = "⏱️ No answer in {mins} min, so I went with {choice} and sent your {what} through."
RESTORED = "♻️ Hermes restarted while I was holding {n} of your messages. Sending them through now, in order."
QUEUED_WAIT = ("📥 Queued your message ({n} waiting). Waiting for your answer above: tap Same chat, "
               "New chat or Cancel.")
# Quick follow-ups (within FOLLOWUP_SECONDS of the previous message, or while Hermes is still working on
# this chat) stay in the same conversation unless the model is at least STRONG_NEW sure it's a new topic.
FOLLOWUP_SECONDS = 300.0  # was 120; replaying the owner's 94 answers: 5 min fixes 3 wrong splits, costs 1
STRONG_NEW = 0.95
# Model not sure + the owner quiet this long = new topic, unless the message points back ("this", "that", "it", ...).
# Replay of the owner's answers: the old "go with the lean" got 3 of 7 such cases wrong; this rule got them all right.
LONG_GAP_SECONDS = 2 * 3600.0
_BACK_REF = re.compile(r"\b(this|that|it|these|those|here|there|again|still|instead|same|also|then)\b", re.I)
# A finished chat whose threadify failed is retried (oldest first) with each later message in the chat.
THREADIFY_MAX_TRIES = 5
# The "right?" check (and its undo) stays open this long after the last message it covers.
CHECK_WINDOW = 300.0
# Reply chats (plugin setting reply_channels): conversations live in Discord replies instead of threads and
# dividers; plain messages get the same act-then-check routing as other chats (Oct 21: the owner wants the checks
# back). Replying to an earlier message reopens that message's conversation. A reply to a later chunk of a Hermes
# answer (no reply anchor of its own) is matched by looking back this many messages.
REPLY_LOOKBACK = 30
RESTART_HELD = ("♻️ Hermes restarted while I was holding {n} of your messages for the topic question. "
                "They're saved: send anything here and I'll pass them through first, in order.")


# Status lines that mean nothing once a chat is over (progress pings, restarts, approval prompts, tool lines).
BOT_CLUTTER = ("⏳ Working —", "⏳ The approval buttons expired", "⚠️ Hermes is shutting down",
               "⚠️ **Hermes wants to run a command", "✅ Command approved", "🗜️ Compressed:", "ℹ ",
               "📚 Reading skill", "💻 terminal", "⚙️ ", "🔀 Delegating", "📥 Queued your message",
               "⏱️ No answer in", "♻️ Hermes restarted")
OWNER_CLUTTER = ("approve", "deny", "approve session", "always approve")


def router_noise(msg: Any, mine: bool) -> bool:
    """The router's own clutter (and the owner's typed answers to it); dropped when a chat is threaded."""
    text = (getattr(msg, "content", "") or "").strip()
    if mine:
        return (text == DIVIDER or "Same topic?" in text or QUESTION in text
                or text.startswith((ASK_QUESTION, "Same chat?", "New chat?"))
                or "Already started; tap" in text  # act-then-check prompt
                or text.startswith(DROPPED) or text.startswith(NO_RESET) or text.startswith("↩️ ")
                or text.startswith(("✨ Session reset!", "✨ New session started!"))  # /new banner
                or text.startswith(BOT_CLUTTER))
    return (parse_answer(text) is not None or text.lower() in OWNER_CLUTTER
            or text.split(" ", 1)[0].lower() in ("/new", "/reset", "/clear", "/approve", "/deny"))


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
    label: Optional[dict] = None  # confirm mode: what to log once the owner answers
    guess: Optional[bool] = None  # decider verdict; what a timeout goes with (unsure = same topic)
    asked_at: float = field(default_factory=time.time)
    wait: bool = False  # the model failed: no timeout, wait for the owner (no guessing, no backup model)


@dataclass
class Check:
    """act-then-check: a confident verdict already acted on, waiting for the owner's yes / no."""
    prompt_id: str
    owner: str
    source: Any
    event: Any
    choice: Any
    verdict: bool  # True = kept in the same session, False = started a new one
    old_sid: Optional[str]
    new_sid: Optional[str]
    label: dict
    followers: List[Any] = field(default_factory=list)  # quick follow-ups that went where this one went
    last_at: float = field(default_factory=time.time)  # the window runs from the latest of them


class Router:
    def __init__(self, decider: Any, channels: Dict[str, List[str]], *, decide_timeout: float = 120.0,
                 spawn: Optional[Callable] = None, bridge_factory: Callable = GatewayBridge,
                 poll_seconds: float = 0.5, confirm_all: bool = False, label_log: Optional[str] = None,
                 threadify: Optional[Dict[str, List[str]]] = None, owner_label: str = "Owner",
                 threadify_state: Optional[str] = None, act_confirm: bool = False,
                 answer_timeout: float = ANSWER_TIMEOUT, held_state: Optional[str] = None,
                 reply: Optional[Dict[str, List[str]]] = None, reply_state: Optional[str] = None):
        self.decider = decider
        # confirm_all: ask the owner yes/no on EVERY message (the decider's verdict is shown as a guess) and log
        # (context, message, verdict, answer) to label_log as training/eval data.
        self.confirm_all = bool(confirm_all)
        # act_confirm: confident verdicts are acted on at once (Hermes starts working) and the owner gets a
        # non-blocking "right?" with an undo; unsure ones still ask first. Answers are logged as labels.
        self.act_confirm = bool(act_confirm)
        self.checks: Dict[str, Check] = {}
        self._last_info, self._last_error = None, ""
        self.label_log = os.path.expanduser(label_log) if label_log else None
        self._last_latency_ms = 0
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
        # threadify: chats where a finished conversation is moved into a Discord thread on reset.
        self.threadify_channels = {str(p).lower(): {str(c) for c in (ids or [])} for p, ids in (threadify or {}).items()}
        self.threadifier = Threadifier(router_noise, owner_label)
        self.reset_trigger: Dict[str, int] = {}  # session key -> id of the message that ended the chat
        # chat scope -> id of the message that began the current conversation (= the last trigger);
        # persisted so a restart doesn't lose where the next thread starts.
        self.threadify_state = os.path.expanduser(threadify_state) if threadify_state else None
        # jobs: finished chats waiting to be threaded (kept until threadify succeeds, so a crash can't lose
        # them); deferred: chats a "New topic?" check ended, waiting on the owner's yes / the window (a restart
        # turns these into jobs); last_msg_at: when the owner last wrote in each chat (survives restarts).
        self.jobs: List[dict] = []
        self.deferred: Dict[str, dict] = {}
        self.last_msg_at: Dict[str, float] = {}
        self.starts: Dict[str, int] = self._load_starts()
        self._job_locks: Dict[str, asyncio.Lock] = {}
        # Messages held behind an unanswered question, persisted so a restart can't lose them; whatever a
        # previous run left behind is replayed from Discord with the next message in that chat.
        self.answer_timeout = float(answer_timeout)
        self.held_state = os.path.expanduser(held_state) if held_state else None
        self.leftovers: Dict[str, dict] = self._load_held()
        self.replay_ids: set = set()
        # reply chats: message id -> session id, so a reply can reopen the conversation it points at.
        # "open" holds this chat's newest messages until we know their session (settled on the next
        # message, or with the old session id when the chat resets).
        self.reply_channels = {str(p).lower(): {str(c) for c in (ids or [])} for p, ids in (reply or {}).items()}
        self.reply_state = os.path.expanduser(reply_state) if reply_state else None
        self.msg_sessions, self.open_msgs = self._load_replies()
        self.reply_trigger: Dict[str, str] = {}  # session key -> message that started the new conversation

    # ------------------------------------------------------------------ helpers
    def enabled(self, source) -> bool:
        return self._listed(self.channels, source)

    def reply_mode(self, source) -> bool:
        return self._listed(self.reply_channels, source) and not getattr(source, "thread_id", None)

    def threadify_enabled(self, source) -> bool:
        return self._listed(self.threadify_channels, source) and not getattr(source, "thread_id", None)

    @staticmethod
    def _listed(table, source) -> bool:
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
        return str(getattr(source, "chat_id", "")) in table.get(str(platform or "").lower(), ())

    def _mark_trigger(self, key: str, event) -> None:
        try:
            self.reset_trigger[key] = int(event.message_id)
        except (TypeError, ValueError):
            pass

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
        self._last_latency_ms = int((time.monotonic() - started) * 1000)
        info = getattr(self.decider, "last_info", None)
        self._last_info = dict(info) if isinstance(info, dict) and not error else None
        self._last_error = error
        logger.info("topic_router decision scope=%s verdict=%s latency_ms=%d decider=%s history_msgs=%d error=%s",
                    self._scope(source), {True: "continue", False: "new", None: "unsure"}[verdict],
                    (time.monotonic() - started) * 1000, type(self.decider).__name__, len(history), error or "-")
        return verdict

    async def _start_new(self, source, event, threadify: bool = True, divider: bool = True) -> bool:
        key = self.bridge.session_key(source)
        self.quiet_resets.add(key)
        if threadify and self.threadify_enabled(source):
            self._mark_trigger(key, event)
        if self.reply_mode(source):  # the message that starts the new chat belongs to it, not the old one
            self.reply_trigger[key] = str(event.message_id)
            divider = False
        try:
            await self.bridge.reset(event)
        except Exception:
            logger.warning("topic_router reset failed scope=%s", self._scope(source), exc_info=True)
            self.reply_trigger.pop(key, None)
            return False
        finally:
            self.quiet_resets.discard(key)
        if divider:
            await self.bridge.send(source, DIVIDER)
        return True

    async def _ask(self, key: str, source, event, label: Optional[dict] = None,
                   guess: Optional[bool] = None, wait: bool = False) -> None:
        prompt_id = f"topic-router-{uuid.uuid4().hex[:12]}"
        owner = f"topic-router:{key}"
        if label is not None:
            question = f"{ASK_QUESTION} ({self._guess_line(label)})"
            choices, hint = list(CONFIRM_CHOICES), ASK_HINT
        else:
            question, choices, hint = QUESTION, list(CHOICES), HINT
        choice = self.bridge.register_choice(prompt_id, owner, question, choices)
        pending = self.pending[key] = Pending(prompt_id, owner, source, choice, [event], label, guess, wait=wait)
        self._save_held()
        if not await self.bridge.send_choice(source, question + hint, choices, prompt_id, owner):
            await self.bridge.send(source, question + hint)
        self.spawn(self._watch(key, pending))

    def _write_label(self, pending: Any, answer: str, via: str) -> None:
        if not self.label_log or pending.label is None or via == "timeout":  # only the owner's answers are labels
            return
        rec = dict(pending.label, answer=answer, via=via, answered_at=time.time())
        try:
            os.makedirs(os.path.dirname(self.label_log), exist_ok=True)
            with open(self.label_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("topic_router could not write label log", exc_info=True)

    def _label(self, source, history: List[dict], text: str, verdict: Optional[bool]) -> dict:
        ctx = [{"role": m["role"], "content": m["content"][:LABEL_CONTEXT_CHARS]}
               for m in history[-LABEL_CONTEXT_TURNS:]]
        return {"scope": self._scope(source), "asked_at": time.time(), "message": text, "context": ctx,
                "verdict": {True: "continue", False: "new", None: "unsure"}[verdict],
                "decider": type(self.decider).__name__, "latency_ms": self._last_latency_ms,
                "p_new": (self._last_info or {}).get("p_new"), "error": self._last_error or None}

    @staticmethod
    def _guess_line(label: dict) -> str:
        """Confirm prompt: what the decider said, how sure, and how long it took."""
        took = f"took {label.get('latency_ms', 0) / 1000:.2f} s"
        if label.get("error"):
            return f"model failed ({label['error']}), {took}"
        guess = CONFIRM_GUESS[{'continue': True, 'new': False}.get(label['verdict'])]
        p = label.get("p_new")
        if p is None:
            return f"my guess: {guess}, {took}"
        if label["verdict"] == "unsure":
            lean = "new topic" if p >= 0.5 else "same topic"
            rule = f", {label['rule']}" if label.get("rule") else ""
            return f"model: not sure, leans {lean} {max(p, 1 - p):.0%}{rule}, {took}"
        return f"model: {guess}, {max(p, 1 - p):.0%} sure, {took}"

    async def _watch(self, key: str, pending: Pending) -> None:
        """Wait for a button press on this prompt; after answer_timeout go with the guess."""
        while self.pending.get(key) is pending:
            if not pending.wait and time.time() - pending.asked_at >= self.answer_timeout:
                async with self.locks.setdefault(key, asyncio.Lock()):
                    if self.pending.get(key) is pending:
                        answer = "new" if pending.guess is False else "continue"
                        n = len(pending.held)
                        await self.bridge.send(pending.source, TIMED_OUT.format(
                            mins=round(self.answer_timeout / 60),
                            choice="a new topic" if answer == "new" else "the same topic",
                            what="message" if n == 1 else f"{n} messages"))
                        await self.resolve(key, answer, via="timeout")
                return
            if pending.choice.event.is_set():
                answer = parse_answer(pending.choice.response)
                if answer:
                    async with self.locks.setdefault(key, asyncio.Lock()):
                        await self.resolve(key, answer, via="button")
                    return
                pending.choice.event.clear()  # "Other" etc.: typed replies still work; keep the timeout
            await asyncio.sleep(self.poll_seconds)

    async def resolve(self, key: str, answer: str, via: str) -> None:
        pending = self.pending.pop(key, None)
        if pending is None:
            return  # already answered (first answer wins)
        self._save_held()
        self.bridge.forget_choice(pending.owner)
        logger.info("topic_router answer scope=%s choice=%s via=%s held=%d",
                    self._scope(pending.source), answer, via, len(pending.held))
        self._write_label(pending, answer, via)
        first, rest = pending.held[0], pending.held[1:]
        if answer == "cancel":
            await self.bridge.send(pending.source, DROPPED)
            release = rest  # later messages get routed normally
        else:
            if answer == "new" and not await self._start_new(pending.source, first):
                await self.bridge.send(pending.source, NO_RESET)
            self.bypass.add(self._token(first))
            release = [first] + rest
        await self.bridge.release(key, release)

    # ------------------------------------------------------------------ act-then-check
    async def _act_and_check(self, key: str, source, event, label: dict, verdict: bool) -> bool:
        old_sid = self.bridge.current_session_id(source)
        new_sid = None
        if verdict is False:
            # No threadify yet: moving the old chat into a thread can't be undone, so it waits for a "yes".
            if not await self._start_new(source, event, threadify=False):
                return False
            new_sid = self.bridge.current_session_id(source)
        prompt_id = f"topic-router-check-{uuid.uuid4().hex[:12]}"
        owner = f"topic-router-check:{key}"
        what, other = ("Same chat", "New chat") if verdict else ("New chat", "Same chat")
        question = f"{what}? ({self._guess_line(label)}) Already started; tap {other} to undo."
        choice = self.bridge.register_choice(prompt_id, owner, question, list(CHECK_CHOICES))
        check = self.checks[key] = Check(prompt_id, owner, source, event, choice, verdict, old_sid, new_sid, label)
        if verdict is False:
            self._remember_deferred(check)
        if not await self.bridge.send_choice(source, question, list(CHECK_CHOICES), prompt_id, owner):
            await self.bridge.send(source, question)
        self.spawn(self._watch_check(key, check))
        return True

    async def _watch_check(self, key: str, check: Check) -> None:
        while self.checks.get(key) is check:
            if time.time() - check.last_at >= CHECK_WINDOW:
                async with self.locks.setdefault(key, asyncio.Lock()):
                    if self.checks.get(key) is check:
                        self._expire_check(key)
                return
            if check.choice.event.is_set():
                same = _CHECK_ANSWERS.get(str(check.choice.response or "").strip().lower())
                if same is not None:
                    async with self.locks.setdefault(key, asyncio.Lock()):
                        # "yes"/"no" are about the check itself; "Same chat"/"New chat" name where it belongs.
                        answer = str(check.choice.response).strip().lower()
                        right = same if answer in ("yes", "no", "no, undo", "undo") else (same == check.verdict)
                        await self.resolve_check(key, check, right=right)
                    return
                check.choice.event.clear()  # anything else: keep watching, so the window still closes
            await asyncio.sleep(self.poll_seconds)

    def _expire_check(self, key: str) -> None:
        """Unanswered check when its window closes or an unrelated message arrives: the action stands
        (no label is logged)."""
        check = self.checks.pop(key, None)
        if check is None:
            return
        self.bridge.forget_choice(check.owner)
        if check.verdict is False:
            self._deferred_threadify(check)

    def _remember_deferred(self, check: Check) -> None:
        """Persist the chat a "New topic?" check ended, so a restart before the owner's yes can't lose it."""
        if not (check.old_sid and self.threadify_enabled(check.source)):
            return
        try:
            self.deferred[self._scope(check.source)] = {"before": int(check.event.message_id), "sid": check.old_sid}
        except (TypeError, ValueError):
            return
        self._save_starts()

    def _forget_deferred(self, check: Check) -> None:
        if self.deferred.pop(self._scope(check.source), None) is not None:
            self._save_starts()

    def _deferred_threadify(self, check: Check) -> None:
        self._forget_deferred(check)
        if check.old_sid and self.threadify_enabled(check.source):
            try:
                trigger = int(check.event.message_id)
            except (TypeError, ValueError):
                return
            self.spawn(self._threadify(check.source, check.old_sid, trigger))

    async def resolve_check(self, key: str, check: Check, right: bool) -> None:
        if self.checks.get(key) is not check:
            return  # expired or already answered
        del self.checks[key]
        self.bridge.forget_choice(check.owner)
        actual = check.verdict if right else (not check.verdict)
        self._write_label(check, "continue" if actual else "new", via="check")
        logger.info("topic_router check scope=%s verdict=%s right=%s", self._scope(check.source),
                    "continue" if check.verdict else "new", right)
        if right:
            if check.verdict is False:
                self._deferred_threadify(check)
            return
        try:
            await self._undo(key, check)
        except Exception as exc:
            logger.warning("topic_router undo failed scope=%s", self._scope(check.source), exc_info=True)
            await self.bridge.send(check.source, f"{UNDO_FAILED} ({type(exc).__name__}). Use /undo or /new by hand.")

    async def _undo(self, key: str, check: Check) -> None:
        """Stopping the turn also drops anything queued behind it, so every covered message is re-sent."""
        source, event = check.source, check.event
        events = [event] + check.followers
        await self.bridge.stop_turn(key, source)
        if check.verdict is False:  # started a new session, but it was the same topic
            if not check.old_sid or not check.new_sid or not await self.bridge.switch_back(key, check.old_sid, check.new_sid):
                await self.bridge.send(source, f"{UNDO_FAILED}: the chat has moved on. Use /resume by hand.")
                self._deferred_threadify(check)  # the old chat really did end
                return
            self._forget_deferred(check)
            try:
                await self.bridge.discard_session(check.new_sid)
            except Exception:
                logger.warning("topic_router could not delete the mistaken session", exc_info=True)
            await self.bridge.delete_bot_messages_after(source, event.message_id)
            await self.bridge.send(source, UNDO_SAME)
        else:  # kept it in the same session, but it was a new topic
            if self.bridge.current_session_id(source) != check.old_sid:
                await self.bridge.send(source, f"{UNDO_FAILED}: the chat has moved on.")
                return
            history = conversation(await self.bridge.transcript(source))
            users = [m["content"] for m in history if m["role"] == "user"][-len(events):]
            text = (event.text or "").strip()
            turns = next((len(users) - i for i, u in enumerate(users) if text and text[:80] in u), 0)
            # Not in the transcript (or nothing to rewind) = the turn was stopped before it was saved. Still
            # move the message to a new conversation: giving up here left the owner stuck in the old chat (Oct 13).
            if turns and await self.bridge.rewind_last(key, check.old_sid, turns) is None:
                logger.warning("topic_router undo: rewind found nothing scope=%s turns=%d", self._scope(source), turns)
            await self.bridge.delete_bot_messages_after(source, event.message_id)
            await self.bridge.send(source, UNDO_NEW)
            if not await self._start_new(source, event):
                await self.bridge.send(source, NO_RESET)
        self.bypass.update(self._token(e) for e in events)
        await self.bridge.release(key, events)

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
        if self.bridge.agent_question_pending(source):
            return None  # the agent asked a question and is waiting: this message is the answer
        token = self._token(event)
        if token in self.bypass:
            self.bypass.discard(token)
            return {"action": "allow"}
        if str(event.message_id) in self.replay_ids:  # a held message replayed after a restart
            self.replay_ids.discard(str(event.message_id))
            return {"action": "allow"}
        command = event.get_command()
        if command == "clear":  # CLI-only in core; make it the real /new (divider via on_reset)
            return {"action": "rewrite", "text": ("/new " + event.get_command_args()).strip()}
        if command in ("new", "reset") and self.threadify_enabled(source):
            self._mark_trigger(self.bridge.session_key(source), event)
        if command:
            return None  # /new and /reset post the divider from on_reset after they really reset
        text = (event.text or "").strip()
        if not text or text == DIVIDER:
            return None
        key = self.bridge.session_key(source)
        leftover = self.leftovers.pop(key, None)
        if leftover and leftover.get("ids"):
            self._save_held()
            self.spawn(self._restore(key, source, leftover["ids"], event))
            return SKIP  # this message goes through right after the restored ones
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            pending = self.pending.get(key)
            if self.reply_mode(source):
                self._settle(key, self.bridge.current_session_id(source))
                self._remember(key, event)
                if pending is None and await self._reopen_replied(key, source, event):
                    return None
            if pending is not None:
                answer = parse_answer(text)
                if answer:
                    await self.resolve(key, answer, via="text")
                else:
                    pending.held.append(event)  # keep order; routed after the answer
                    self._save_held()
                    if pending.wait:
                        await self.bridge.send(source, QUEUED_WAIT.format(n=len(pending.held)))
                    else:
                        left = max(1, round((self.answer_timeout - (time.time() - pending.asked_at)) / 60))
                        await self.bridge.send(source, QUEUED.format(n=len(pending.held), mins=left))
                return SKIP
            try:
                history = conversation(await self.bridge.transcript(source))
            except Exception:
                logger.warning("topic_router transcript read failed scope=%s", self._scope(source), exc_info=True)
                history = None
            now, previous = time.time(), self.last_msg_at.get(key, 0.0)
            self.last_msg_at[key] = now
            self._save_starts()
            gap = now - previous if previous else None  # None = unknown (never seen this chat)
            if self._due_jobs(source):  # a finished chat still waiting for its thread (failure or restart)
                self.spawn(self._run_jobs(source))
            if history == []:  # first message of a session: nothing to compare against
                check = self.checks.get(key) if self.act_confirm else None
                if check is not None:  # the session the open check just started: same place, same undo
                    check.followers.append(event)
                    check.last_at = now
                return None
            if self.act_confirm and history is None:  # couldn't read the chat: ask and wait, no guessing
                self._expire_check(key)
                await self._ask(key, source, event, wait=True)
                return SKIP
            verdict = None if history is None else await self._decide(source, history, text)
            if self.act_confirm and history:
                p_new = (self._last_info or {}).get("p_new")
                quick = (gap is not None and gap <= FOLLOWUP_SECONDS) or self.bridge.busy(key)
                if quick and not self._last_error and not (verdict is False and (p_new or 0) >= STRONG_NEW):
                    check = self.checks.get(key)
                    if check is not None:  # rides along with the open check: one undo covers both
                        check.followers.append(event)
                        check.last_at = now
                    return None  # quick follow-up: same conversation, no new check
                self._expire_check(key)  # an unrelated message ends the previous undo window
                if verdict is None and not self._last_error and p_new is not None:
                    label = self._label(source, history, text, None)
                    if gap is not None and gap >= LONG_GAP_SECONDS:
                        refers_back = bool(_BACK_REF.search(" ".join(text.split()[:8])))
                        verdict = refers_back  # quiet for hours: new topic unless it points back
                        label["rule"] = ("quiet 2+ h but refers back, so same topic" if refers_back
                                         else "quiet 2+ h, so new topic")
                    else:
                        verdict = p_new < 0.5  # not sure: go with the lean, the check says so
                else:
                    label = self._label(source, history, text, verdict)
                if verdict is False:  # leans new at all: hold it, no reply, until the owner picks (no timeout)
                    await self._ask(key, source, event, label=label, guess=False, wait=True)
                    return SKIP
                if verdict is not None:
                    if await self._act_and_check(key, source, event, label, verdict):
                        return None  # acted: Hermes processes the message now; the check runs alongside
                    verdict = None  # could not act (reset failed): ask instead
                await self._ask(key, source, event, label=label, guess=verdict, wait=bool(self._last_error))
                return SKIP
            self._expire_check(key)
            if self.confirm_all and history:
                await self._ask(key, source, event, label=self._label(source, history, text, verdict), guess=verdict)
                return SKIP
            if verdict is True:
                return None
            if verdict is False and await self._start_new(source, event):
                return None
            await self._ask(key, source, event)
            return SKIP

    def on_reset(self, session_id=None, old_session_id=None, **_kwargs):
        """Divider for /new (and /clear) in an enabled chat, posted once the reset really happened.
        In threadify chats the conversation that just ended is also moved into a thread."""
        if self.bridge is None or self.loop is None or not session_id:
            return
        try:
            key, origin = self.bridge.origin_of(session_id)
        except Exception:
            return
        if origin is None:
            return
        trigger = self.reset_trigger.pop(key, None)
        if trigger and old_session_id and self.threadify_enabled(origin):
            asyncio.run_coroutine_threadsafe(self._threadify(origin, old_session_id, trigger), self.loop)
        if self.reply_mode(origin):
            # the chat that just ended owns its open messages, except the one that started the new chat
            first = self.reply_trigger.pop(key, None)
            keep = [i for i in self.open_msgs.get(key, []) if i == first]
            self.open_msgs[key] = [i for i in self.open_msgs.get(key, []) if i != first]
            self._settle(key, old_session_id)
            if keep:
                self.open_msgs[key] = keep
                self._save_replies()
            return  # no divider: the conversation lives in replies
        if key in self.quiet_resets or not self.enabled(origin):
            return
        asyncio.run_coroutine_threadsafe(self.bridge.send(origin, DIVIDER), self.loop)

    # ------------------------------------------------------------------ reply chats
    async def _reopen_replied(self, key: str, source, event) -> bool:
        """Reply chats: a reply to an earlier message goes back to that message's conversation, without
        asking the model or the owner. False = not a reply we can place: route it like any other message."""
        target = await self._replied_session(source, getattr(event, "reply_to_message_id", None))
        if not target:
            return False
        now = time.time()
        self.last_msg_at[key] = now
        self._save_starts()
        current = self.bridge.current_session_id(source)
        if target != current:
            self._expire_check(key)  # going back elsewhere ends the previous undo window
            if self.bridge.busy(key) or not current:
                logger.info("topic_router reply scope=%s: Hermes busy, stays in current", self._scope(source))
            elif await self.bridge.switch_back(key, target, current):
                logger.info("topic_router reply scope=%s reopened=%s", self._scope(source), target)
            else:
                logger.warning("topic_router reply scope=%s could not reopen %s", self._scope(source), target)
        return True

    async def _replied_session(self, source, reply_to) -> Optional[str]:
        if not reply_to:
            return None
        sid = self.msg_sessions.get(str(reply_to))
        if sid:
            return sid
        try:  # a Hermes message: its reply anchor, else the nearest earlier message we know
            for mid in await self.bridge.reply_anchor_ids(source, str(reply_to), REPLY_LOOKBACK):
                if self.msg_sessions.get(mid):
                    return self.msg_sessions[mid]
        except Exception:
            logger.warning("topic_router reply lookup failed scope=%s", self._scope(source), exc_info=True)
        return None

    def _remember(self, key: str, event) -> None:
        if event.message_id:
            self.open_msgs.setdefault(key, []).append(str(event.message_id))
            self._save_replies()

    def _settle(self, key: str, session_id: Optional[str]) -> None:
        if not session_id or not self.open_msgs.get(key):
            return  # unknown session: keep them open until we know
        self.msg_sessions.update((i, session_id) for i in self.open_msgs.pop(key))
        self._save_replies()

    def _load_replies(self):
        if not self.reply_state:
            return {}, {}
        try:
            with open(self.reply_state, encoding="utf-8") as f:
                data = json.load(f)
            return dict(data.get("sessions") or {}), {k: list(v) for k, v in (data.get("open") or {}).items()}
        except (OSError, ValueError, AttributeError):
            return {}, {}

    def _save_replies(self) -> None:
        if not self.reply_state:
            return
        try:
            os.makedirs(os.path.dirname(self.reply_state), exist_ok=True)
            tmp = self.reply_state + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sessions": self.msg_sessions, "open": self.open_msgs}, f)
            os.replace(tmp, self.reply_state)
        except OSError:
            logger.warning("topic_router could not save reply state", exc_info=True)

    async def _threadify(self, source, old_session_id: str, before_id: int) -> None:
        """Queue the chat that just ended (it began where the previous one ended), then thread every queued
        chat in this channel, oldest first. A job leaves the queue only once its thread exists, so a crash
        or Discord error can't lose a chat (Oct 11-15 the pointer moved on before a crash and 16 chats
        stayed loose)."""
        scope = self._scope(source)
        self.jobs.append({"scope": scope, "start": self.starts.get(scope), "before": int(before_id),
                          "sid": old_session_id, "tries": 0})
        self.starts[scope] = int(before_id)  # the message that ended this chat begins the next one
        self._save_starts()
        await self._run_jobs(source)

    def _due_jobs(self, source) -> List[dict]:
        if not self.jobs or not self.threadify_enabled(source):
            return []
        scope = self._scope(source)
        return [j for j in self.jobs if j["scope"] == scope and j.get("tries", 0) < THREADIFY_MAX_TRIES]

    async def _run_jobs(self, source) -> None:
        scope = self._scope(source)
        async with self._job_locks.setdefault(scope, asyncio.Lock()):
            for job in list(self._due_jobs(source)):
                try:
                    meta = self.bridge.session_meta(job["sid"])
                    title, started_at = (await meta) if inspect.isawaitable(meta) else meta
                    channel, bot_id = await self.bridge.discord_channel(source)
                    await self.threadifier.run(channel, bot_id, job["start"], started_at, job["before"], title)
                except Exception as exc:
                    job["tries"] = job.get("tries", 0) + 1
                    job["error"] = f"{type(exc).__name__}: {exc}"[:300]
                    self._save_starts()
                    logger.warning("topic_router threadify failed scope=%s before=%s try=%d/%d (kept for retry)",
                                   scope, job["before"], job["tries"], THREADIFY_MAX_TRIES, exc_info=True)
                    return  # keep order: later chats wait behind this one
                self.jobs = [j for j in self.jobs if j is not job]
                self._save_starts()

    def _load_starts(self) -> Dict[str, int]:
        if not self.threadify_state:
            return {}
        try:
            with open(self.threadify_state, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        if "starts" not in data:  # old format: {scope: start id}
            data = {"starts": data}
        try:
            starts = {str(k): int(v) for k, v in (data.get("starts") or {}).items()}
            self.jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict) and j.get("before")]
            self.last_msg_at = {str(k): float(v) for k, v in (data.get("last_msg_at") or {}).items()}
            deferred = {str(k): v for k, v in (data.get("deferred") or {}).items() if isinstance(v, dict)}
        except (TypeError, ValueError, AttributeError):
            return {}
        # A "New topic?" check that was still open when Hermes stopped: its buttons are gone, so the new
        # topic stands; queue the old chat for its thread (runs with the next message in that channel).
        for scope, d in deferred.items():
            self.jobs.append({"scope": scope, "start": starts.get(scope), "before": int(d["before"]),
                              "sid": d.get("sid"), "tries": 0})
            starts[scope] = int(d["before"])
        return starts

    def _save_starts(self) -> None:
        if not self.threadify_state:
            return
        data = {"starts": self.starts, "jobs": self.jobs, "deferred": self.deferred, "last_msg_at": self.last_msg_at}
        try:
            os.makedirs(os.path.dirname(self.threadify_state), exist_ok=True)
            tmp = self.threadify_state + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.threadify_state)
        except OSError:
            logger.warning("topic_router could not save threadify state", exc_info=True)

    # ------------------------------------------------------------------ held messages across restarts
    async def _restore(self, key: str, source, ids: List[str], event) -> None:
        await self.bridge.send(source, RESTORED.format(n=len(ids)))
        self.replay_ids.update(str(i) for i in ids)
        try:
            sent = await self.bridge.replay_discord(source, ids)
            logger.info("topic_router restored held scope=%s replayed=%d/%d", self._scope(source), sent, len(ids))
        except Exception:
            logger.warning("topic_router restore failed scope=%s", self._scope(source), exc_info=True)
        self.bypass.add(self._token(event))
        await self.bridge.release(key, [event])

    def _load_held(self) -> Dict[str, dict]:
        if not self.held_state:
            return {}
        try:
            with open(self.held_state, encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): v for k, v in data.items() if isinstance(v, dict) and v.get("ids")}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_held(self) -> None:
        if not self.held_state:
            return
        data = dict(self.leftovers)
        for key, pending in self.pending.items():
            ids = [str(e.message_id) for e in pending.held if getattr(e, "message_id", None)]
            if ids:
                data[key] = {"platform": getattr(pending.source.platform, "value", str(pending.source.platform)),
                             "chat_id": str(pending.source.chat_id), "ids": ids, "saved_at": time.time()}
        try:
            os.makedirs(os.path.dirname(self.held_state), exist_ok=True)
            tmp = self.held_state + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.held_state)
        except OSError:
            logger.warning("topic_router could not save held messages", exc_info=True)

    def announce_leftovers(self, delay: float = 60.0) -> None:
        """At startup: tell the owner (via Discord REST; the gateway isn't reachable from here yet) that held
        messages survived a restart and go through with the next message in that chat."""
        chats = [(v["chat_id"], len(v["ids"])) for v in self.leftovers.values() if v.get("platform") == "discord"]
        if not chats:
            return
        def post():
            for chat_id, n in chats:
                try:
                    _discord_post(chat_id, RESTART_HELD.format(n=n))
                except Exception:
                    logger.warning("topic_router restart notice failed chat=%s", chat_id, exc_info=True)
        timer = threading.Timer(delay, post)
        timer.daemon = True
        timer.start()


def _discord_post(channel_id: str, text: str) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode(),
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "hermes-topic-router (local, 0.1)"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15):
        pass
