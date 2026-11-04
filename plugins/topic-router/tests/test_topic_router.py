"""Offline tests. They import real gateway types, so run them on Hermes's runtime (bin/test.sh does this):
    ~/.hermes/hermes-agent/.hermes/bin/hermes --run-module unittest discover -s plugins/topic-router/tests -v
"""
import asyncio
import json
import importlib.util
import sys
import time
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
from topic_router.router import DIVIDER, QUESTION, SKIP, Router, conversation, parse_answer  # noqa: E402

from gateway.config import Platform  # noqa: E402
from gateway.platforms.event import MessageEvent  # noqa: E402
from gateway.session import SessionSource  # noqa: E402
from tools import clarify_gateway  # noqa: E402

CHAN = "4242"
HISTORY = [{"role": "user", "content": "plan my trip"}, {"role": "assistant", "content": "sure"}]


def make_event(text, chat=CHAN, msg_id="m1", is_bot=False, platform=Platform.DISCORD, reply_to=None):
    src = SessionSource(platform=platform, chat_id=chat, chat_type="group", user_id="owner", is_bot=is_bot)
    return MessageEvent(text=text, source=src, message_id=msg_id, reply_to_message_id=reply_to)


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
        self.question_pending = False
        self.router = None
        self.sid, self.calls, self.switch_ok = "old-sid", [], True
        self.replay_results = []
        self.is_busy = False
        self.anchors = {}

    def busy(self, key):
        return self.is_busy

    def agent_question_pending(self, source):
        return self.question_pending

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
        self.sid = "new-sid"
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

    async def replay_discord(self, source, ids):
        self.calls.append(("replay", list(ids)))
        for i in ids:  # core re-dispatches each one; the router must let it straight through
            self.replay_results.append(await self.router.on_dispatch(event=make_event("old", msg_id=i), gateway=GW))
        return len(ids)

    # act-then-check undo surface
    def current_session_id(self, source):
        return self.sid

    async def stop_turn(self, key, source):
        self.calls.append("stop")

    async def switch_back(self, key, old_sid, new_sid):
        self.calls.append(("switch_back", old_sid, new_sid))
        if self.switch_ok:
            self.sid = old_sid
        return self.switch_ok

    async def discard_session(self, session_id):
        self.calls.append(("discard", session_id))

    async def rewind_last(self, key, session_id, turns=1):
        self.calls.append(("rewind", session_id) if turns == 1 else ("rewind", session_id, turns))
        return "x"

    async def reply_anchor_ids(self, source, message_id, lookback):
        self.calls.append(("anchors", message_id))
        return self.anchors.get(message_id, [])

    async def delete_bot_messages_after(self, source, after_id):
        self.calls.append(("delete_after", after_id))
        return 2


def build(decider, channels=None, **kw):
    bridge = FakeBridge()
    router = Router(decider, channels if channels is not None else {"discord": [CHAN]},
                    bridge_factory=lambda gw, store: bridge, poll_seconds=0.01, decide_timeout=1, **kw)
    bridge.router = router
    key = bridge.session_key(make_event("x").source)
    bridge.transcripts[key] = HISTORY
    return router, bridge, key


GW = object()


async def new_check(router, bridge, text, msg_id):
    """A "New chat?" act-then-check, driven directly: since Oct 27 dispatch asks first when the model leans
    new, but the check machinery (undo, deferred threadify) still backs reply-mode and older flows."""
    router.bridge = bridge
    ev = make_event(text, msg_id=msg_id)
    key = bridge.session_key(ev.source)
    label = {"verdict": "new", "p_new": 0.99, "latency_ms": 0}
    router.last_msg_at[key] = time.time()
    return await router._act_and_check(key, ev.source, ev, label, False)


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

    async def test_answer_to_agent_question_is_never_held(self):
        decider = FakeDecider(False)
        router, bridge, _ = build(decider)
        bridge.question_pending = True  # the agent called clarify and is waiting on the owner
        self.assertIsNone(await router.on_dispatch(event=make_event("the one from tuesday"), gateway=GW))
        self.assertEqual((decider.calls, bridge.choices, bridge.sent), ([], [], []))

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

    async def test_confirm_all_asks_yes_no_every_time_and_logs(self):
        import json, os, tempfile
        log = os.path.join(tempfile.mkdtemp(), "labels.jsonl")
        for verdict, reply, expect in ((True, "yes", "continue"), (False, "no", "new"), (True, "no", "new")):
            router, bridge, key = build(FakeDecider(verdict))
            router.confirm_all, router.label_log = True, log
            self.assertEqual((await router.on_dispatch(event=make_event("next thing"), gateway=GW))["action"], "skip")
            self.assertIn(key, router.pending)
            self.assertEqual(bridge.sent[0][2], ("Same chat", "New chat", "Cancel"))
            self.assertIn("Same chat or new chat?", bridge.sent[0][1])
            await router.on_dispatch(event=make_event(reply, msg_id="m2"), gateway=GW)
            self.assertNotIn(key, router.pending)
            self.assertEqual(len(bridge.resets), 1 if expect == "new" else 0)
        rows = [json.loads(l) for l in open(log)]
        self.assertEqual([r["answer"] for r in rows], ["continue", "new", "new"])
        self.assertEqual([r["verdict"] for r in rows], ["continue", "new", "continue"])
        self.assertEqual(rows[0]["message"], "next thing")
        self.assertTrue(rows[0]["context"])

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
        self._running_agents = {}

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


class ActConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def _setup(self, verdict, log=None):
        router, bridge, key = build(FakeDecider(verdict))
        router.act_confirm, router.label_log = True, log
        return router, bridge, key

    async def _press(self, router, bridge, key, answer):
        check = router.checks[key]
        check.choice.response = answer
        check.choice.event.set()
        await settle()

    async def test_confident_continue_acts_and_yes_logs(self):
        import json, os, tempfile
        log = os.path.join(tempfile.mkdtemp(), "labels.jsonl")
        router, bridge, key = await self._setup(True, log)
        self.assertIsNone(await router.on_dispatch(event=make_event("next thing"), gateway=GW))  # goes through now
        self.assertIn(key, router.checks)
        self.assertEqual(bridge.sent[0][0], "BUTTONS")
        self.assertIn("Same chat?", bridge.sent[0][1])
        self.assertEqual(bridge.sent[0][2], ("Same chat", "New chat"))
        await self._press(router, bridge, key, "Same chat")
        self.assertNotIn(key, router.checks)
        self.assertEqual(bridge.calls, [])
        row = json.loads(open(log).read().splitlines()[-1])
        self.assertEqual((row["verdict"], row["answer"], row["via"]), ("continue", "continue", "check"))

    async def test_confident_new_holds_silently_until_owner_picks(self):
        router, bridge, key = await self._setup(False)
        result = await router.on_dispatch(event=make_event("other thing", msg_id="76"), gateway=GW)
        self.assertEqual(result["action"], "skip")  # Hermes does not reply yet
        self.assertTrue(router.pending[key].wait)  # no timeout: waits for the owner
        self.assertNotIn(key, router.checks)
        self.assertEqual(bridge.resets, [])
        self.assertEqual(bridge.sent[0][2], ("Same chat", "New chat", "Cancel"))
        self.assertTrue(bridge.sent[0][1].startswith("Same chat or new chat?"))
        await router.on_dispatch(event=make_event("new chat", msg_id="76b"), gateway=GW)
        self.assertEqual(bridge.resets, [key])
        self.assertEqual(bridge.released[-1], ["other thing"])

    async def test_confident_new_resets_without_threadify_then_undo_switches_back(self):
        router, bridge, key = await self._setup(False)
        router.threadify_channels = {"discord": {CHAN}}
        self.assertTrue(await new_check(router, bridge, "other thing", "77"))
        self.assertEqual(len(bridge.resets), 1)
        self.assertNotIn(key, router.reset_trigger)  # threadify waits for the owner
        check = router.checks[key]
        self.assertEqual((check.old_sid, check.new_sid), ("old-sid", "new-sid"))
        self.assertIn("New chat?", bridge.sent[-1][1])
        await self._press(router, bridge, key, "Same chat")
        self.assertEqual(bridge.calls, ["stop", ("switch_back", "old-sid", "new-sid"), ("discard", "new-sid"),
                                        ("delete_after", "77")])
        self.assertIn("↩️ Undone: back in the same conversation.", bridge.sent)
        self.assertEqual(bridge.released[-1], ["other thing"])
        self.assertIn(router._token(make_event("other thing", msg_id="77")), router.bypass)

    async def test_confident_continue_undo_rewinds_and_starts_new(self):
        router, bridge, key = await self._setup(True)
        bridge.transcripts[key] = HISTORY
        ev = make_event("other thing", msg_id="78")
        self.assertIsNone(await router.on_dispatch(event=ev, gateway=GW))
        bridge.transcripts[key] = HISTORY + [{"role": "user", "content": "other thing"}, {"role": "assistant", "content": "partial"}]
        await self._press(router, bridge, key, "No, undo")
        self.assertEqual(bridge.calls, ["stop", ("rewind", "old-sid"), ("delete_after", "78")])
        self.assertEqual(len(bridge.resets), 1)
        self.assertIn(DIVIDER, bridge.sent)
        self.assertEqual(bridge.released[-1], ["other thing"])

    async def test_undo_still_moves_message_when_turn_was_never_saved(self):
        router, bridge, key = await self._setup(True)
        self.assertIsNone(await router.on_dispatch(event=make_event("other thing", msg_id="79"), gateway=GW))
        await self._press(router, bridge, key, "No, undo")  # transcript still lacks "other thing"
        self.assertEqual(bridge.calls, ["stop", ("delete_after", "79")])  # nothing to rewind
        self.assertEqual(len(bridge.resets), 1)  # but it still goes to a new conversation (Oct 13 bug)
        self.assertEqual(bridge.released[-1], ["other thing"])

    async def test_failed_switch_back_does_not_release(self):
        router, bridge, key = await self._setup(False)
        bridge.switch_ok = False
        await new_check(router, bridge, "other thing", "80")
        await self._press(router, bridge, key, "No, undo")
        self.assertNotIn(("discard", "new-sid"), bridge.calls)
        self.assertFalse([r for r in bridge.released if r])

    async def test_unsure_still_asks_first(self):
        router, bridge, key = await self._setup(None)
        self.assertEqual((await router.on_dispatch(event=make_event("hmm"), gateway=GW))["action"], "skip")
        self.assertIn(key, router.pending)
        self.assertNotIn(key, router.checks)

    async def test_next_message_expires_check_and_runs_deferred_threadify(self):
        router, bridge, key = build(FakeDecider(True))
        router.act_confirm = True
        router.threadify_channels = {"discord": {CHAN}}
        ran = []
        async def fake_threadify(source, old_sid, before_id):
            ran.append((old_sid, before_id))
        router._threadify = fake_threadify
        await new_check(router, bridge, "other thing", "81")
        bridge.transcripts[key] = HISTORY
        router.last_msg_at[key] -= 600  # not a quick follow-up
        await router.on_dispatch(event=make_event("more", msg_id="82"), gateway=GW)
        await settle()
        self.assertEqual(ran, [("old-sid", 81)])
        self.assertIn(key, router.checks)  # the new message has its own check
        self.assertEqual(router.checks[key].event.text, "more")

    async def test_guess_line_shows_model_and_time(self):
        line = Router._guess_line({"verdict": "new", "p_new": 0.93, "latency_ms": 212})
        self.assertEqual(line, "model: new topic, 93% sure, took 0.21 s")
        line = Router._guess_line({"verdict": "unsure", "p_new": 0.35, "latency_ms": 180})
        self.assertEqual(line, "model: not sure, leans same topic 65%, took 0.18 s")


class WaitingTests(unittest.IsolatedAsyncioTestCase):
    """Messages sent while the router waits on the owner: announced, time-limited, and restart-safe."""

    async def test_held_message_gets_a_queued_notice_with_count(self):
        router, bridge, key = build(FakeDecider(None))
        await router.on_dispatch(event=make_event("a", msg_id="1"), gateway=GW)
        await router.on_dispatch(event=make_event("b", msg_id="2"), gateway=GW)
        await router.on_dispatch(event=make_event("c", msg_id="3"), gateway=GW)
        notices = [m for m in bridge.sent if isinstance(m, str) and m.startswith("📥 Queued")]
        self.assertEqual(len(notices), 2)
        self.assertIn("(3 waiting)", notices[-1])

    async def test_timeout_goes_with_guess_and_logs_no_label(self):
        import tempfile, os
        log = os.path.join(tempfile.mkdtemp(), "labels.jsonl")
        router, bridge, key = build(FakeDecider(False), confirm_all=True, label_log=log, answer_timeout=0.05)
        await router.on_dispatch(event=make_event("new thing", msg_id="1"), gateway=GW)
        await router.on_dispatch(event=make_event("more", msg_id="2"), gateway=GW)
        for _ in range(40):
            await asyncio.sleep(0.01)
        self.assertNotIn(key, router.pending)
        self.assertTrue(any(isinstance(m, str) and m.startswith("⏱️ No answer") and "a new topic" in m
                            and "2 messages" in m for m in bridge.sent))
        self.assertEqual(bridge.resets, [key])
        self.assertEqual(bridge.released, [["new thing", "more"]])
        self.assertFalse(os.path.exists(log))

    async def test_unsure_timeout_stays_in_same_topic(self):
        router, bridge, key = build(FakeDecider(None), answer_timeout=0.05)
        await router.on_dispatch(event=make_event("hmm", msg_id="1"), gateway=GW)
        for _ in range(40):
            await asyncio.sleep(0.01)
        self.assertEqual(bridge.resets, [])
        self.assertEqual(bridge.released, [["hmm"]])

    async def test_held_messages_survive_a_restart_and_replay_first(self):
        import tempfile, os
        state = os.path.join(tempfile.mkdtemp(), "held.json")
        router, bridge, key = build(FakeDecider(None), held_state=state)
        await router.on_dispatch(event=make_event("a", msg_id="11"), gateway=GW)
        await router.on_dispatch(event=make_event("b", msg_id="12"), gateway=GW)
        # gateway restarts: a fresh router loads what was held
        router2, bridge2, _ = build(FakeDecider(), held_state=state)
        self.assertEqual(router2.leftovers[key]["ids"], ["11", "12"])
        result = await router2.on_dispatch(event=make_event("c", msg_id="13"), gateway=GW)
        self.assertEqual(result["action"], "skip")
        await settle()
        self.assertTrue(bridge2.sent[0].startswith("♻️ Hermes restarted"))
        self.assertEqual(bridge2.calls, [("replay", ["11", "12"])])
        self.assertEqual([r["action"] for r in bridge2.replay_results], ["allow", "allow"])
        self.assertEqual(bridge2.released, [["c"]])  # after the restored ones
        self.assertEqual(await router2.on_dispatch(event=make_event("c", msg_id="13"), gateway=GW),
                         {"action": "allow"})
        with open(state) as f:
            self.assertEqual(json.load(f), {})

    async def test_answer_clears_saved_state(self):
        import tempfile, os
        state = os.path.join(tempfile.mkdtemp(), "held.json")
        router, bridge, key = build(FakeDecider(None), held_state=state)
        await router.on_dispatch(event=make_event("a", msg_id="11"), gateway=GW)
        await router.on_dispatch(event=make_event("continue", msg_id="12"), gateway=GW)
        with open(state) as f:
            self.assertEqual(json.load(f), {})


class InfoDecider(FakeDecider):
    """Like the local model: a verdict plus p_new in last_info (None = below the confidence guard)."""

    async def decide(self, history, message):
        self.calls.append((history, message))
        verdict, p_new = self.answers.pop(0)
        self.last_info = {"p_new": p_new, "model_ms": 5}
        return verdict


class SafeRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Act fast, never guess when the model is down, keep quick follow-ups together under one undo."""

    def _build(self, decider, **kw):
        router, bridge, key = build(decider, **kw)
        router.act_confirm = True
        return router, bridge, key

    async def _press(self, router, key, answer):
        check = router.checks[key]
        check.choice.response = answer
        check.choice.event.set()
        await settle()

    async def test_model_down_asks_and_waits_without_timeout_or_backup(self):
        router, bridge, key = self._build(FakeDecider(RuntimeError("socket gone")), answer_timeout=0.05)
        self.assertEqual((await router.on_dispatch(event=make_event("hi", msg_id="1"), gateway=GW))["action"], "skip")
        self.assertTrue(router.pending[key].wait)
        self.assertIn("model failed (RuntimeError)", bridge.sent[0][1])
        await router.on_dispatch(event=make_event("hello?", msg_id="2"), gateway=GW)
        for _ in range(20):
            await asyncio.sleep(0.01)
        self.assertIn(key, router.pending)  # still waiting: no timeout guess
        self.assertEqual(bridge.released, [])
        self.assertTrue(any(isinstance(m, str) and "Waiting for your answer" in m and "(2 waiting)" in m
                            for m in bridge.sent))
        await router.on_dispatch(event=make_event("yes", msg_id="3"), gateway=GW)
        self.assertEqual(bridge.released, [["hi", "hello?"]])

    async def test_unreadable_chat_asks_and_waits(self):
        router, bridge, key = self._build(FakeDecider(), answer_timeout=0.05)
        bridge.transcripts[key] = OSError("db")
        self.assertEqual((await router.on_dispatch(event=make_event("hi"), gateway=GW))["action"], "skip")
        self.assertTrue(router.pending[key].wait)

    async def test_unsure_goes_with_the_lean_and_says_so(self):
        router, bridge, key = self._build(InfoDecider((None, 0.3)))
        self.assertIsNone(await router.on_dispatch(event=make_event("hmm"), gateway=GW))
        self.assertTrue(router.checks[key].verdict)  # leaned same topic
        self.assertIn("Same chat? (model: not sure, leans same topic 70%", bridge.sent[0][1])
        self.assertEqual(router.checks[key].label["verdict"], "unsure")
        router2, bridge2, key2 = self._build(InfoDecider((None, 0.6)))
        self.assertEqual((await router2.on_dispatch(event=make_event("hmm"), gateway=GW))["action"], "skip")
        self.assertIs(router2.pending[key2].guess, False)  # leans new: ask first, no reply
        self.assertEqual(bridge2.resets, [])

    async def test_quick_follow_up_joins_the_open_check_and_undo_covers_both(self):
        router, bridge, key = self._build(InfoDecider((True, 0.02), (False, 0.7)))
        await router.on_dispatch(event=make_event("other thing", msg_id="90"), gateway=GW)
        self.assertIsNone(await router.on_dispatch(event=make_event("and also this", msg_id="91"), gateway=GW))
        check = router.checks[key]
        self.assertEqual([e.text for e in check.followers], ["and also this"])
        self.assertEqual(len([m for m in bridge.sent if isinstance(m, tuple)]), 1)  # no second check
        bridge.transcripts[key] = HISTORY + [{"role": "user", "content": "other thing"},
                                             {"role": "assistant", "content": "ok"},
                                             {"role": "user", "content": "and also this"}]
        await self._press(router, key, "No, undo")
        self.assertEqual(bridge.calls, ["stop", ("rewind", "old-sid", 2), ("delete_after", "90")])
        self.assertEqual(bridge.released[-1], ["other thing", "and also this"])

    async def test_new_topic_undo_brings_follow_ups_back_too(self):
        router, bridge, key = self._build(InfoDecider((True, 0.1)))
        await new_check(router, bridge, "other thing", "92")
        self.assertIsNone(await router.on_dispatch(event=make_event("still empty", msg_id="93"), gateway=GW))
        bridge.transcripts[key] = [{"role": "user", "content": "other thing"}]
        await router.on_dispatch(event=make_event("more", msg_id="94"), gateway=GW)
        await self._press(router, key, "Same chat")
        self.assertIn(("switch_back", "old-sid", "new-sid"), bridge.calls)
        self.assertEqual(bridge.released[-1], ["other thing", "still empty", "more"])

    async def test_quick_follow_up_only_splits_when_model_is_very_sure(self):
        router, bridge, key = self._build(InfoDecider((True, 0.02), (False, 0.97)))
        await router.on_dispatch(event=make_event("a", msg_id="1"), gateway=GW)
        self.assertEqual((await router.on_dispatch(event=make_event("totally different", msg_id="2"),
                                                   gateway=GW))["action"], "skip")
        self.assertEqual(bridge.resets, [])  # very sure it's new: ask first
        self.assertEqual(router.pending[key].held[0].text, "totally different")

    async def test_follow_up_while_hermes_is_busy_counts_as_quick(self):
        router, bridge, key = self._build(InfoDecider((False, 0.8)))
        router.last_msg_at[key] = 0
        bridge.is_busy = True
        self.assertIsNone(await router.on_dispatch(event=make_event("btw"), gateway=GW))
        self.assertEqual(bridge.resets, [])
        self.assertNotIn(key, router.checks)

    async def test_check_window_closes_by_itself(self):
        import topic_router.router as R
        router, bridge, key = self._build(InfoDecider((False, 0.99)))
        router.threadify_channels = {"discord": {CHAN}}
        ran = []
        async def fake_threadify(source, old_sid, before_id):
            ran.append((old_sid, before_id))
        router._threadify = fake_threadify
        old, R.CHECK_WINDOW = R.CHECK_WINDOW, 0.05
        try:
            await new_check(router, bridge, "other", "95")
            for _ in range(20):
                await asyncio.sleep(0.01)
        finally:
            R.CHECK_WINDOW = old
        self.assertNotIn(key, router.checks)
        self.assertEqual(ran, [("old-sid", 95)])


class QtFixTests(unittest.IsolatedAsyncioTestCase):
    """Oct 16 fixes: threadify jobs survive failures/restarts; gap rules for unsure verdicts."""

    def _build(self, decider, **kw):
        router, bridge, key = build(decider, **kw)
        router.act_confirm = True
        return router, bridge, key

    async def test_unsure_after_long_quiet_is_new_unless_it_points_back(self):
        router, bridge, key = self._build(InfoDecider((None, 0.45)))
        router.last_msg_at[key] = time.time() - 3 * 3600
        self.assertEqual((await router.on_dispatch(event=make_event("draft an email to my professor"),
                                                   gateway=GW))["action"], "skip")
        self.assertIs(router.pending[key].guess, False)
        self.assertEqual(bridge.resets, [])
        self.assertIn("quiet 2+ h, so new topic", bridge.sent[-1][1])
        router2, bridge2, key2 = self._build(InfoDecider((None, 0.45)))
        router2.last_msg_at[key2] = time.time() - 3 * 3600
        self.assertIsNone(await router2.on_dispatch(event=make_event("move this to the coding channel"), gateway=GW))
        self.assertTrue(router2.checks[key2].verdict)
        self.assertEqual(bridge2.resets, [])

    async def test_unsure_after_short_quiet_still_goes_with_lean(self):
        router, bridge, key = self._build(InfoDecider((None, 0.6)))
        router.last_msg_at[key] = time.time() - 20 * 60
        await router.on_dispatch(event=make_event("hmm"), gateway=GW)
        self.assertIs(router.pending[key].guess, False)

    async def test_follow_up_within_five_minutes_stays(self):
        router, bridge, key = self._build(InfoDecider((False, 0.9)))
        router.last_msg_at[key] = time.time() - 200
        self.assertIsNone(await router.on_dispatch(event=make_event("no, change the wording"), gateway=GW))
        self.assertEqual(bridge.resets, [])
        self.assertNotIn(key, router.checks)

    async def test_last_message_time_survives_restart(self):
        import tempfile, os
        state = os.path.join(tempfile.mkdtemp(), "t.json")
        router, bridge, key = self._build(InfoDecider((True, 0.01)), threadify_state=state)
        await router.on_dispatch(event=make_event("a"), gateway=GW)
        router2, _, _ = self._build(InfoDecider(), threadify_state=state)
        self.assertAlmostEqual(router2.last_msg_at[key], router.last_msg_at[key])

    async def test_open_new_topic_check_survives_restart_as_a_threadify_job(self):
        import tempfile, os
        state = os.path.join(tempfile.mkdtemp(), "t.json")
        router, bridge, key = self._build(InfoDecider((False, 0.99)), threadify_state=state,
                                          threadify={"discord": [CHAN]})
        router.starts["discord:" + CHAN] = 50
        await new_check(router, bridge, "other thing", "81")
        self.assertIn(key, router.checks)
        # Hermes restarts before the owner answers: the old chat (50..81) must still get its thread
        router2, _, _ = self._build(InfoDecider(), threadify_state=state, threadify={"discord": [CHAN]})
        self.assertEqual([(j["start"], j["before"], j["sid"]) for j in router2.jobs], [(50, 81, "old-sid")])
        self.assertEqual(router2.starts["discord:" + CHAN], 81)

    async def test_undo_of_new_topic_forgets_the_deferred_chat(self):
        import tempfile, os
        state = os.path.join(tempfile.mkdtemp(), "t.json")
        router, bridge, key = self._build(InfoDecider((False, 0.99)), threadify_state=state,
                                          threadify={"discord": [CHAN]})
        await new_check(router, bridge, "other thing", "82")
        check = router.checks[key]
        check.choice.response = "Same chat"
        check.choice.event.set()
        await settle()
        self.assertEqual(router.deferred, {})
        router2, _, _ = self._build(InfoDecider(), threadify_state=state, threadify={"discord": [CHAN]})
        self.assertEqual(router2.jobs, [])

    async def test_failed_threadify_is_kept_and_retried_in_order(self):
        import tempfile, os
        state = os.path.join(tempfile.mkdtemp(), "t.json")
        router, bridge, key = build(FakeDecider(), threadify_state=state, threadify={"discord": [CHAN]})
        runs, fail = [], [True]

        async def fake_run(channel, bot_id, start_id, started_at, before_id, title):
            if fail[0]:
                raise AttributeError("'coroutine' object has no attribute 'get'")
            runs.append((start_id, before_id, title))
        router.threadifier.run = fake_run
        bridge.session_meta = lambda sid: (f"title {sid}", 1.0)

        async def chan(source):
            return "chan", 1
        bridge.discord_channel = chan
        router.bridge = bridge
        src = make_event("x").source
        router.starts["discord:" + CHAN] = 10
        await router._threadify(src, "s1", 20)
        await router._threadify(src, "s2", 30)
        self.assertEqual(runs, [])
        self.assertEqual([(j["start"], j["before"]) for j in router.jobs], [(10, 20), (20, 30)])
        # restart, then the fault is fixed: the next message threads both chats, oldest first, separately
        router2, bridge2, key2 = build(FakeDecider(True), threadify_state=state, threadify={"discord": [CHAN]})
        router2.threadifier.run = fake_run
        bridge2.session_meta, bridge2.discord_channel = bridge.session_meta, chan
        fail[0] = False
        await router2.on_dispatch(event=make_event("hello", msg_id="40"), gateway=GW)
        await settle()
        self.assertEqual(runs, [(10, 20, "title s1"), (20, 30, "title s2")])
        self.assertEqual(router2.jobs, [])

    async def test_odd_button_answer_does_not_stop_the_window(self):
        import topic_router.router as R
        router, bridge, key = self._build(InfoDecider((False, 0.99)))
        router.threadify_channels = {"discord": {CHAN}}
        ran = []

        async def fake_threadify(source, old_sid, before_id):
            ran.append((old_sid, before_id))
        router._threadify = fake_threadify
        old, R.CHECK_WINDOW = R.CHECK_WINDOW, 0.1
        try:
            await new_check(router, bridge, "other", "96")
            check = router.checks[key]
            check.choice.response = "Other"
            check.choice.event.set()
            for _ in range(30):
                await asyncio.sleep(0.01)
        finally:
            R.CHECK_WINDOW = old
        self.assertNotIn(key, router.checks)
        self.assertEqual(ran, [("old-sid", 96)])


if __name__ == "__main__":
    unittest.main()


class ReplyModeTests(unittest.IsolatedAsyncioTestCase):
    """A reply-mode chat lives in replies: no threads or dividers; a reply reopens its chat. Oct 21: plain
    messages get the act-then-check prompts back (they were missed)."""

    def _build(self, decider, state=None):
        router, bridge, key = build(decider, reply={"discord": [CHAN]}, reply_state=state,
                                    confirm_all=True, act_confirm=True)
        bridge.router = router
        router.loop = asyncio.get_event_loop()
        return router, bridge, key

    async def test_new_topic_acts_then_checks_and_maps_messages(self):
        router, bridge, key = self._build(InfoDecider((False, 0.9)))
        bridge.transcripts[key] = HISTORY
        router.last_msg_at[key] = time.time() - 3600
        router.open_msgs[key] = ["a1"]  # an earlier message of the old chat
        self.assertIsNone(await router.on_dispatch(event=make_event("new thing", msg_id="b1"), gateway=GW))
        self.assertEqual(len(bridge.resets), 1)
        self.assertNotIn(DIVIDER, bridge.sent)
        self.assertEqual(len(bridge.sent), 1)
        self.assertEqual(bridge.sent[0][0], "BUTTONS")
        self.assertIn("New topic?", bridge.sent[0][1])
        self.assertIn(key, router.checks)
        self.assertEqual(router.msg_sessions, {"a1": "old-sid"})
        bridge.transcripts[key] = HISTORY
        await router.on_dispatch(event=make_event("more", msg_id="b2"), gateway=GW)  # within 5 min: same chat
        self.assertEqual(router.msg_sessions["b1"], "new-sid")
        self.assertEqual(router.open_msgs[key], ["b2"])

    async def test_new_topic_trigger_stays_with_new_chat_when_reset_settles(self):
        router, bridge, key = self._build(InfoDecider((False, 0.9)))
        bridge.transcripts[key] = HISTORY
        router.last_msg_at[key] = time.time() - 3600
        router.open_msgs[key] = ["a1"]

        async def reset(event):  # core fires on_reset with the old session id
            bridge.resets.append(key)
            bridge.sid = "new-sid"
            router.on_reset(session_id="new-sid", old_session_id="old-sid")
        bridge.reset = reset
        await router.on_dispatch(event=make_event("new thing", msg_id="b1"), gateway=GW)
        self.assertEqual(router.msg_sessions, {"a1": "old-sid"})
        self.assertEqual(router.open_msgs[key], ["b1"])

    async def test_same_topic_check_undo_starts_new_without_divider(self):
        router, bridge, key = self._build(InfoDecider((True, 0.05)))
        bridge.transcripts[key] = HISTORY
        router.last_msg_at[key] = time.time() - 3600
        await router.on_dispatch(event=make_event("and another", msg_id="c1"), gateway=GW)
        check = router.checks[key]
        self.assertIn("Same topic?", bridge.sent[0][1])
        await router.resolve_check(key, check, right=False)
        self.assertEqual(len(bridge.resets), 1)
        self.assertNotIn(DIVIDER, bridge.sent)

    async def test_model_failure_asks_and_waits(self):
        router, bridge, key = self._build(FakeDecider(RuntimeError("down")))
        bridge.transcripts[key] = HISTORY
        self.assertEqual(await router.on_dispatch(event=make_event("hi", msg_id="c1"), gateway=GW), SKIP)
        self.assertEqual(bridge.resets, [])
        self.assertIn(key, router.pending)
        self.assertTrue(router.pending[key].wait)

    async def test_unsure_after_long_quiet_is_new_with_a_check(self):
        router, bridge, key = self._build(InfoDecider((None, 0.45)))
        bridge.transcripts[key] = HISTORY
        router.last_msg_at[key] = time.time() - 3 * 3600
        await router.on_dispatch(event=make_event("draft an email to my professor", msg_id="c1"), gateway=GW)
        self.assertEqual(len(bridge.resets), 1)
        self.assertIn("New topic?", bridge.sent[-1][1])

    async def test_reply_elsewhere_closes_the_open_check(self):
        router, bridge, key = self._build(InfoDecider((True, 0.05)))
        bridge.transcripts[key] = HISTORY
        router.last_msg_at[key] = time.time() - 3600
        await router.on_dispatch(event=make_event("and another", msg_id="c1"), gateway=GW)
        self.assertIn(key, router.checks)
        router.msg_sessions["a1"] = "older-sid"
        await router.on_dispatch(event=make_event("back", msg_id="c2", reply_to="a1"), gateway=GW)
        self.assertNotIn(key, router.checks)
        self.assertIn(("switch_back", "older-sid", "old-sid"), bridge.calls)

    async def test_reply_reopens_the_earlier_conversation_without_asking_the_model(self):
        decider = FakeDecider()
        router, bridge, key = self._build(decider)
        router.msg_sessions = {"a1": "older-sid"}
        self.assertIsNone(await router.on_dispatch(event=make_event("back to this", msg_id="c1", reply_to="a1"), gateway=GW))
        self.assertEqual(decider.calls, [])
        self.assertIn(("switch_back", "older-sid", "old-sid"), bridge.calls)
        self.assertEqual(bridge.sent, [])

    async def test_reply_to_current_conversation_just_continues(self):
        decider = FakeDecider()
        router, bridge, key = self._build(decider)
        router.msg_sessions = {"a1": "old-sid"}
        await router.on_dispatch(event=make_event("and also", msg_id="c1", reply_to="a1"), gateway=GW)
        self.assertEqual(decider.calls, [])
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "switch_back" for c in bridge.calls))

    async def test_reply_to_a_hermes_message_follows_its_anchor(self):
        router, bridge, key = self._build(FakeDecider())
        router.msg_sessions = {"a1": "older-sid"}
        bridge.anchors = {"h9": ["zz", "a1"]}
        await router.on_dispatch(event=make_event("yes do that", msg_id="c1", reply_to="h9"), gateway=GW)
        self.assertIn(("switch_back", "older-sid", "old-sid"), bridge.calls)

    async def test_unknown_reply_and_busy_fall_back_safely(self):
        router, bridge, key = self._build(InfoDecider((True, 0.1)))
        await router.on_dispatch(event=make_event("hm", msg_id="c1", reply_to="nope"), gateway=GW)
        self.assertEqual(bridge.resets, [])  # routed by the model instead: same topic
        router.msg_sessions["x"] = "older-sid"
        bridge.is_busy = True
        await router.on_dispatch(event=make_event("hm", msg_id="c2", reply_to="x"), gateway=GW)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "switch_back" for c in bridge.calls))

    async def test_map_survives_restart_and_new_posts_no_divider(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "r.json")
            router, bridge, key = self._build(InfoDecider((True, 0.1)), state=state)
            router.last_msg_at[key] = time.time() - 3600
            await router.on_dispatch(event=make_event("hi", msg_id="c1"), gateway=GW)
            router.loop = asyncio.get_running_loop()
            router.on_reset(session_id="new-sid", old_session_id="old-sid")  # /new
            self.assertNotIn(DIVIDER, bridge.sent)
            router2, _, _ = self._build(FakeDecider(), state=state)
            self.assertEqual(router2.msg_sessions, {"c1": "old-sid"})


class ReplyAnchorBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_hermes_message_anchor_then_earlier_messages(self):
        bot, owner = SimpleNamespace(id=1), SimpleNamespace(id=2)
        msgs = {
            "h2": SimpleNamespace(id=12, author=bot, reference=None),  # 2nd chunk of an answer
            "h1": SimpleNamespace(id=11, author=bot, reference=SimpleNamespace(message_id=10)),
            "s0": SimpleNamespace(id=10, author=owner, reference=None),
        }

        class Chan:
            async def fetch_message(self, mid):
                return {12: msgs["h2"], 10: msgs["s0"]}[mid]

            async def history(self, limit, before):
                for m in (msgs["h1"], msgs["s0"]):
                    yield m

        bridge = GatewayBridge(object(), object())

        async def chan(source):
            return Chan(), 1
        bridge.discord_channel = chan
        self.assertEqual(await bridge.reply_anchor_ids(None, "12", 30), ["11", "10", "10"])
        self.assertEqual(await bridge.reply_anchor_ids(None, "10", 30), [])  # The owner's own, unknown: nothing
