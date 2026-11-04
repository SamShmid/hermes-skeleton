"""brain memory provider: recall facts + wiki before each turn, queue conversations for the automatic
extract -> approve pipeline (run by the brain-ops plugin's worker), and expose brain_search /
brain_remember. Activate with ``memory.provider: brain``.

Settings (``plugins.entries.brain.settings`` in config.yaml):
  owner_name       how prompts and tool descriptions refer to the person (default "the user")
  owner_pronouns   optional, e.g. "they/them"
  channel_projects {chat_id or "#name": project} defaults for chats with no /project set; threads inherit
                   their parent channel's
  auto_project_channels [chat_id or "#name"] mixed channels: each new thread picks the existing project that
                   clearly matches its title/first message (by meaning), else stays unassigned
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
from brain_store import KINDS, Store, channel_setting, owner_name, parent_channel  # noqa: E402

log = logging.getLogger("brain")
FLUSH_EVERY_TURNS = 20

def search_schema() -> Dict[str, Any]:
    owner = owner_name()
    return {
        "name": "brain_search",
        "description": f"Search the long-term brain (durable facts about {owner}, people, projects, decisions, "
                       f"preferences) and the wiki. Use before asking {owner} something they may have told you before.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look up (keywords or a question; matched by meaning too)."},
            "project": {"type": "string", "description": "Only facts and chats of this project (optional)."},
            "limit": {"type": "integer", "description": "Max facts (default 10)."}}, "required": ["query"]},
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
        self.session_id = self.session_key = self.chat_id = self.chat_name = self.chat_type = self.title = ""
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
        self.chat_name = str(kwargs.get("chat_name") or "")
        self.chat_type = str(kwargs.get("chat_type") or "")
        self.title = str(kwargs.get("session_title") or "") or self.chat_name.split(" / ")[-1]
        self.writable = kwargs.get("agent_context", "primary") == "primary"

    def system_prompt_block(self) -> str:
        return ("Long-term brain: relevant facts, wiki passages and, in a project, the project's other chats "
                "(summaries with session ids and files/links) are recalled automatically each turn. Use brain_search "
                "(optionally with project) for more, session_search with a session id to open one of those chats, and "
                "brain_remember to propose a durable fact (an approver checks it automatically).")

    def parent(self) -> str:
        """Parent channel of this thread: the gateway's id when it is bound to this chat, else from the chat name."""
        if self.chat_type != "thread":
            return ""
        if _session("HERMES_SESSION_CHAT_ID") == self.chat_id and _session("HERMES_SESSION_PARENT_CHAT_ID"):
            return _session("HERMES_SESSION_PARENT_CHAT_ID")
        return parent_channel(self.chat_name)

    def project(self, text: str = "") -> str:
        """This chat's project; a thread in a mixed channel with none yet tries to auto-pick one from ``text``."""
        if not self.store:
            return ""
        parent = self.parent()
        project = self.store.resolve_project(self.session_id, self.session_key, self.chat_id, parent, self.chat_name)
        if not project and text and self.writable and parent and channel_setting("auto_project_channels", "", parent):
            project = self.store.auto_project(self.chat_id, f"{self.title}\n{text}")
        return project

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self.store or is_trivial_prompt(query):
            return ""
        return self.store.prefetch(query, self.project(query), session_id=session_id or self.session_id)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [search_schema(), remember_schema()]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "brain_search":
                q, project = str(args.get("query") or ""), str(args.get("project") or "")
                facts = self.store.search(q, limit=int(args.get("limit") or 10), project=project or None)
                chats = self.store.chats(q, project, {self.session_id}, limit=5, min_overlap=1)
                return json.dumps({"facts": [{k: f[k] for k in ("id", "text", "kind", "entity", "project", "updated")}
                                             for f in facts],
                                   "chats": [{"session_id": n["session_id"], "project": n["project"], "title": n["title"],
                                              "summary": n["summary"], "refs": json.loads(n["refs"] or "[]"),
                                              "updated": n["updated"]} for n in chats],
                                   "wiki": [{k: w[k] for k in ("path", "title", "snip")} for w in self.store.wiki_search(q, 5)]},
                                  ensure_ascii=False)
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
                self.store.queue_messages(self.session_id, messages, source, project=self.project(), title=self.title)
            except Exception as exc:
                log.warning("brain could not queue conversation: %s", exc)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name) or "")
    except Exception:
        return ""


def register(ctx) -> None:
    ctx.register_memory_provider(BrainProvider())
