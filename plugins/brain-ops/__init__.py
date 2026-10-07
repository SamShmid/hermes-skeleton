"""brain-ops: the parts of the brain that must live once per process, which an exclusive memory
plugin cannot provide: /brain and /project commands, the brain_extract / brain_approve auxiliary LLM
tasks, and the background worker that runs the automatic extract -> approve pipeline (gateway only)."""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain"))  # sibling provider dir
import brain_pipeline  # noqa: E402
from brain_store import Store, cmd_brain, cmd_project  # noqa: E402

log = logging.getLogger("brain")
_worker = {"thread": None, "stop": threading.Event()}


def make_llm(llm_facade, timeout=180.0):
    """Adapt ctx.llm to the pipeline's ``llm(task, system, user) -> text``."""
    def call(task, system, user):
        result = llm_facade.complete([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                     task=task, timeout=timeout, purpose=f"brain:{task}")
        return result.text
    return call


def worker_loop(store_factory, llm, stop, interval=60.0):
    store, backoff = store_factory(), interval
    store.x("UPDATE chunks SET status='new' WHERE status='working'")  # crashed mid-run last time
    while not stop.is_set():
        try:
            store.index_wiki()
            busy = brain_pipeline.run_once(store, llm)
            backoff = interval
        except Exception as exc:
            log.warning("brain worker pass failed: %s", exc)
            busy, backoff = 0, min(backoff * 2, 3600)
        stop.wait(1 if busy else backoff)


def _session(name):
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name)
    except Exception:
        return ""


def register(ctx):
    for task, label, desc in (("brain_extract", "Brain extractor", "Extracts durable facts from chats for the brain."),
                              ("brain_approve", "Brain approver", "Accepts or rejects proposed brain changes.")):
        ctx.register_auxiliary_task(task, display_name=label, description=desc)
    ctx.register_command("brain", lambda raw: cmd_brain(Store.open(), raw),
                         description="Hermes brain: what it remembers (try /brain help)",
                         args_hint="[help | search <words> | forget <id> | projects]")
    ctx.register_command("project", lambda raw: cmd_project(
        Store.open(), raw, _session("HERMES_SESSION_KEY"), _session("HERMES_SESSION_ID"),
        _session("HERMES_SESSION_CHAT_ID")), description="Show or set this chat project (try /project help)", args_hint="[name | none | help]")
    if sys.argv[1:3] == ["gateway", "run"] and (_worker["thread"] is None or not _worker["thread"].is_alive()):
        from agent.memory_provider import spawn_context_thread
        _worker["stop"].clear()
        _worker["thread"] = spawn_context_thread(worker_loop, name="brain-worker",
                                                 args=(Store.open, make_llm(ctx.llm), _worker["stop"]))
        _worker["thread"].start()
        ctx.on_unload(_worker["stop"].set)
