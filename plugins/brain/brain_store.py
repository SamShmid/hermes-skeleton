"""Brain: SQLite + FTS5 store for facts, candidates, chat->project map, change log and wiki index.

Shared by the ``brain`` memory provider, the ``brain-ops`` plugin (commands + worker) and the
scripts in $HERMES_HOME/scripts. Plain sqlite3, no Hermes imports.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("fact", "person", "project", "decision", "preference", "writing_sample", "episode")
ACTIONS = ("add", "update", "retire")
PREFETCH_CHARS = 2400  # ~600 tokens

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL, kind TEXT DEFAULT 'fact',
  entity TEXT DEFAULT '', project TEXT DEFAULT '', source TEXT DEFAULT '', created TEXT, updated TEXT,
  status TEXT DEFAULT 'active');
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, entity, project, content=facts, content_rowid=id);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text, entity, project) VALUES (new.id, new.text, new.entity, new.project); END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, entity, project) VALUES ('delete', old.id, old.text, old.entity, old.project); END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, entity, project) VALUES ('delete', old.id, old.text, old.entity, old.project);
  INSERT INTO facts_fts(rowid, text, entity, project) VALUES (new.id, new.text, new.entity, new.project); END;
CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, text TEXT NOT NULL, kind TEXT DEFAULT 'fact',
  entity TEXT DEFAULT '', project TEXT DEFAULT '', source TEXT DEFAULT '', action TEXT DEFAULT 'add',
  target_fact_id INTEGER, evidence TEXT DEFAULT '', status TEXT DEFAULT 'new', reason TEXT DEFAULT '',
  created TEXT, decided TEXT);
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, project TEXT, title TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY, ts TEXT, action TEXT, fact_id INTEGER,
  project TEXT, text TEXT, old_text TEXT, source TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, session_id TEXT, text TEXT, source TEXT,
  created TEXT, status TEXT DEFAULT 'new', tries INTEGER DEFAULT 0, error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS seen (hash TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(path UNINDEXED, title, body);
"""

STOP = set("""a an and are as at be been but by can could did do does for from had has have how i if in into is it
its just me my no not of on or our please should so that the their them then there these they this to up us was we
were what when where which who why will with would you your about also any some like want need get got make know
tell yes ok okay hi hey thanks""".split())


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (text or "").lower())).strip()


def slug(project) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(project or "").lower()).strip("-")[:40]


def tokens(text: str) -> list:
    seen, out = set(), []
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(t) > 1 and t not in STOP and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def fts_query(text: str, limit: int = 12) -> str:
    """OR-query of the query's words; for long text, its most frequent longer words."""
    if len(text or "") > 500:
        from collections import Counter
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3 and w not in STOP]
        picked = [w for w, _ in Counter(words).most_common(limit)]
    else:
        picked = tokens(text)[:limit]
    return " OR ".join(f'"{t}"' for t in picked)


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


_settings_cache: dict = {}


def _load_yaml(text: str):
    """PyYAML when present, else ruamel.yaml (Hermes's own runtime ships ruamel, not PyYAML)."""
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        from ruamel.yaml import YAML
        return YAML(typ="safe", pure=True).load(text)


def settings() -> dict:
    """plugins.entries.brain.settings from config.yaml, re-read when the file changes ({} on any problem)."""
    try:
        path = home() / "config.yaml"
        mtime = path.stat().st_mtime
        if _settings_cache.get("mtime") != mtime:
            cfg = _load_yaml(path.read_text()) or {}
            entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("brain") or {}
            _settings_cache.update(mtime=mtime, value=entry.get("settings") or {})
        return _settings_cache["value"]
    except Exception:
        return {}


def owner_name() -> str:
    """How prompts refer to the person the brain is about (setting ``owner_name``; default "the user")."""
    return str(settings().get("owner_name") or "").strip() or "the user"


def owner_pronouns() -> str:
    """Optional pronouns for prompts (setting ``owner_pronouns``, e.g. "they/them"); "" when unset."""
    return str(settings().get("owner_pronouns") or "").strip()


def owner_intro() -> str:
    """'<owner>' or '<owner>, who uses <pronouns> pronouns' for LLM prompts."""
    pronouns = owner_pronouns()
    return f"{owner_name()}, who uses {pronouns} pronouns" if pronouns else owner_name()


def speaker_label() -> str:
    """Label for the owner's lines in queued transcripts: the configured name, else "User"."""
    return str(settings().get("owner_name") or "").strip() or "User"


class Store:
    _cache: dict = {}
    _guard = threading.Lock()

    @classmethod
    def open(cls, path=None) -> "Store":
        path = str(Path(path or home() / "brain" / "brain.db"))
        with cls._guard:
            if path not in cls._cache:
                cls._cache[path] = cls(path)
            return cls._cache[path]

    def __init__(self, path: str):
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def q(self, sql, args=()) -> list:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    # -- facts ---------------------------------------------------------------------------------
    def fact(self, fid):
        rows = self.q("SELECT * FROM facts WHERE id=?", (fid,))
        return rows[0] if rows else None

    def find_exact(self, text):
        n = norm(text)
        for f in self.search(text, limit=10):
            if norm(f["text"]) == n:
                return f
        return None

    def add_fact(self, text, kind="fact", entity="", project="", source="", reason="") -> int:
        dup = self.find_exact(text)
        if dup:
            return dup["id"]
        t = now()
        with self.lock:
            fid = self.x("INSERT INTO facts(text,kind,entity,project,source,created,updated) VALUES(?,?,?,?,?,?,?)",
                         (text.strip(), kind if kind in KINDS else "fact", entity or "", slug(project), source, t, t)).lastrowid
            self.log("added", fid, slug(project), text, "", source, reason)
        return fid

    def update_fact(self, fid, text, source="", reason="", **fields) -> bool:
        f = self.fact(fid)
        if not f or f["status"] != "active":
            return False
        kind = fields.get("kind") if fields.get("kind") in KINDS else f["kind"]
        project = slug(fields["project"]) if fields.get("project") else f["project"]
        entity = fields.get("entity") or f["entity"]
        with self.lock:
            self.x("UPDATE facts SET text=?, kind=?, entity=?, project=?, updated=? WHERE id=?",
                   (text.strip(), kind, entity, project, now(), fid))
            self.log("updated", fid, project, text, f["text"], source, reason)
        return True

    def retire_fact(self, fid, source="", reason="") -> bool:
        f = self.fact(fid)
        if not f or f["status"] != "active":
            return False
        with self.lock:
            self.x("UPDATE facts SET status='retired', updated=? WHERE id=?", (now(), fid))
            self.log("retired", fid, f["project"], f["text"], "", source, reason)
        return True

    def log(self, action, fid, project, text, old_text, source, reason):
        self.x("INSERT INTO changes(ts,action,fact_id,project,text,old_text,source,reason) VALUES(?,?,?,?,?,?,?,?)",
               (now(), action, fid, project or "", text, old_text or "", source or "", reason or ""))

    def search(self, query, limit=10, project=None, min_overlap=1) -> list:
        """Active facts ranked by FTS5 bm25; ``min_overlap`` = query words a fact must share."""
        mq = fts_query(query)
        if not mq:
            return []
        sql = ("SELECT f.* FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid WHERE facts_fts MATCH ? "
               "AND f.status='active'" + (" AND f.project=?" if project else "") + " ORDER BY bm25(facts_fts) LIMIT ?")
        try:
            rows = self.q(sql, (mq, project, limit * 3) if project else (mq, limit * 3))
        except sqlite3.OperationalError:
            return []
        qt = set(tokens(query))
        need = min(min_overlap, len(qt))
        rows = [r for r in rows if len(qt & set(tokens(r["text"] + " " + r["entity"] + " " + r["project"]))) >= need]
        return rows[:limit]

    def project_facts(self, project, limit=5) -> list:
        return self.q("SELECT * FROM facts WHERE status='active' AND project=? ORDER BY "
                      "(kind IN ('project','decision')) DESC, updated DESC LIMIT ?", (slug(project), limit))

    def projects(self) -> list:
        rows = self.q("SELECT project FROM facts WHERE project!='' AND status='active' UNION "
                      "SELECT project FROM sessions WHERE project!='' AND project IS NOT NULL")
        return sorted({r["project"] for r in rows})

    # -- candidates & chunks ---------------------------------------------------------------------
    def add_candidate(self, text, kind="fact", entity="", project="", source="", action="add",
                      target_fact_id=None, evidence="") -> int:
        if not (text or "").strip() and action != "retire":
            raise ValueError("candidate text is empty")
        return self.x("INSERT INTO candidates(text,kind,entity,project,source,action,target_fact_id,evidence,created) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      ((text or "").strip(), kind if kind in KINDS else "fact", entity or "", slug(project), source,
                       action if action in ACTIONS else "add", target_fact_id, (evidence or "")[:400], now())).lastrowid

    def decide(self, cid, status, reason="") -> bool:
        """Set a new candidate's verdict; False if someone else already decided it."""
        return self.x("UPDATE candidates SET status=?, reason=?, decided=? WHERE id=? AND status='new'",
                      (status, (reason or "")[:300], now(), cid)).rowcount == 1

    def queue_messages(self, session_id, messages, source, max_chars=12000) -> int:
        """Queue unseen user/assistant text from ``messages`` as extraction chunks. Idempotent."""
        lines, me = [], speaker_label()
        for m in messages or []:
            content = m.get("content") if isinstance(m, dict) else None
            if m.get("role") not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            h = hashlib.sha1(f"{session_id}|{m['role']}|{content}".encode()).hexdigest()
            if self.x("INSERT OR IGNORE INTO seen(hash) VALUES(?)", (h,)).rowcount:
                lines.append(f"{me if m['role'] == 'user' else 'Hermes'}: {content.strip()[:2000]}")
        chunks, cur = [], ""
        for line in lines:
            if cur and len(cur) + len(line) > max_chars:
                chunks.append(cur)
                cur = ""
            cur += line + "\n\n"
        if cur.strip():
            chunks.append(cur)
        for c in chunks:
            self.x("INSERT INTO chunks(session_id,text,source,created) VALUES(?,?,?,?)", (session_id, c, source, now()))
        return len(chunks)

    # -- projects -----------------------------------------------------------------------------
    def set_project(self, session_id, project, title=None):
        self.x("INSERT INTO sessions(session_id,project,title,updated) VALUES(?,?,?,?) ON CONFLICT(session_id) DO "
               "UPDATE SET project=excluded.project, title=COALESCE(excluded.title, sessions.title), updated=excluded.updated",
               (session_id, slug(project), title, now()))

    def get_project(self, session_id) -> str:
        rows = self.q("SELECT project FROM sessions WHERE session_id=?", (session_id,))
        return (rows[0]["project"] or "") if rows else ""

    def resolve_project(self, session_id="", session_key="", chat_id="") -> str:
        """Manual /project for this chat, else the extractor's guess for the session, else channel default."""
        for sid in (f"key:{session_key}" if session_key else "", session_id):
            project = self.get_project(sid) if sid else ""
            if project:
                return project
        return slug((settings().get("channel_projects") or {}).get(str(chat_id), "")) if chat_id else ""

    # -- wiki ---------------------------------------------------------------------------------
    def index_wiki(self, wiki=None, force=False) -> int:
        wiki = Path(wiki or home() / "wiki")
        wiki.mkdir(parents=True, exist_ok=True)
        files = [p for p in sorted(wiki.rglob("*.md"))
                 if not any(part.startswith((".", "_")) or part == "raw" for part in p.relative_to(wiki).parts[:-1])]
        sig = json.dumps([(str(p), p.stat().st_mtime) for p in files])
        old = self.q("SELECT value FROM meta WHERE key='wiki_sig'")
        if not force and old and old[0]["value"] == sig:
            return 0
        with self.lock:
            self.x("DELETE FROM wiki_fts")
            for p in files:
                body = p.read_text(errors="replace")
                m = re.search(r"^#\s+(.+)$", body, re.M)
                self.x("INSERT INTO wiki_fts(path,title,body) VALUES(?,?,?)",
                       (str(p.relative_to(wiki)), m.group(1).strip() if m else p.stem, body))
            self.x("INSERT OR REPLACE INTO meta(key,value) VALUES('wiki_sig',?)", (sig,))
        return len(files)

    def wiki_search(self, query, limit=2) -> list:
        mq = fts_query(query)
        if not mq:
            return []
        try:
            return self.q("SELECT path, title, snippet(wiki_fts, 2, '', '', ' ... ', 24) AS snip FROM wiki_fts "
                          "WHERE wiki_fts MATCH ? ORDER BY bm25(wiki_fts) LIMIT ?", (mq, limit))
        except sqlite3.OperationalError:
            return []

    # -- recall -------------------------------------------------------------------------------
    def prefetch(self, query, project="", cap=PREFETCH_CHARS) -> str:
        """Context block for one turn: project facts, top matching facts, wiki snippets. "" if none."""
        proj = self.project_facts(project) if project else []
        seen = {f["id"] for f in proj}
        hits = [f for f in self.search(query, limit=8, min_overlap=2) if f["id"] not in seen][:5]
        wiki = self.wiki_search(query, 2)
        if not (proj or hits or wiki):
            return ""
        out = ["## Brain (auto-recalled background; may be outdated)"]
        if proj:
            out.append(f"Current project: {project}")
            out += [f"- [#{f['id']} {f['kind']}] {f['text']}" for f in proj]
        if hits:
            out.append("Relevant facts:")
            out += [f"- [#{f['id']} {f['kind']}{'/' + f['project'] if f['project'] else ''}] {f['text']}" for f in hits]
        if wiki:
            out.append("Wiki:")
            out += [f"- {w['path']} ({w['title']}): {' '.join(w['snip'].split())}" for w in wiki]
        text = ""
        for line in out:
            line = line if len(line) <= 400 else line[:397] + "..."
            if len(text) + len(line) + 1 > cap:
                break
            text += line + "\n"
        return text.strip()

    def stats(self) -> dict:
        one = lambda sql: self.q(sql)[0]["n"]  # noqa: E731
        return {"facts": one("SELECT COUNT(*) n FROM facts WHERE status='active'"),
                "retired": one("SELECT COUNT(*) n FROM facts WHERE status='retired'"),
                "candidates_new": one("SELECT COUNT(*) n FROM candidates WHERE status='new'"),
                "approved": one("SELECT COUNT(*) n FROM candidates WHERE status='approved'"),
                "rejected": one("SELECT COUNT(*) n FROM candidates WHERE status='rejected'"),
                "chunks_pending": one("SELECT COUNT(*) n FROM chunks WHERE status IN ('new','working')"),
                "projects": len(self.projects()),
                "wiki_pages": one("SELECT COUNT(*) n FROM wiki_fts")}


# -- slash command handlers (wired by the brain-ops plugin) ---------------------------------
BRAIN_HELP = """**Hermes's brain**: what Hermes remembers about you, your projects and your setup.
It works on its own. Hermes pulls durable facts out of your chats, a separate approver agent accepts or rejects each one, and (if you schedule scripts/brain_report.py) a daily report lists what changed. Before each reply Hermes looks up the few facts that matter for what you're talking about.

**Commands**
`/brain`: how much is stored, and the last 10 changes
`/brain search <words>`: find facts (each one shows its #id)
`/brain forget <id>`: remove a fact that's wrong or outdated
`/brain projects`: list projects and how many facts each has
`/brain help`: this message

**Projects**
Each chat belongs to a project (like `email-system` or `home-assistant`). That decides which facts Hermes pulls in first.
`/project`: show this chat's project and the known projects
`/project <name>`: put this chat on a project
`/project none`: clear it and go back to automatic
If you don't set one, Hermes guesses from the conversation (some channels have a default)."""


def cmd_brain(store: Store, raw: str) -> str:
    parts = (raw or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if sub in ("help", "?"):
        return BRAIN_HELP
    if sub == "projects":
        rows = store.q("SELECT project, count(*) n FROM facts WHERE status='active' AND project!='' "
                       "GROUP BY project ORDER BY n DESC")
        return "Projects (active facts):\n" + "\n".join(f"• {r['project']}: {r['n']}" for r in rows) \
            if rows else "No projects yet."
    if sub == "search":
        rows = store.search(rest, limit=10)
        return "\n".join(f"• {_short(f['text'], 160)}  `#{f['id']}`" for f in rows) or "No matching facts."
    if sub == "forget":
        if not rest.lstrip("#").isdigit():
            return "Usage: /brain forget <fact id>"
        fid = int(rest.lstrip("#"))
        return f"Retired fact #{fid}." if store.retire_fact(fid, source="owner", reason="/brain forget") \
            else f"No active fact #{fid}."
    if sub:
        return BRAIN_HELP
    s = store.stats()
    icon = {"added": "➕", "updated": "✏️", "retired": "➖"}
    lines = [f"🧠 **{s['facts']} facts** · {s['projects']} projects · {s['wiki_pages']} wiki pages", "", "**Recent changes**"]
    for c in store.q("SELECT * FROM changes ORDER BY id DESC LIMIT 5"):
        lines.append(f"{icon.get(c['action'], '•')} {_short(c['text'])}  `#{c['fact_id']}`")
    if len(lines) == 3:
        lines.append("Nothing yet.")
    lines += ["", "-# /brain help for commands"]
    return "\n".join(lines)


def _short(text: str, limit: int = 80) -> str:
    """One clean line for Discord: cut at a word boundary and escape markdown characters."""
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return re.sub(r"([_*~`|>])", r"\\\1", text)


def cmd_project(store: Store, raw: str, session_key: str, session_id: str = "", chat_id: str = "") -> str:
    name = (raw or "").strip()
    key = session_key or "cli"
    if name.lower() in ("help", "?"):
        return BRAIN_HELP.split("**Projects**", 1)[1].strip()
    if not name:
        cur = store.resolve_project(session_id, key, chat_id)
        known = ", ".join(store.projects()) or "none yet"
        return (f"Current project: {cur or '(none: Hermes guesses automatically)'}\nKnown projects: {known}\n"
                "Set with `/project <name>`, clear with `/project none`, more with `/brain help`.")
    if name.lower() in ("none", "clear", "-"):
        store.set_project(f"key:{key}", "")
        return "Project cleared for this chat."
    store.set_project(f"key:{key}", name)
    return f"This chat is now mapped to project: {slug(name)}"
