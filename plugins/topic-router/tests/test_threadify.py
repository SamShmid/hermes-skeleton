"""Offline tests for moving a finished chat into a thread (fake discord.py objects).
Run like test_topic_router.py, on Hermes's runtime."""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if "topic_router" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "topic_router", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    sys.modules["topic_router"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["topic_router"])

from topic_router.router import DIVIDER, Router, router_noise  # noqa: E402
from topic_router.threadify import Threadifier, plan, thread_name  # noqa: E402

BOT, OWNER = 1, 2


class FakeThread:
    def __init__(self, name):
        self.id, self.name, self.sent = 999, name, []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class FakeAttachment:
    async def to_file(self):
        return "FILE"


class Msg:
    def __init__(self, mid, author, content, attachments=(), age_days=0):
        self.id, self.author, self.content = mid, SimpleNamespace(id=author), content
        self.attachments, self.embeds = list(attachments), []
        self.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        self.thread, self.deleted, self.channel = None, False, None

    async def create_thread(self, name, **_):
        self.thread = FakeThread(name)
        if self.channel is not None:  # Discord posts a "started a thread" notice in the channel
            import discord
            notice = Msg(10**6, BOT, name)
            notice.type = discord.MessageType.thread_created
            self.channel.messages.append(notice)
        return self.thread

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, messages):
        self.id, self.messages, self.bulk = 77, messages, []
        for m in messages:
            m.channel = self

    async def history(self, limit=100, after=None, before=None, oldest_first=False):
        for m in sorted(self.messages, key=lambda m: m.id):
            if (after is None or m.id > after.id) and (before is None or m.id < before.id):
                yield m

    async def delete_messages(self, msgs, reason=None):
        self.bulk.append([m.id for m in msgs])
        for m in msgs:
            m.deleted = True


def chat():
    return [
        Msg(10, OWNER, "what's a good pasta recipe"),       # previous trigger = starter
        Msg(11, BOT, "Same topic? (my guess: new topic)"),  # its prompt
        Msg(12, OWNER, "no"),                                 # typed answer
        Msg(13, BOT, DIVIDER),
        Msg(14, BOT, "Try cacio e pepe."),
        Msg(15, OWNER, "and a sauce?", attachments=[FakeAttachment()]),
        Msg(16, BOT, "Same topic? (my guess: same topic)"),
        Msg(17, BOT, "Brown butter sage."),
        Msg(20, OWNER, "remind me to call mom"),              # new trigger (excluded)
    ]


class PlanTests(unittest.TestCase):
    def test_noise(self):
        self.assertTrue(router_noise(Msg(1, BOT, DIVIDER), True))
        self.assertTrue(router_noise(Msg(1, BOT, "❓ **Hermes needs your input**\n\nSame topic? (my guess: x)"), True))
        self.assertTrue(router_noise(Msg(1, BOT, "✨ Session reset! Starting fresh."), True))
        self.assertTrue(router_noise(Msg(1, OWNER, "No, new topic"), False))
        self.assertTrue(router_noise(Msg(1, OWNER, "/new"), False))
        self.assertFalse(router_noise(Msg(1, OWNER, "no idea what to cook"), False))
        self.assertFalse(router_noise(Msg(1, BOT, "Sure, here it is."), True))
        self.assertTrue(router_noise(Msg(1, BOT, "⏳ Working — 3 min — execute_code"), True))
        self.assertTrue(router_noise(Msg(1, BOT, "✅ Command approved. The agent is resuming..."), True))
        self.assertTrue(router_noise(Msg(1, OWNER, "approve"), False))
        self.assertTrue(router_noise(Msg(1, BOT, "❓ **Hermes needs your input**\n\nNew topic? (model: new topic, 99% sure, "
                                                "took 0.21 s) Already started; tap No to undo."), True))
        self.assertFalse(router_noise(Msg(1, OWNER, "approve the budget email draft please"), False))

    def test_plan_splits_starter_copy_noise(self):
        msgs = [m for m in chat() if m.id < 20]
        starter, copy, noise = plan(msgs, BOT, router_noise)
        self.assertEqual(starter.id, 10)
        self.assertEqual([m.id for m in copy], [14, 15, 17])
        self.assertEqual([m.id for m in noise], [11, 12, 13, 16])

    def test_bot_lines_before_starter_are_left_alone(self):
        starter, copy, noise = plan([Msg(5, BOT, "cron says hi"), Msg(6, OWNER, "hi"), Msg(7, BOT, "hello")],
                                    BOT, router_noise)
        self.assertEqual((starter.id, [m.id for m in copy], noise), (6, [7], []))

    def test_yes_to_hermes_own_question_is_kept(self):
        msgs = [Msg(1, OWNER, "make a recurring event"), Msg(2, BOT, "Found the contact. Confirm?"), Msg(3, OWNER, "Yes"),
                Msg(4, BOT, "Same topic? (model: same topic)"), Msg(5, OWNER, "yes"), Msg(6, BOT, "Created.")]
        starter, copy, noise = plan(msgs, BOT, router_noise)
        self.assertEqual(([m.id for m in copy], [m.id for m in noise]), ([2, 3, 6], [4, 5]))

    def test_single_message_is_not_threaded(self):
        self.assertEqual(plan([Msg(6, OWNER, "hi")], BOT, router_noise), (None, [], []))

    def test_thread_name(self):
        self.assertEqual(thread_name("Pasta ideas", "x"), "Pasta ideas")
        self.assertEqual(thread_name(None, "hey <@123> look"), "hey look")
        self.assertEqual(len(thread_name("x" * 300, "")), 100)


class RunTests(unittest.TestCase):
    def test_moves_chat_into_thread_and_clears_channel(self):
        msgs = chat()
        channel = FakeChannel(msgs)
        thread = asyncio.run(Threadifier(router_noise).run(channel, BOT, 10, None, 20, "Pasta recipes"))
        self.assertEqual(thread.name, "Pasta recipes")
        self.assertEqual([c for c, _ in thread.sent], ["Try cacio e pepe.", "**Owner:** and a sauce?", "Brown butter sage."])
        self.assertEqual(thread.sent[1][1]["files"], ["FILE"])
        by_id = {m.id: m for m in msgs}
        self.assertFalse(by_id[10].deleted)  # starter stays, with the thread attached
        self.assertFalse(by_id[20].deleted)  # the new chat is untouched
        self.assertTrue(all(by_id[i].deleted for i in (11, 12, 13, 14, 15, 16, 17)))
        self.assertTrue(channel.messages[-1].deleted)  # the "started a thread" notice

    def test_copy_failure_deletes_nothing(self):
        msgs = chat()

        class Broken(FakeThread):
            async def send(self, content=None, **kwargs):
                raise RuntimeError("discord down")

        async def broken_thread(name, **_):
            return Broken(name)
        msgs[0].create_thread = broken_thread
        with self.assertRaises(RuntimeError):
            asyncio.run(Threadifier(router_noise).run(FakeChannel(msgs), BOT, 10, None, 20, "t"))
        self.assertFalse(any(m.deleted for m in msgs))

    def test_old_messages_deleted_one_by_one(self):
        msgs = [Msg(1, OWNER, "a", age_days=20), Msg(2, BOT, "b", age_days=20), Msg(3, BOT, "c")]
        channel = FakeChannel(msgs)
        asyncio.run(Threadifier(router_noise).run(channel, BOT, 1, None, 50, "t"))
        self.assertEqual(channel.bulk, [])  # one recent + one old: no bulk call
        self.assertTrue(msgs[1].deleted and msgs[2].deleted)

    def test_retry_reuses_thread_and_skips_copied(self):
        msgs = chat()
        existing = FakeThread("Pasta recipes")
        existing.sent = [("Try cacio e pepe.", {})]

        async def history(limit=100):
            for content, _ in list(existing.sent):
                yield SimpleNamespace(content=content)
        existing.history = history
        msgs[0].thread = existing
        thread = asyncio.run(Threadifier(router_noise).run(FakeChannel(msgs), BOT, 10, None, 20, "Pasta recipes"))
        self.assertIs(thread, existing)
        self.assertEqual([c for c, _ in thread.sent], ["Try cacio e pepe.", "**Owner:** and a sauce?", "Brown butter sage."])

    def test_old_state_format_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "s.json")
            with open(state, "w") as f:
                json.dump({"discord:4242": 424242}, f)
            self.assertEqual(Router(None, {}, threadify_state=state).starts, {"discord:4242": 424242})

    def test_long_message_is_split(self):
        long = "line\n" * 900  # 4500 chars
        msgs = [Msg(1, OWNER, "start"), Msg(2, OWNER, long)]
        thread = asyncio.run(Threadifier(router_noise).run(FakeChannel(msgs), BOT, 1, None, 50, "t"))
        self.assertGreater(len(thread.sent), 1)
        self.assertTrue(all(len(c) <= 2000 for c, _ in thread.sent))


class RouterWiringTests(unittest.TestCase):
    def test_reset_threadifies_and_remembers_next_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "s.json")
            router = Router(None, {"discord": ["4242"]}, threadify={"discord": ["4242"]}, threadify_state=state)
            calls = []

            class B:
                def origin_of(self, sid):
                    return "k", SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id="4242", thread_id=None)

                def session_meta(self, sid):
                    return "Title", 123.0

                async def discord_channel(self, source):
                    return "chan", BOT

                async def send(self, source, text):
                    pass

            async def fake_run(channel, bot_id, start_id, started_at, before_id, title):
                calls.append((start_id, started_at, before_id, title))

            router.threadifier.run = fake_run

            async def go():
                router.bridge, router.loop = B(), asyncio.get_running_loop()
                router.reset_trigger["k"] = 20
                router.on_reset(session_id="new", old_session_id="old")
                await asyncio.sleep(0.05)
                router.reset_trigger["k"] = 30
                router.on_reset(session_id="new2", old_session_id="new")
                await asyncio.sleep(0.05)
            asyncio.run(go())
            self.assertEqual(calls, [(None, 123.0, 20, "Title"), (20, 123.0, 30, "Title")])
            with open(state) as f:
                self.assertEqual(json.load(f)["starts"], {"discord:4242": 30})
            # a fresh router (restart) picks up where the last one left off
            self.assertEqual(Router(None, {}, threadify_state=state).starts, {"discord:4242": 30})

    def test_no_trigger_no_threadify(self):
        router = Router(None, {"discord": ["4242"]}, threadify={"discord": ["4242"]})
        router.threadifier.run = None  # would crash if called
        router.bridge = SimpleNamespace(origin_of=lambda sid: ("k", SimpleNamespace(
            platform=SimpleNamespace(value="discord"), chat_id="4242", thread_id=None)))
        router.loop = asyncio.new_event_loop()
        router.quiet_resets.add("k")
        router.on_reset(session_id="new", old_session_id="old")  # no reset_trigger -> nothing
        router.loop.close()


if __name__ == "__main__":
    unittest.main()
