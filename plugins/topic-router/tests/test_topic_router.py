"""Offline tests. They import real gateway types, so run them on Hermes's runtime (bin/test.sh does this):
    ~/.hermes/hermes-agent/.hermes/bin/hermes --run-module unittest discover -s plugins/topic-router/tests -v
"""
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if "topic_router" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "topic_router", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    sys.modules["topic_router"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["topic_router"])

from topic_router import register  # noqa: E402
from topic_router.bridge import GatewayBridge  # noqa: E402
from topic_router.decider import LlmDecider, build_decider, parse_verdict, AskDecider  # noqa: E402
from topic_router.router import DIVIDER, QUESTION, Router, conversation, parse_answer  # noqa: E402

from gateway.config import Platform  # noqa: E402
from gateway.platforms.event import MessageEvent  # noqa: E402
from gateway.session import SessionSource  # noqa: E402
from tools import clarify_gateway  # noqa: E402

CHAN = "4242"
HISTORY = [{"role": "user", "content": "plan my trip"}, {"role": "assistant", "content": "sure"}]


def make_event(text, chat=CHAN, msg_id="m1", is_bot=False, platform=Platform.DISCORD):
    src = SessionSource(platform=platform, chat_id=chat, chat_type="group", user_id="owner", is_bot=is_bot)
    return MessageEvent(text=text, source=src, message_id=msg_id)


class FakeDecider:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def decide(self, history, message):
        self.calls.append((history, message))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeBridge:
    """Mirrors GatewayBridge's surface; records what the router asked the gateway to do."""

    def __init__(self, gateway=None, store=None):
        self.gw = gateway
        self.transcripts = {}  # key -> list
        self.sent, self.resets, self.released, self.choices = [], [], [], []
        self.button_ok = True
        self.authorized_ok = True
        self.router = None

    def session_key(self, source):
        return f"agent:main:{source.platform.value}:group:{source.chat_id}:{source.user_id}"

    def authorized(self, source):
        return self.authorized_ok

    async def transcript(self, source):
        value = self.transcripts.get(self.session_key(source), [])
        if isinstance(value, Exception):
            raise value
        return list(value)

    def origin_of(self, session_id):
        src = SessionSource(platform=Platform.DISCORD, chat_id=CHAN, chat_type="group", user_id="owner")
        return self.session_key(src), src

    async def reset(self, event):
        key = self.session_key(event.source)
        self.resets.append(key)
        self.transcripts[key] = []
        if self.router:  # core fires on_session_reset synchronously inside the reset
            self.router.on_reset(session_id="new-sid")

    async def send(self, source, text):
        self.sent.append(text)
        return True

    def register_choice(self, prompt_id, owner, question, choices):
        entry = SimpleNamespace(event=asyncio.Event(), response=None)
        self.choices.append((prompt_id, owner, entry))
        return entry

    def forget_choice(self, owner):
        pass

    async def send_choice(self, source, question, choices, prompt_id, owner):
        self.sent.append(("BUTTONS", question, tuple(choices)))
        return self.button_ok

    async def release(self, key, events):
        self.released.append([e.text for e in events])


def build(decider, channels=None):
    bridge = FakeBridge()
    router = Router(decider, channels if channels is not None else {"discord": [CHAN]},
                    bridge_factory=lambda gw, store: bridge, poll_seconds=0.01, decide_timeout=1)
    bridge.router = router
    key = bridge.session_key(make_event("x").source)
    bridge.transcripts[key] = HISTORY
    return router, bridge, key


GW = object()


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.02)


class ParsingTests(unittest.TestCase):
    def test_strict_verdicts(self):
        self.assertIs(parse_verdict('{"continue": true}'), True)
        self.assertIs(parse_verdict(' {"continue": false}\n'), False)
        for bad in ['{"continue": null}', '```json\n{"continue": true}\n```', '{"continue": "true"}',
                    '{"continue": true, "why": "x"}', '{"continue": true, "continue": false}',
                    'true', '', None, 'Sure! {"continue": true}', '[{"continue": true}]', '{"continue": 1}']:
            self.assertIsNone(parse_verdict(bad), bad)

    def test_answers(self):
        self.assertEqual(parse_answer(" Continue. "), "continue")
        self.assertEqual(parse_answer("2. New session"), "new")
        self.assertEqual(parse_answer("NEW"), "new")
        self.assertEqual(parse_answer("3"), "cancel")
        self.assertIsNone(parse_answer("new idea: buy milk"))

    def test_conversation_keeps_full_text_and_drops_tool_rows(self):
        big = "x" * 500_000
        rows = [{"role": "system", "content": "s"}, {"role": "user", "content": big},
                {"role": "assistant", "content": None, "tool_calls": [{}]}, {"role": "tool", "content": "out"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}, {"type": "image_url"}]}]
        conv = conversation(rows)
        self.assertEqual(conv[0]["content"], big)  # no truncation
        self.assertEqual([m["role"] for m in conv], ["user", "assistant"])
        self.assertEqual(conv[1]["content"], "hi\n[image_url]")


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_continue_passes_through_with_full_history(self):
        decider = FakeDecider(True)
        router, bridge, _ = build(decider)
        self.assertIsNone(await router.on_dispatch(event=make_event("and hotels?"), gateway=GW))
        self.assertEqual(decider.calls, [(HISTORY, "and hotels?")])
        self.assertEqual((bridge.resets, bridge.sent), ([], []))

    async def test_new_resets_then_divider_then_processes(self):
        router, bridge, key = build(FakeDecider(False))
        self.assertIsNone(await router.on_dispatch(event=make_event("fix my printer"), gateway=GW))
        self.assertEqual(bridge.resets, [key])
        self.assertEqual(bridge.sent, [DIVIDER])  # exactly one divider (on_reset suppressed)

    async def test_unsure_error_timeout_and_garbage_all_hold_and_ask(self):
        async def slow(h, m):
            await asyncio.sleep(5)
        for answer in (None, RuntimeError("boom"), "yes", 1):
            router, bridge, key = build(FakeDecider(answer))
            result = await router.on_dispatch(event=make_event("hmm"), gateway=GW)
            self.assertEqual(result["action"], "skip", answer)
            self.assertIn(key, router.pending)
            self.assertEqual(bridge.sent[0][0:3], ("BUTTONS", bridge.sent[0][1], ("Continue", "New session", "Cancel")))
            self.assertTrue(bridge.sent[0][1].startswith(QUESTION))
            self.assertEqual(bridge.resets, [])
        router, bridge, key = build(SimpleNamespace(decide=slow))
        router.decide_timeout = 0.05
        self.assertEqual((await router.on_dispatch(event=make_event("hmm"), gateway=GW))["action"], "skip")

    async def test_text_fallback_when_no_buttons(self):
        router, bridge, _ = build(FakeDecider(None))
        bridge.button_ok = False
        await router.on_dispatch(event=make_event("hmm"), gateway=GW)
        self.assertTrue(isinstance(bridge.sent[-1], str) and bridge.sent[-1].startswith(QUESTION))

    async def test_typed_answers(self):
        for answer, resets, released, extra in (("continue", 0, [["hmm"]], []),
                                                ("new", 1, [["hmm"]], [DIVIDER]),
                                                ("cancel", 0, [], ["Okay, I dropped that message."])):
            router, bridge, key = build(FakeDecider(None))
            held = make_event("hmm", msg_id="m1")
            await router.on_dispatch(event=held, gateway=GW)
            self.assertEqual((await router.on_dispatch(event=make_event(answer, msg_id="m2"), gateway=GW))["action"], "skip")
            self.assertNotIn(key, router.pending)
            self.assertEqual(len(bridge.resets), resets, answer)
            self.assertEqual([r for r in bridge.released if r], released, answer)
            self.assertEqual(bridge.sent[1:], extra, answer)
            if answer != "cancel":  # the released message skips the decider exactly once
                self.assertEqual(await router.on_dispatch(event=held, gateway=GW), {"action": "allow"})

    async def test_button_answer_and_no_expiry(self):
        router, bridge, key = build(FakeDecider(None))
        await router.on_dispatch(event=make_event("hmm"), gateway=GW)
        await settle()
        self.assertIn(key, router.pending)  # still waiting; nothing expires
        _pid, owner, entry = bridge.choices[0]
        self.assertTrue(owner.startswith("topic-router:"))  # never a real session key
        entry.response = "New session"
        entry.event.set()
        await settle()
        self.assertNotIn(key, router.pending)
        self.assertEqual(bridge.resets, [key])
        self.assertEqual(bridge.released, [["hmm"]])

    async def test_messages_while_waiting_are_held_in_order_without_new_decisions(self):
        decider = FakeDecider(None)
        router, bridge, _ = build(decider)
        await router.on_dispatch(event=make_event("a", msg_id="1"), gateway=GW)
        self.assertEqual((await router.on_dispatch(event=make_event("b", msg_id="2"), gateway=GW))["action"], "skip")
        await router.on_dispatch(event=make_event("continue", msg_id="3"), gateway=GW)
        self.assertEqual(len(decider.calls), 1)
        self.assertEqual(bridge.released, [["a", "b"]])
        self.assertEqual(bridge.resets, [])

    async def test_concurrent_messages_cannot_double_divide(self):
        gate = asyncio.Event()

        class Slow:
            calls = 0

            async def decide(self, h, m):
                Slow.calls += 1
                await gate.wait()
                return False
        router, bridge, key = build(Slow())
        first = asyncio.ensure_future(router.on_dispatch(event=make_event("a", msg_id="1"), gateway=GW))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(router.on_dispatch(event=make_event("b", msg_id="2"), gateway=GW))
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(first, second)
        # second waited for the first; after the reset its session is empty -> first message, no decision
        self.assertEqual((Slow.calls, bridge.resets, bridge.sent), (1, [key], [DIVIDER]))

    async def test_skips_first_message_commands_bots_other_channels_and_unauthorized(self):
        decider = FakeDecider()
        router, bridge, key = build(decider)
        bridge.transcripts[key] = []
        self.assertIsNone(await router.on_dispatch(event=make_event("hello"), gateway=GW))  # first message
        bridge.transcripts[key] = HISTORY
        self.assertIsNone(await router.on_dispatch(event=make_event("/new"), gateway=GW))
        self.assertIsNone(await router.on_dispatch(event=make_event("/model x"), gateway=GW))
        self.assertIsNone(await router.on_dispatch(event=make_event("hi", is_bot=True), gateway=GW))
        self.assertIsNone(await router.on_dispatch(event=make_event(DIVIDER), gateway=GW))
        self.assertIsNone(await router.on_dispatch(event=make_event("hi", chat="999"), gateway=GW))
        self.assertIsNone(await router.on_dispatch(event=make_event("hi", platform=Platform.TELEGRAM), gateway=GW))
        bridge.authorized_ok = False
        self.assertIsNone(await router.on_dispatch(event=make_event("hi"), gateway=GW))
        self.assertEqual((decider.calls, bridge.sent, bridge.resets), ([], [], []))

    async def test_clear_becomes_real_new_and_new_posts_divider_after_reset(self):
        router, bridge, _ = build(FakeDecider())
        self.assertEqual(await router.on_dispatch(event=make_event("/clear"), gateway=GW),
                         {"action": "rewrite", "text": "/new"})
        router.on_reset(session_id="new-sid", reason="new_session")  # what core fires after /new
        await settle()
        self.assertEqual(bridge.sent, [DIVIDER])

    async def test_telegram_is_just_config(self):
        router, bridge, _ = build(FakeDecider(True), {"telegram": ["42"]})
        key = bridge.session_key(make_event("x", chat="42", platform=Platform.TELEGRAM).source)
        bridge.transcripts[key] = HISTORY
        self.assertIsNone(await router.on_dispatch(
            event=make_event("more", chat="42", platform=Platform.TELEGRAM), gateway=GW))
        self.assertEqual(len(router.decider.calls), 1)

    async def test_transcript_read_failure_asks(self):
        router, bridge, key = build(FakeDecider())
        bridge.transcripts[key] = RuntimeError("db")
        self.assertEqual((await router.on_dispatch(event=make_event("x"), gateway=GW))["action"], "skip")
        self.assertIn(key, router.pending)


class DeciderTests(unittest.IsolatedAsyncioTestCase):
    def llm(self, text, provider="example-provider", model="model-a"):
        calls = []

        async def acomplete(**kw):
            calls.append(kw)
            return SimpleNamespace(text=text, provider=provider, model=model)
        return SimpleNamespace(acomplete=acomplete), calls

    async def test_llm_decider_sends_full_history_and_parses(self):
        llm, calls = self.llm('{"continue": false}')
        decider = LlmDecider(lambda: llm, route=lambda: ("example-provider", "model-a"))
        self.assertIs(await decider.decide(HISTORY, "new thing"), False)
        self.assertEqual(calls[0]["task"], "topic_router")
        self.assertIn('"plan my trip"', calls[0]["messages"][1]["content"])
        self.assertIn('"new thing"', calls[0]["messages"][1]["content"])

    async def test_fallback_route_is_rejected(self):
        llm, _ = self.llm('{"continue": true}', provider="openrouter", model="other")
        with self.assertRaises(RuntimeError):
            await LlmDecider(lambda: llm, route=lambda: ("example-provider", "model-a")).decide(HISTORY, "x")
        llm, _ = self.llm('{"continue": true}', model="model-b")
        with self.assertRaises(RuntimeError):
            await LlmDecider(lambda: llm, route=lambda: ("", "model-a")).decide(HISTORY, "x")

    def test_build_backends(self):
        self.assertIsInstance(build_decider({}, lambda: None), LlmDecider)
        self.assertIsInstance(build_decider({"backend": "ask"}, lambda: None), AskDecider)
        custom = build_decider({"backend": "topic_router.decider:AskDecider"}, lambda: None)
        self.assertIsNone(custom.decide([], "x"))


class FakeAdapter:
    def __init__(self):
        self.sent, self.handled, self._active_sessions = [], [], {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=True)

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.sent.append((chat_id, question, tuple(choices)))
        return SimpleNamespace(success=True)

    async def handle_message(self, event):
        self.handled.append(event.text)


class FakeStore:
    def __init__(self, entry, rows):
        self.entry, self.rows, self.writes = entry, rows, 0

    def lookup_by_session_key(self, key):
        return self.entry if self.entry and key == self.entry.session_key else None

    def lookup_by_session_id(self, sid):
        return self.entry if self.entry and sid == self.entry.session_id else None

    def load_transcript(self, sid):
        return list(self.rows)

    def get_or_create_session(self, *a, **k):  # must never be called
        self.writes += 1


class FakeGateway:
    def __init__(self, store):
        self.session_store, self.adapter = store, FakeAdapter()
        self.reset_calls, self.queued = [], []

    def _session_key_for_source(self, source):
        return f"k:{source.chat_id}"

    def _is_user_authorized_for_source(self, source):
        return True

    def _delivery_adapter_for(self, source):
        return self.adapter

    _intake_adapter_for = _delivery_adapter_for

    async def _handle_reset_command(self, event):
        self.reset_calls.append(event.text)

    def _queue_or_replace_pending_event(self, key, event):
        self.queued.append(event.text)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_against_fake_gateway(self):
        entry = SimpleNamespace(session_key=f"k:{CHAN}", session_id="s1", origin=make_event("x").source)
        store = FakeStore(entry, HISTORY)
        gw = FakeGateway(store)
        bridge = GatewayBridge(gw)
        src = make_event("x").source
        self.assertEqual(await bridge.transcript(src), HISTORY)
        self.assertEqual(await GatewayBridge(FakeGateway(FakeStore(None, []))).transcript(src), [])
        self.assertEqual(store.writes, 0)
        self.assertEqual(bridge.origin_of("s1"), (f"k:{CHAN}", src))
        await bridge.reset(make_event("fix printer"))
        self.assertEqual(gw.reset_calls, ["/new"])
        await bridge.release("k", [make_event("a"), make_event("b")])
        self.assertEqual((gw.adapter.handled, gw.queued), (["a"], ["b"]))
        gw.adapter._active_sessions["k"] = object()
        await bridge.release("k", [make_event("c")])
        self.assertEqual(gw.queued, ["b", "c"])

    async def test_real_clarify_registry_round_trip(self):
        bridge = GatewayBridge(FakeGateway(FakeStore(None, [])))
        entry = bridge.register_choice("tr-test", "topic-router:k", QUESTION, ["Continue", "New session", "Cancel"])
        self.assertFalse(clarify_gateway.has_pending("k"))  # real session key untouched
        self.assertTrue(clarify_gateway.resolve_gateway_clarify("tr-test", "New session"))  # what a button does
        self.assertTrue(entry.event.is_set())
        self.assertEqual(parse_answer(entry.response), "new")
        bridge.forget_choice("topic-router:k")
        self.assertFalse(clarify_gateway.has_pending("topic-router:k"))

    async def test_end_to_end_with_fake_gateway(self):
        entry = SimpleNamespace(session_key=f"k:{CHAN}", session_id="s1", origin=make_event("x").source)
        gw = FakeGateway(FakeStore(entry, HISTORY))
        router = Router(FakeDecider(None), {"discord": [CHAN]}, poll_seconds=0.01)
        held = make_event("hmm")
        self.assertEqual((await router.on_dispatch(event=held, gateway=gw, session_store=gw.session_store))["action"], "skip")
        self.assertEqual(gw.adapter.sent[0][2], ("Continue", "New session", "Cancel"))
        prompt_id = router.pending[f"k:{CHAN}"].prompt_id
        clarify_gateway.resolve_gateway_clarify(prompt_id, "New session")  # the user taps the button
        await settle()
        self.assertEqual(gw.reset_calls, ["/new"])
        self.assertEqual(gw.adapter.sent[-1], (CHAN, DIVIDER))
        self.assertEqual(gw.adapter.handled, ["hmm"])
        self.assertEqual(await router.on_dispatch(event=held, gateway=gw), {"action": "allow"})


class RegisterTests(unittest.TestCase):
    def test_register_wires_hooks_and_aux_task(self):
        calls = []
        settings = {"channels": {"discord": [CHAN]}, "decider": {"backend": "ask"}}
        ctx = SimpleNamespace(
            get_config=lambda k, d=None: settings.get(k, d),
            register_auxiliary_task=lambda key, **kw: calls.append(("aux", key)),
            register_hook=lambda name, cb: calls.append(("hook", name)),
            spawn_task=lambda coro, name=None: asyncio.ensure_future(coro))
        router = register(ctx)
        self.assertEqual(calls, [("aux", "topic_router"), ("hook", "pre_gateway_dispatch"),
                                 ("hook", "on_session_reset")])
        self.assertTrue(router.enabled(make_event("x").source))


if __name__ == "__main__":
    unittest.main()
