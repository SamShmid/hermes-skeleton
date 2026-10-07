"""brain memory provider: recall facts + wiki before each turn, queue conversations for the automatic
extract -> approve pipeline (run by the brain-ops plugin's worker), and expose brain_search /
brain_remember. Activate with ``memory.provider: brain``.

Settings (``plugins.entries.brain.settings`` in config.yaml):
  owner_name       how prompts and tool descriptions refer to the person (default "the user")
  owner_pronouns   optional, e.g. "they/them"
  channel_projects {chat_id: project} defaults for chats with no /project set
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider, is_trivial_prompt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_pipeline  # noqa: E402
from brain_store import KINDS, Store, owner_name  # noqa: E402

log = logging.getLogger("brain")
FLUSH_EVERY_TURNS = 20

def search_schema() -> Dict[str, Any]:
    owner = owner_name()
    return {
        "name": "brain_search",
        "description": f"Search the long-term brain (durable facts about {owner}, people, projects, decisions, "
                       f"preferences) and the wiki. Use before asking {owner} something they may have told you before.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords to look up."},
            "limit": {"type": "integer", "description": "Max facts (default 8)."}}, "required": ["query"]},
    }


def remember_schema() -> Dict[str, Any]:
    return {
        "name": "brain_remember",
        "description": f"Propose a durable fact for the brain about {owner_name()} (a preference, decision, person, "
                       "project state or ongoing commitment). It is reviewed automatically by an approver; nothing is "
                       "stored directly.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "One short self-contained sentence."},
            "kind": {"type": "string", "enum": list(KINDS)},
            "project": {"type": "string", "description": "Project name, if it belongs to one."}}, "required": ["text"]},
    }


class BrainProvider(MemoryProvider):
    def __init__(self):
        self.store = None
        self.session_id = self.session_key = self.chat_id = ""
        self.writable = True
        self.turns = 0

    @property
    def name(self) -> str:
        return "brain"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self.store = Store.open(Path(kwargs.get("hermes_home") or "~/.hermes").expanduser() / "brain" / "brain.db")
        self.session_id = session_id or ""
        self.session_key = str(kwargs.get("gateway_session_key") or "")
        self.chat_id = str(kwargs.get("chat_id") or "")
        self.writable = kwargs.get("agent_context", "primary") == "primary"

    def system_prompt_block(self) -> str:
        return ("Long-term brain: relevant facts are recalled automatically each turn. Use brain_search to look "
                "things up and brain_remember to propose a durable fact (an approver checks it automatically).")

    def project(self) -> str:
        return self.store.resolve_project(self.session_id, self.session_key, self.chat_id) if self.store else ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self.store or is_trivial_prompt(query):
            return ""
        return self.store.prefetch(query, self.project())

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [search_schema(), remember_schema()]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "brain_search":
                q = str(args.get("query") or "")
                facts = self.store.search(q, limit=int(args.get("limit") or 8))
                return json.dumps({"facts": [{k: f[k] for k in ("id", "text", "kind", "entity", "project", "updated")}
                                             for f in facts],
                                   "wiki": self.store.wiki_search(q, 3)}, ensure_ascii=False)
            if tool_name == "brain_remember":
                if not self.writable:
                    return json.dumps({"ok": False, "error": "brain is read-only in this context"})
                cid = self.store.add_candidate(str(args.get("text") or ""), str(args.get("kind") or "fact"),
                                               project=str(args.get("project") or self.project()),
                                               source=f"tool:{self.session_id}", evidence="proposed by Hermes in chat")
                return json.dumps({"ok": True, "candidate_id": cid,
                                   "note": "Queued; an approver will accept or reject it automatically."})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": False, "error": f"unknown tool {tool_name}"})

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "", messages=None, **kwargs) -> None:
        self.turns += 1
        if self.writable and messages and self.turns % FLUSH_EVERY_TURNS == 0:
            self._queue(messages, "turns")

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        self._queue(messages, "compress")
        return ""

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        self._queue(messages, "session_end")

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, **kwargs):
        if parent_session_id and self.store and self.writable and not reset:  # compression continuation keeps project
            project = self.store.get_project(parent_session_id)
            if project and not self.store.get_project(new_session_id):
                self.store.set_project(new_session_id, project)
        self.session_id = new_session_id
        self.turns = 0

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        if self.writable and self.store:
            brain_pipeline.mirror_memory_write(self.store, action, target, content,
                                               str((metadata or {}).get("previous_content") or ""))

    def _queue(self, messages, source) -> None:
        if self.writable and self.store and messages:
            try:
                self.store.queue_messages(self.session_id, messages, source)
            except Exception as exc:
                log.warning("brain could not queue conversation: %s", exc)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []


def register(ctx) -> None:
    ctx.register_memory_provider(BrainProvider())
