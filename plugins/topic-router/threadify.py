"""Threadify: when a chat in an enabled channel ends (router "new topic" or /new), move that
finished conversation out of the channel into a Discord thread so the channel stays readable.

Discord can't move messages, so: the conversation's first message by the owner becomes the thread's
starter (it stays in the channel with the thread attached), every later message is copied into
the thread (the owner's prefixed with their name, attachments re-uploaded), then the originals and the
router's own clutter (dividers, "Same topic?" prompts, yes/no answers) are deleted. Nothing is
deleted unless every copy succeeded. The thread is named after Hermes's title for the session.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional, Tuple

logger = logging.getLogger("hermes_plugins.topic_router")

MAX_MESSAGES = 500
DISCORD_LIMIT = 2000
THREAD_NAME_LIMIT = 100
BULK_DELETE_MAX_AGE_DAYS = 13  # Discord's bulk delete refuses messages older than 14 days


def _chunks(text: str, size: int = DISCORD_LIMIT) -> List[str]:
    text = text or ""
    if len(text) <= size:
        return [text]
    out = []
    while text:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


def thread_name(title: Optional[str], first_text: str) -> str:
    name = (title or "").strip()
    if not name:
        name = re.sub(r"<[@#][!&]?\d+>", "", first_text or "")
        name = re.sub(r"\s+", " ", name).strip() or "Earlier chat"
    return name if len(name) <= THREAD_NAME_LIMIT else name[:THREAD_NAME_LIMIT - 1] + "…"


def plan(messages: Iterable[Any], bot_id: int, is_noise: Callable[[Any, bool], bool]) -> Tuple[Optional[Any], List[Any], List[Any]]:
    """Split one finished conversation (oldest first) into (starter, to_copy, noise).

    starter = the owner's first real message (None means there is nothing worth a thread).
    Bot messages before it (e.g. the previous /new banner) are left alone. Noise after it is
    deleted without copying."""
    starter, to_copy, noise = None, [], []
    prev_real_bot = False  # was the message just before this one a real Hermes reply (not router clutter)?
    for msg in messages:
        mine = getattr(msg.author, "id", None) == bot_id
        noisy = is_noise(msg, mine)
        if noisy and not mine and prev_real_bot and not (msg.content or "").strip().startswith("/"):
            noisy = False  # The owner's "yes" to Hermes's own question is part of the chat, not a router answer
        prev_real_bot = mine and not noisy
        if noisy:
            noise.append(msg)
        elif starter is None:
            if not mine:
                starter = msg
        else:
            to_copy.append(msg)
    if starter is None or not to_copy:
        return None, [], []
    return starter, to_copy, noise


class Threadifier:
    def __init__(self, is_noise: Callable[[Any, bool], bool], owner_label: str = "Owner"):
        self.is_noise = is_noise
        self.owner_label = owner_label

    async def run(self, channel: Any, bot_id: int, start_id: Optional[int], started_at: Optional[float],
                  before_id: int, title: Optional[str]) -> Optional[Any]:
        """Thread the conversation that began at message ``start_id`` (the message that ended the chat
        before it; inclusive) and ended before message ``before_id``. Without ``start_id`` (first run in
        a chat) the session's start time is used."""
        import discord

        after = None
        if start_id:
            after = discord.Object(id=int(start_id) - 1)
        elif started_at:
            # 5 s of slack: the session row is written a moment before/after the owner's first message lands.
            after = discord.Object(id=discord.utils.time_snowflake(
                datetime.fromtimestamp(float(started_at) - 5, tz=timezone.utc)))
        history = [m async for m in channel.history(limit=MAX_MESSAGES, after=after,
                                                    before=discord.Object(id=int(before_id)), oldest_first=True)]
        starter, to_copy, noise = plan(history, bot_id, self.is_noise)
        if starter is None:
            for msg in noise:  # still tidy away router clutter around a one-message chat
                await self._delete_one(msg)
            return None
        name = thread_name(title, starter.content)
        thread, done = getattr(starter, "thread", None), set()
        if thread is not None:  # a retry after a half-finished run: reuse the thread, skip what's copied
            done = {m.content async for m in thread.history(limit=MAX_MESSAGES)}
        else:
            thread = await starter.create_thread(name=name, auto_archive_duration=10080,
                                                 reason="topic-router: finished chat moved into a thread")
        async for notice in channel.history(limit=10):  # Discord's "Hermes started a thread" line
            if (getattr(notice, "type", None) == discord.MessageType.thread_created
                    and getattr(notice.author, "id", None) == bot_id and notice.content == thread.name):
                await self._delete_one(notice)
        none = discord.AllowedMentions.none()
        for msg in to_copy:
            await self._copy(thread, msg, bot_id, none, done)
        await self._delete(channel, to_copy + noise)
        logger.info("topic_router threadify channel=%s thread=%s moved=%d cleared=%d",
                    getattr(channel, "id", "?"), thread.id, len(to_copy), len(noise))
        return thread

    async def _copy(self, thread: Any, msg: Any, bot_id: int, none: Any, done: frozenset = frozenset()) -> None:
        mine = getattr(msg.author, "id", None) == bot_id
        text = msg.content or ""
        if not mine:
            text = f"**{self.owner_label}:** {text}" if text else f"**{self.owner_label}:**"
        if text and _chunks(text)[0] in done:
            return  # already copied by an earlier, interrupted run
        files =[await a.to_file() for a in (msg.attachments or [])]
        embeds = [e for e in (msg.embeds or []) if getattr(e, "type", "rich") == "rich"]
        parts = _chunks(text) if text else [""]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            kwargs = {"allowed_mentions": none}
            if last and files:
                kwargs["files"] = files
            if last and embeds:
                kwargs["embeds"] = embeds[:10]
            if part or len(kwargs) > 1:
                await thread.send(part or None, **kwargs)

    async def _delete(self, channel: Any, msgs: List[Any]) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - BULK_DELETE_MAX_AGE_DAYS * 86400
        recent = [m for m in msgs if m.created_at.timestamp() > cutoff]
        old = [m for m in msgs if m.created_at.timestamp() <= cutoff]
        for i in range(0, len(recent), 100):
            batch = recent[i:i + 100]
            if len(batch) == 1:
                await self._delete_one(batch[0])
            else:
                await channel.delete_messages(batch, reason="topic-router: moved into a thread")
        for msg in old:
            await self._delete_one(msg)

    @staticmethod
    async def _delete_one(msg: Any) -> None:
        try:
            await msg.delete()
        except Exception:
            logger.warning("topic_router threadify could not delete message %s", getattr(msg, "id", "?"), exc_info=True)
