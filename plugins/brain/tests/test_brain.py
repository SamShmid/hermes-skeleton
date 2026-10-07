"""Offline tests for the brain plugins and brain scripts (fake LLM, temp dirs).

Needs Hermes importable (``agent.memory_provider``); run through bin/test.sh, or with Hermes's runtime:
    HERMES_HOME=$(mktemp -d) ~/.hermes/hermes-agent/.hermes/bin/hermes --run-module unittest discover -s plugins/brain/tests -v
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent
OPS = PLUGIN.parent / "brain-ops"
SCRIPTS = PLUGIN.parent.parent / "scripts"  # repo: plugins/brain + scripts/; deployed: $HERMES_HOME/{plugins/brain,scripts}
sys.path.insert(0, str(PLUGIN))
import brain_pipeline as bp  # noqa: E402
import brain_store  # noqa: E402
from brain_store import Store, cmd_brain, cmd_project  # noqa: E402


REAL_SETTINGS = brain_store.settings


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=None)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeLLM:
    """Replies by task from queued strings or callables; records calls."""

    def __init__(self, **replies):
        self.replies, self.calls = {k: list(v) for k, v in replies.items()}, []

    def __call__(self, task, system, user):
        self.calls.append((task, json.loads(user)))
        r = self.replies[task].pop(0)
        return r(json.loads(user)) if callable(r) else r


def approve_all(payload):
    return json.dumps({"decisions": [{"id": c["id"], "approve": True, "reason": "durable"} for c in payload["candidates"]]})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(str(self.dir / "brain" / "brain.db"))
        patcher = mock.patch("brain_store.settings", return_value={})  # never read a real config.yaml
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()


class StoreTests(Base):
    def test_add_search_dedupe_update_retire_and_log(self):
        fid = self.store.add_fact("Alex prefers dark roast coffee", "preference", project="Home Life")
        self.assertEqual(self.store.add_fact("alex prefers dark roast coffee!", "preference"), fid)  # exact dup
        self.assertEqual([f["id"] for f in self.store.search("what coffee does Alex like")], [fid])
        self.assertEqual(self.store.fact(fid)["project"], "home-life")
        self.assertTrue(self.store.update_fact(fid, "Alex prefers light roast coffee", source="t"))
        self.assertTrue(self.store.retire_fact(fid, source="t"))
        self.assertFalse(self.store.retire_fact(fid))
        self.assertEqual(self.store.search("coffee"), [])
        self.assertEqual([c["action"] for c in self.store.q("SELECT action FROM changes ORDER BY id")],
                         ["added", "updated", "retired"])

    def test_fts_handles_punctuation_and_empty(self):
        self.store.add_fact("Hermes runs on the homelab box in container 7")
        self.assertEqual(self.store.search('"; DROP TABLE facts; -- homelab?'), self.store.search("homelab"))
        self.assertEqual(self.store.search("the and of"), [])

    def test_queue_messages_is_idempotent_and_text_only(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "I moved to Brooklyn"},
                {"role": "assistant", "content": "Noted"}, {"role": "tool", "content": "x"},
                {"role": "assistant", "content": [{"type": "text"}]}]
        self.assertEqual(self.store.queue_messages("s1", msgs, "end"), 1)
        self.assertEqual(self.store.queue_messages("s1", msgs, "end"), 0)
        chunk = self.store.q("SELECT text FROM chunks")[0]["text"]
        self.assertIn("User: I moved to Brooklyn", chunk)
        self.assertNotIn("sys", chunk)
        with mock.patch("brain_store.settings", return_value={"owner_name": "Alex"}):
            self.store.queue_messages("s2", [{"role": "user", "content": "hello there"}], "end")
        self.assertIn("Alex: hello there", self.store.q("SELECT text FROM chunks WHERE session_id='s2'")[0]["text"])

    def test_project_resolution_order(self):
        with mock.patch("brain_store.settings", return_value={"channel_projects": {"123": "Quick Thoughts"}}):
            self.assertEqual(self.store.resolve_project("s1", "k1", "123"), "quick-thoughts")
            self.store.set_project("s1", "hermes")
            self.assertEqual(self.store.resolve_project("s1", "k1", "123"), "hermes")
            self.store.set_project("key:k1", "taxes")
            self.assertEqual(self.store.resolve_project("s1", "k1", "123"), "taxes")

    def test_wiki_index_and_search(self):
        wiki = self.dir / "wiki"
        (wiki / "concepts").mkdir(parents=True)
        (wiki / "concepts" / "garden.md").write_text("# Garden\nTomatoes need full sun and deep watering.")
        (wiki / "raw").mkdir()
        (wiki / "raw" / "dump.md").write_text("# Raw\ntomatoes tomatoes")
        self.assertEqual(self.store.index_wiki(wiki), 1)
        self.assertEqual(self.store.index_wiki(wiki), 0)  # unchanged -> skipped
        hits = self.store.wiki_search("how much sun do tomatoes need")
        self.assertEqual(hits[0]["path"], "concepts/garden.md")


class OwnerConfigTests(Base):
    def test_defaults_are_generic(self):
        self.assertEqual(brain_store.owner_name(), "the user")
        self.assertIn("long-term memory (\"brain\") of the user.", bp.extract_prompt())
        self.assertNotIn("pronouns", bp.approve_prompt())

    def test_owner_settings_flow_into_prompts(self):
        with mock.patch("brain_store.settings", return_value={"owner_name": "Alex", "owner_pronouns": "they/them"}):
            self.assertIn("Alex, who uses they/them pronouns", bp.extract_prompt())
            self.assertIn("Alex's preferences", bp.extract_prompt())
            self.assertIn("Alex, who uses they/them pronouns", bp.approve_prompt())
            mod = load("brain_provider_owner_test", PLUGIN / "__init__.py")
            self.assertIn("about Alex", mod.search_schema()["description"])

    def test_settings_reads_brain_entry(self):
        home = self.dir / "home"
        home.mkdir()
        (home / "config.yaml").write_text("plugins:\n  entries:\n    brain:\n      settings:\n        owner_name: Kim\n")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            brain_store._settings_cache.clear()
            self.assertEqual(REAL_SETTINGS(), {"owner_name": "Kim"})
            brain_store._settings_cache.clear()


class PrefetchTests(Base):
    def test_empty_when_nothing_relevant(self):
        self.store.add_fact("Alex's sister is named Dana", "person")
        self.assertEqual(self.store.prefetch("compile the rust project"), "")

    def test_cap_and_sections(self):
        for i in range(40):
            self.store.add_fact(f"Garden note {i}: tomatoes beds compost watering schedule " + "x" * 300, project="garden")
        self.store.add_fact("Hermes project uses Discord and Telegram", "project", project="hermes")
        out = self.store.prefetch("tomatoes compost watering", project="hermes")
        self.assertLessEqual(len(out), 2400)
        self.assertIn("Current project: hermes", out)
        self.assertIn("Relevant facts:", out)
        self.assertLessEqual(out.count("Garden note"), 5)


class PipelineTests(Base):
    def chunk(self, text="User: I decided to move Hermes to the mini PC for good.\n\nHermes: Done."):
        self.store.x("INSERT INTO chunks(session_id,text,source,created) VALUES('s1',?, 'end', 'now')", (text,))

    def test_extract_then_approve_applies_and_maps_project(self):
        self.chunk()
        extract = json.dumps({"project": "Hermes", "facts": [
            {"action": "add", "text": "Alex moved Hermes to the mini PC permanently.", "kind": "decision",
             "entity": "Hermes", "project": "", "target_fact_id": None, "evidence": "move Hermes to the mini PC"}]})
        llm = FakeLLM(brain_extract=[extract], brain_approve=[approve_all])
        self.assertGreater(bp.run_once(self.store, llm), 0)
        facts = self.store.q("SELECT * FROM facts")
        self.assertEqual(len(facts), 1)
        self.assertEqual((facts[0]["kind"], facts[0]["project"]), ("decision", "hermes"))
        self.assertEqual(self.store.get_project("s1"), "hermes")
        self.assertEqual(self.store.q("SELECT status FROM chunks")[0]["status"], "done")
        self.assertEqual(llm.calls[1][1]["candidates"][0]["evidence"], "move Hermes to the mini PC")

    def test_extract_dedupes_and_targets_updates(self):
        fid = self.store.add_fact("Alex lives in Queens", "fact")
        self.chunk("Alex: I live in Brooklyn now.")
        extract = json.dumps({"project": None, "facts": [
            {"action": "add", "text": "Alex lives in Queens", "kind": "fact"},
            {"action": "update", "text": "Alex lives in Brooklyn", "kind": "fact", "target_fact_id": fid},
            {"action": "retire", "text": "", "target_fact_id": 999}]})
        llm = FakeLLM(brain_extract=[extract], brain_approve=[approve_all])
        bp.run_once(self.store, llm)
        self.assertEqual([f["id"] for f in llm.calls[0][1]["existing_facts"]], [fid])
        self.assertEqual(len(llm.calls[1][1]["candidates"]), 1)  # dup add + bad retire dropped
        self.assertEqual(self.store.fact(fid)["text"], "Alex lives in Brooklyn")

    def test_reject_and_edited_text(self):
        a = self.store.add_candidate("Alex is tired today", source="t")
        b = self.store.add_candidate("alex likes   jazz", "preference", source="t")
        reply = json.dumps({"decisions": [{"id": a, "approve": False, "reason": "transient"},
                                          {"id": b, "approve": True, "reason": "ok", "text": "Alex likes jazz."}]})
        bp.run_once(self.store, FakeLLM(brain_approve=[reply]))
        self.assertEqual(self.store.q("SELECT status, reason FROM candidates WHERE id=?", (a,))[0],
                         {"status": "rejected", "reason": "transient"})
        self.assertEqual(self.store.q("SELECT text FROM facts")[0]["text"], "Alex likes jazz.")

    def test_malformed_approver_rejects_all(self):
        ids = [self.store.add_candidate(f"Fact {i}", source="t") for i in range(3)]
        with self.assertLogs("brain", "WARNING"):
            bp.run_once(self.store, FakeLLM(brain_approve=['```json\n{"decisions": []}\n```']))
        self.assertEqual({r["status"] for r in self.store.q("SELECT status FROM candidates")}, {"rejected"})
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM facts")[0]["n"], 0)
        self.assertEqual(len(ids), 3)

    def test_missing_verdict_rejects_only_that_item(self):
        a = self.store.add_candidate("Alex owns a cat named Miso", "fact", source="t")
        b = self.store.add_candidate("Alex owns a dog", "fact", source="t")
        bp.run_once(self.store, FakeLLM(brain_approve=[json.dumps({"decisions": [{"id": a, "approve": True}]})]))
        self.assertEqual(self.store.q("SELECT status FROM candidates WHERE id=?", (b,))[0]["status"], "rejected")
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM facts")[0]["n"], 1)

    def test_malformed_extract_retries_then_fails(self):
        self.chunk()
        llm = FakeLLM(brain_extract=["not json"] * 3)
        for _ in range(3):
            with self.assertLogs("brain", "WARNING"):
                bp.run_once(self.store, llm)
        self.assertEqual(self.store.q("SELECT status, tries FROM chunks")[0], {"status": "failed", "tries": 3})

    def test_llm_exception_leaves_candidates_waiting(self):
        self.store.add_candidate("Alex likes tea", source="t")

        def boom(_):
            raise TimeoutError("network")
        with self.assertRaises(TimeoutError):
            bp.run_once(self.store, FakeLLM(brain_approve=[boom]))
        self.assertEqual(self.store.q("SELECT status FROM candidates")[0]["status"], "new")

    def test_memory_write_mirror_and_seed(self):
        fid = self.store.add_fact("Alex uses Bitwarden", source="seed")
        bp.mirror_memory_write(self.store, "replace", "user", "Alex uses 1Password", "Alex uses Bitwarden")
        bp.mirror_memory_write(self.store, "remove", "memory", "", "Alex uses Bitwarden")
        bp.mirror_memory_write(self.store, "add", "user", "Alex is vegetarian")
        rows = self.store.q("SELECT action, target_fact_id, kind FROM candidates ORDER BY id")
        self.assertEqual(rows, [{"action": "update", "target_fact_id": fid, "kind": "preference"},
                                {"action": "retire", "target_fact_id": fid, "kind": "fact"},
                                {"action": "add", "target_fact_id": None, "kind": "preference"}])
        mem = self.dir / "memories"
        mem.mkdir()
        (mem / "USER.md").write_text("Alex is in New York.\n§\nAlex prefers short replies.")
        self.assertEqual(bp.seed_from_memory_files(self.store, mem), 2)
        self.assertEqual(bp.seed_from_memory_files(self.store, mem), 0)


class CommandTests(Base):
    def test_brain_commands(self):
        fid = self.store.add_fact("Alex's favorite editor is Zed", project="tools")
        self.assertIn("1 facts", cmd_brain(self.store, ""))
        self.assertIn("➕ Alex's favorite editor is Zed  `#%d`" % fid, cmd_brain(self.store, ""))
        self.assertIn(f"#{fid}", cmd_brain(self.store, "search editor"))
        self.assertEqual(cmd_brain(self.store, f"forget #{fid}"), f"Retired fact #{fid}.")
        self.assertEqual(cmd_brain(self.store, f"forget {fid}"), f"No active fact #{fid}.")
        self.assertIn("Usage", cmd_brain(self.store, "forget x"))
        self.assertIn("Commands", cmd_brain(self.store, "bogus"))
        self.assertEqual(self.store.q("SELECT source FROM changes ORDER BY id DESC LIMIT 1")[0]["source"], "owner")

    def test_project_command(self):
        with mock.patch("brain_store.settings", return_value={}):
            self.assertIn("(none", cmd_project(self.store, "", "agent:discord:1"))
            self.assertIn("my-taxes", cmd_project(self.store, "My Taxes", "agent:discord:1"))
            self.assertIn("Current project: my-taxes", cmd_project(self.store, "", "agent:discord:1"))
            cmd_project(self.store, "none", "agent:discord:1")
            self.assertIn("(none", cmd_project(self.store, "", "agent:discord:1"))


class ProviderTests(Base):
    def setUp(self):
        super().setUp()
        self.mod = load("brain_provider_test", PLUGIN / "__init__.py")
        self.p = self.mod.BrainProvider()
        self.p.initialize("s1", hermes_home=str(self.dir), platform="discord", agent_context="primary")
        self.p.store.add_fact("Alex's partner is named Robin", "person")

    def test_tools_and_prefetch(self):
        self.assertEqual({s["name"] for s in self.p.get_tool_schemas()}, {"brain_search", "brain_remember"})
        res = json.loads(self.p.handle_tool_call("brain_search", {"query": "Robin"}))
        self.assertEqual(res["facts"][0]["text"], "Alex's partner is named Robin")
        res = json.loads(self.p.handle_tool_call("brain_remember", {"text": "Robin likes hiking", "kind": "person"}))
        self.assertTrue(res["ok"])
        self.assertEqual(self.p.store.q("SELECT COUNT(*) n FROM facts")[0]["n"], 1)  # never a direct write
        self.assertIn("Robin", self.p.prefetch("what does my partner Robin like?"))
        self.assertEqual(self.p.prefetch("thanks!"), "")

    def test_hooks_queue_and_non_primary_is_read_only(self):
        msgs = [{"role": "user", "content": "Remember I quit coffee"}]
        self.p.on_pre_compress(msgs)
        self.p.on_session_end(msgs + [{"role": "assistant", "content": "Got it"}])
        self.assertEqual(self.p.store.q("SELECT COUNT(*) n FROM chunks")[0]["n"], 2)
        cron = self.mod.BrainProvider()
        cron.initialize("c1", hermes_home=str(self.dir), agent_context="cron")
        cron.on_session_end([{"role": "user", "content": "cron stuff"}])
        cron.on_memory_write("add", "user", "x")
        self.assertFalse(json.loads(cron.handle_tool_call("brain_remember", {"text": "y"}))["ok"])
        self.assertEqual(self.p.store.q("SELECT COUNT(*) n FROM chunks")[0]["n"], 2)
        self.assertEqual(self.p.store.q("SELECT COUNT(*) n FROM candidates")[0]["n"], 0)


class OpsTests(Base):
    def test_worker_pass_uses_ctx_llm_task_slots(self):
        ops = load("brain_ops_test", OPS / "__init__.py")
        facade = mock.Mock()
        facade.complete.return_value = mock.Mock(text=json.dumps({"decisions": []}))
        self.store.add_candidate("Alex likes tea", source="t")
        stop, real = threading.Event(), bp.run_once

        def one_pass(store, llm):
            stop.set()
            return real(store, llm)
        with mock.patch.object(self.store, "index_wiki"), mock.patch.object(ops.brain_pipeline, "run_once", one_pass):
            ops.worker_loop(lambda: self.store, ops.make_llm(facade), stop, interval=0.01)
        self.assertEqual(facade.complete.call_args.kwargs["task"], "brain_approve")
        self.assertEqual(self.store.q("SELECT status FROM candidates")[0]["status"], "rejected")


class ScriptTests(Base):
    def test_report_and_cli(self):
        scripts = SCRIPTS
        report = load("brain_report_test", scripts / "brain_report.py")
        cli = load("brain_cli_test", scripts / "brain_cli.py")
        self.assertEqual(report.report(self.dir), "")  # nothing yet -> silent
        src = self.dir / "c.json"
        src.write_text(json.dumps([{"text": "Alex reads sci-fi", "kind": "preference"}, {"text": "Alex runs Mondays"}]))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(len(cli.main(["add-candidate", "--json", str(src)], store=self.store)["candidate_ids"]), 2)
            fid = cli.main(["add-fact", "--text", "Alex's cat is Miso", "--project", "Home"], store=self.store)["fact_ids"][0]
            self.assertEqual(cli.main(["stats"], store=self.store)["candidates_new"], 2)
        self.store.update_fact(fid, "Alex's cat is named Miso", source="t")
        text = report.report(self.dir)
        self.assertIn("1 added, 1 updated, 0 retired", text)
        self.assertIn("home:", text)
        self.assertIn("(was: Alex's cat is Miso)", text)
        later = datetime(2099, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(report.report(self.dir, now=later), "")

    def test_report_wiki_git(self):
        report = load("brain_report_test2", SCRIPTS / "brain_report.py")
        wiki = self.dir / "wiki"
        wiki.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(wiki), *a], check=True, capture_output=True)  # noqa: E731
        run("init", "-q")
        (wiki / "a.md").write_text("# A")
        run("add", ".")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "wiki: add A")
        self.assertIn("wiki: add A", report.report(self.dir))


if __name__ == "__main__":
    unittest.main()
