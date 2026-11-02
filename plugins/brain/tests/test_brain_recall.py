"""Offline tests for recall thresholds, meaning (embedding) search, project shared memory and thread projects.

A fake embedder maps words to a few "concepts" so meaning search is deterministic (no model, no daemon).
Run like test_brain.py (Hermes's runtime; unittest discover -s plugins/brain/tests).
"""
import json
import os
import re
import socketserver
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

from test_brain import PLUGIN, Base, FakeLLM, approve_all, load  # noqa: F401  (sets sys.path for the plugin)
import brain_pipeline as bp  # noqa: E402
import brain_store  # noqa: E402
from brain_store import Embedder, cmd_project, find_refs, wiki_chunks  # noqa: E402

CONCEPTS = [
    {"email", "emails", "inbox", "mail", "gmail", "folders", "sorting", "sorted", "promos"},
    {"nas", "zimaboard", "storage", "backup", "backups"},
    {"login", "password", "credentials", "vault"},
    {"garden", "tomatoes", "compost", "watering", "plants", "soil", "beds"},
    {"lights", "lamp", "lamps", "bulb", "dim", "brightness", "bright"},
    {"printer", "jammed", "nozzle", "filament", "positron"},
]


def fake_vec(text):
    """One dimension per concept (count of its words) plus 16 small hashed dims for all other words, so texts
    without any concept still differ from each other."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    v = [float(sum(w in group for w in words)) for group in CONCEPTS] + [0.0] * 16
    for w in words:
        if not any(w in group for group in CONCEPTS):
            v[len(CONCEPTS) + zlib.crc32(w.encode()) % 16] += 0.3
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class FakeEmbedder:
    """Stands in for brain_store.Embedder: same interface, concept vectors, can be switched off."""

    def __init__(self):
        self.sock, self.model, self.up, self.calls = "fake", "fake-model", True, 0

    def available(self):
        return self.up

    def embed(self, texts, mode="passage", timeout=2.0):
        self.calls += 1
        return [fake_vec(t) for t in texts] if self.up else None


class EmbedBase(Base):
    def setUp(self):
        super().setUp()
        self.emb = self.store.embedder = FakeEmbedder()


class HybridSearchTests(EmbedBase):
    def test_meaning_finds_what_keywords_miss(self):
        mail = self.store.add_fact("The owner's mail is split into folders by source account", project="email-system")
        nas = self.store.add_fact("ZimaBoard 2 is the main storage box; its credentials are in the vault")
        self.store.add_fact("The owner's sister is named Dana", "person")
        self.assertEqual(self.store.keyword_search("my inbox is sorting weird"), [])
        hits = self.store.search("my inbox is sorting weird")
        self.assertEqual([f["id"] for f in hits], [mail])
        self.assertFalse(hits[0]["kw"])
        self.assertGreater(hits[0]["sim"], 0.8)
        self.assertEqual(self.store.search("the NAS login")[0]["id"], nas)
        self.assertIn(f"#{mail}", self.store.prefetch("my inbox is sorting weird"))

    def test_hits_found_both_ways_rank_first(self):
        only_kw = self.store.add_fact("The weird router config lives on the old laptop")
        both = self.store.add_fact("Inbox sorting rules: promos go to a folder")
        only_sem = self.store.add_fact("Gmail filters move mail into folders")
        ids = [f["id"] for f in self.store.search("inbox sorting is weird")]
        self.assertEqual(ids[0], both)
        self.assertEqual(set(ids), {only_kw, both, only_sem})

    def test_falls_back_to_keywords_when_embedder_is_down(self):
        a = self.store.add_fact("Garden beds need compost every spring")
        self.store.add_fact("Gmail filters move mail into folders")
        self.emb.up = False
        self.store._qvec.clear()
        rows = self.store.search("garden compost")
        self.assertEqual([f["id"] for f in rows], [a])
        self.assertIsNone(rows[0]["sim"])
        self.assertEqual(self.store.search("my inbox is sorting weird"), [])  # meaning-only hit is simply missing
        self.assertIn("Garden beds", self.store.prefetch("garden compost"))
        # the real client: no socket -> None at once, no exception
        self.store.embedder = Embedder(self.dir / "missing.sock")
        self.assertIsNone(self.store.embedder.embed(["x"], "query"))
        self.assertEqual([f["id"] for f in self.store.search("garden compost")], [a])

    def test_broken_daemon_marks_down_and_keeps_working(self):
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "e.sock"  # short path: AF_UNIX limit

        class Bad(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                self.wfile.write(b"not json\n")
        srv = socketserver.UnixStreamServer(str(sock), Bad)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.store.embedder = emb = Embedder(sock)
        a = self.store.add_fact("Garden beds need compost every spring")  # add still works, just no vector
        self.assertFalse(emb.available())  # failure -> quiet for RETRY seconds
        self.assertEqual([f["id"] for f in self.store.search("garden compost")], [a])
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM vectors")[0]["n"], 0)

    def test_socket_protocol_roundtrip(self):
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "e.sock"

        class Good(socketserver.StreamRequestHandler):
            def handle(self):
                import base64
                from array import array
                req = json.loads(self.rfile.readline())
                vecs = [base64.b64encode(array("f", fake_vec(t)).tobytes()).decode() for t in req["texts"]]
                self.wfile.write((json.dumps({"model": "m1", "dim": 7, "vecs": vecs}) + "\n").encode())
        srv = socketserver.UnixStreamServer(str(sock), Good)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        emb = Embedder(sock)
        out = emb.embed(["garden soil", "inbox"], "passage")
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(sum(a * b for a, b in zip(out[0], fake_vec("garden soil"))), 1.0, places=5)
        self.assertEqual(emb.model, "m1")

    def test_embed_on_add_update_backfill_and_cleanup(self):
        vec = lambda fid: self.store.q("SELECT hash FROM vectors WHERE kind='fact' AND ref=?", (str(fid),))  # noqa: E731
        a = self.store.add_fact("Garden beds need compost")
        h1 = vec(a)[0]["hash"]
        self.store.update_fact(a, "Garden beds need compost and mulch")
        self.assertNotEqual(vec(a)[0]["hash"], h1)
        self.emb.up = False
        b = self.store.add_fact("The printer nozzle jammed twice")
        self.assertEqual(vec(b), [])
        self.emb.up = True
        self.assertEqual(self.store.embed_missing(), 1)
        self.assertEqual(len(vec(b)), 1)
        self.assertEqual(self.store.embed_missing(), 0)  # nothing stale -> no work
        self.store.retire_fact(b)
        self.store.embed_missing()
        self.assertEqual(vec(b), [])


class RecallTests(EmbedBase):
    def test_no_truncation_of_long_facts(self):
        long = "Garden plan: " + " ".join(f"bed{i} gets compost and tomatoes" for i in range(60))
        self.store.add_fact(long)
        out = self.store.prefetch("garden compost tomatoes")
        self.assertIn(long, out)
        self.assertGreater(len(long), 1500)

    def test_soft_limit_grows_for_strong_meaning_hits(self):
        for i in range(20):
            self.store.add_fact(f"Garden note {i}: tomatoes beds compost watering schedule")
        out = self.store.prefetch("tomatoes compost watering")
        self.assertEqual(out.count("Garden note"), brain_store.FACTS_MAX)
        self.assertIn("(+8 more brain matches not shown; call brain_search", out)
        self.emb.up = False  # keyword-only: only the soft default, the rest is counted
        self.store._qvec.clear()
        out = self.store.prefetch("tomatoes compost watering")
        self.assertEqual(out.count("Garden note"), brain_store.FACTS_SOFT)
        self.assertIn("(+15 more brain matches", out)

    def test_threshold_keeps_out_weak_matches(self):
        a = self.store.add_fact("Garden beds need compost")
        self.store.add_fact("The printer nozzle jammed twice")
        self.store.add_fact("The owner's router is in the hall")
        out = self.store.prefetch("garden beds compost")
        self.assertIn(f"#{a}", out)
        self.assertNotIn("printer", out)
        self.assertNotIn("more brain matches", out)
        self.assertEqual(self.store.prefetch("compile the rust project"), "")

    def test_cap_counts_what_was_cut_and_never_splits_a_line(self):
        for i in range(8):
            self.store.add_fact(f"Garden note {i}: tomatoes beds compost " + "x" * 400)
        out = self.store.prefetch("tomatoes beds compost", cap=1500)
        self.assertLessEqual(len(out), 1500)
        self.assertRegex(out, r"\(\+\d+ more brain matches")
        self.assertTrue(all(line.endswith("x") for line in out.splitlines() if line.startswith("- [#")))

    def test_wiki_up_to_five_long_passages(self):
        wiki = self.dir / "wiki"
        (wiki / "projects").mkdir(parents=True)
        for i in range(7):
            body = f"# Garden {i}\n\n## Beds\n" + " ".join(["Tomatoes like compost and steady watering."] * 15)
            (wiki / "projects" / f"garden{i}.md").write_text(body)
        (wiki / "log.md").write_text("# Log\ntomatoes compost watering")  # bookkeeping page: never recalled
        self.store.index_wiki(wiki)
        self.store.embed_missing()
        out = self.store.prefetch("tomatoes compost watering")
        lines = [ln for ln in out.splitlines() if ln.startswith("- projects/")]
        self.assertEqual(len(lines), brain_store.WIKI_MAX)
        self.assertTrue(all(len(ln) > 500 for ln in lines))
        self.assertNotIn("log.md", out)

    def test_wiki_chunks_split_by_heading_and_size(self):
        body = "---\ntitle: X\n---\n# X\n\nIntro line.\n\n## A\n" + ("word " * 400) + "\n\n## B\nshort"
        parts = wiki_chunks(body, size=500)
        self.assertEqual(parts[0], ("X", "Intro line."))
        self.assertTrue(all(len(t) <= 500 for _, t in parts))
        self.assertEqual({h for h, _ in parts}, {"X", "A", "B"})
        self.assertEqual(parts[-1], ("B", "short"))

    def test_old_index_without_chunks_is_rebuilt(self):
        wiki = self.dir / "wiki"
        wiki.mkdir()
        (wiki / "a.md").write_text("# A\ntomatoes")
        self.store.index_wiki(wiki)
        self.store.x("DELETE FROM wiki_chunks")  # as left by the pre-upgrade plugin
        self.assertEqual(self.store.index_wiki(wiki), 1)
        self.assertEqual(self.store.wiki_search("tomatoes")[0]["path"], "a.md")


class ProjectMemoryTests(EmbedBase):
    def notes(self):
        self.store.add_fact("Garden beds get compost in March", "decision", project="garden")
        self.store.set_note("s1", "Planned the tomato beds and the compost schedule.", ["https://github.com/example/garden-planner"],
                            project="garden", title="tomato beds")
        self.store.set_note("s2", "Painted the fence and fixed the gate latch.", ["~/garden/fence.md"], project="garden")
        self.store.set_note("s3", "Compost bin research for the office.", [], project="office")

    def test_project_block_has_relevant_other_chats_with_ids_and_refs(self):
        self.notes()
        out = self.store.prefetch("when do the tomatoes get compost?", project="garden", session_id="s9")
        self.assertIn("Current project: garden", out)
        self.assertIn("session s1 (", out)
        self.assertIn('"tomato beds"', out)
        self.assertIn("https://github.com/example/garden-planner", out)
        self.assertIn("session_search", out)
        self.assertNotIn("session s3", out)  # other project
        self.assertIn("~/garden/fence.md", out)  # project files & links
        self.assertNotIn("session s2", out)  # not relevant to this message
        mine = self.store.prefetch("when do the tomatoes get compost?", project="garden", session_id="s1")
        self.assertNotIn("session s1", mine)  # never echo the current chat
        self.assertIn("Recent other chats", mine)  # nothing relevant -> most recent ones instead

    def test_extract_writes_chat_note_with_project_summary_and_refs(self):
        self.store.queue_messages("s5", [{"role": "user", "content": "Plan the tomato beds, see "
                                          "https://docs.example.com/beds?token=abc123 and ~/garden/plan.md"}],
                                  "session_end", project="Garden", title="beds")
        reply = json.dumps({"project": "garden", "summary": "Planned tomato beds.", "refs": ["github.com/example/beds"],
                            "facts": []})
        llm = FakeLLM(brain_extract=[reply, lambda body: json.dumps(
            {"project": None, "summary": body["previous_summary"] + " Then compost.", "facts": []})])
        bp.run_once(self.store, llm)
        note = self.store.q("SELECT * FROM notes WHERE session_id='s5'")[0]
        self.assertEqual((note["project"], note["title"], note["summary"]), ("garden", "beds", "Planned tomato beds."))
        refs = json.loads(note["refs"])
        self.assertIn("https://docs.example.com/beds", refs)  # query string (token) dropped
        self.assertIn("~/garden/plan.md", refs)
        self.assertIn("github.com/example/beds", refs)
        self.assertFalse(any("token" in r for r in refs))
        self.store.queue_messages("s5", [{"role": "user", "content": "now the compost"}], "session_end", project="garden")
        bp.run_once(self.store, llm)
        self.assertEqual(llm.calls[1][1]["previous_summary"], "Planned tomato beds.")
        self.assertEqual(self.store.q("SELECT summary FROM notes WHERE session_id='s5'")[0]["summary"],
                         "Planned tomato beds. Then compost.")

    def test_find_refs_never_keeps_secrets(self):
        text = ("hook https://discord.com/api/webhooks/123/abcDEF1234567890abcdef1234567890XYZ ok, "
                "https://user:pw" "@" "host.example/x, https://example.com/page?key=s3cret#frag, run ./scripts/sync.sh")
        self.assertEqual(find_refs(text), ["https://example.com/page", "./scripts/sync.sh"])

    def test_brain_search_project_filter_and_chats(self):
        self.notes()
        self.store.add_fact("Office compost bin is under the desk", project="office")
        mod = load("brain_provider_recall_test", PLUGIN / "__init__.py")
        p = mod.BrainProvider()
        p.initialize("s9", hermes_home=str(self.dir), agent_context="primary")
        p.store.embedder = self.emb
        res = json.loads(p.handle_tool_call("brain_search", {"query": "compost", "project": "garden"}))
        self.assertEqual({f["project"] for f in res["facts"]}, {"garden"})
        self.assertEqual([c["session_id"] for c in res["chats"]], ["s1"])
        res = json.loads(p.handle_tool_call("brain_search", {"query": "compost"}))
        self.assertEqual({f["project"] for f in res["facts"]}, {"garden", "office"})
        self.assertIn("s3", [c["session_id"] for c in res["chats"]])


class ThreadProjectTests(EmbedBase):
    CFG = {"channel_projects": {"111": "home-assistant", "#tasks-reminders": "tasks"},
           "auto_project_channels": ["222", "#research"]}

    def setUp(self):
        super().setUp()
        (self.dir / "channel_directory.json").write_text(json.dumps({"platforms": {"discord": [
            {"id": "111", "name": "home-assistant", "type": "channel"},
            {"id": "222", "name": "projects", "type": "channel"},
            {"id": "333", "name": "vault", "type": "channel"}]}}))
        for patcher in (mock.patch.dict(os.environ, {"HERMES_HOME": str(self.dir)}),
                        mock.patch("brain_store.settings", return_value=self.CFG)):
            patcher.start()
            self.addCleanup(patcher.stop)
        brain_store._dir_cache.clear()
        for t in ("Office lamp is an IKEA floor lamp", "Bedroom lights dim to 20% at night", "Hall bulb is a Hue bulb"):
            self.store.add_fact(t, project="home-assistant")
        for t in ("Garden beds get compost", "Tomatoes need watering daily"):
            self.store.add_fact(t, project="garden")
        self.mod = load("brain_provider_thread_test", PLUGIN / "__init__.py")

    def provider(self, chat_id, chat_name, session="s1"):
        p = self.mod.BrainProvider()
        p.initialize(session, hermes_home=str(self.dir), agent_context="primary", chat_id=chat_id, chat_type="thread",
                     chat_name=chat_name, gateway_session_key=f"agent:discord:thread:{chat_id}:{chat_id}")
        p.store.embedder = self.emb
        return p

    def test_thread_inherits_parent_channel_project(self):
        self.assertEqual(self.store.resolve_project("s1", "", "900", chat_name="G / #home-assistant / fix it"),
                         "home-assistant")
        self.assertEqual(self.store.resolve_project("s1", "", "901", chat_name="G / #tasks-reminders / x"), "tasks")
        self.assertEqual(self.store.resolve_project("s1", "", "902", parent="111"), "home-assistant")
        self.assertEqual(self.store.resolve_project("s1", "", "903", chat_name="G / #vault / x"), "")

    def test_mixed_channel_thread_auto_picks_by_meaning(self):
        p = self.provider("777", "G / #projects / Lamp too bright")
        out = p.prefetch("can we make the lamp dim after 10pm?")
        self.assertIn("Current project: home-assistant", out)
        self.assertEqual(self.store.get_project("thread:777"), "home-assistant")
        p2 = self.provider("777", "G / #projects / Lamp too bright", session="s2")  # a later chat in the thread
        self.assertEqual(p2.project(), "home-assistant")

    def test_unclear_thread_stays_unassigned_and_stops_trying(self):
        before = self.store.projects()
        p = self.provider("778", "G / #research / Thoughts on philosophy")
        for msg in ("what is a good life?", "and what about free will?", "more on ethics", "the lamp is nice"):
            p.prefetch(msg)
        self.assertEqual(self.store.get_project("thread:778"), "")  # 4th message would match, but 3 tries are used up
        self.assertEqual(self.store.projects(), before)  # never invents a project

    def test_project_command_overrides_auto_pick(self):
        p = self.provider("779", "G / #projects / Lamp too bright")
        p.prefetch("dim the lamp please")
        self.assertEqual(p.project(), "home-assistant")
        cmd_project(self.store, "garden", p.session_key)
        self.assertEqual(p.project(), "garden")
        self.assertIn("Current project: garden", cmd_project(self.store, "", p.session_key, "s1", "779", "",
                                                             "G / #projects / Lamp too bright"))

    def test_name_match_when_embedder_is_down(self):
        self.emb.up = False
        p = self.provider("780", "G / #projects / home assistant automations")
        p.prefetch("what automations should I add?")
        self.assertEqual(self.store.get_project("thread:780"), "home-assistant")

    def test_threads_outside_mixed_channels_are_not_auto_picked(self):
        p = self.provider("781", "G / #vault / lamp password")
        p.prefetch("dim the lamp")
        self.assertEqual(self.store.q("SELECT * FROM sessions WHERE session_id='thread:781'"), [])

    def test_queued_chunks_carry_project_and_title(self):
        p = self.provider("782", "G / #home-assistant / Hall bulb")
        p.on_session_end([{"role": "user", "content": "the hall bulb flickers"}])
        row = self.store.q("SELECT project, title FROM chunks")[0]
        self.assertEqual(row, {"project": "home-assistant", "title": "Hall bulb"})


if __name__ == "__main__":
    unittest.main()
