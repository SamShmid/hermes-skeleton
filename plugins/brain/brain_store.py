"""Brain: SQLite + FTS5 store for facts, candidates, chat->project map, change log, wiki index, chat
summaries ("notes") and embedding vectors for meaning search.

Shared by the ``brain`` memory provider, the ``brain-ops`` plugin (commands + worker) and the
scripts in $HERMES_HOME/scripts. Plain sqlite3, no Hermes imports. Meaning search talks to the local
embedder daemon (brain_embed_server.py, Unix socket next to brain.db); without it everything falls
back to keyword (FTS5) search.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import threading
import time
from array import array
from datetime import datetime, timezone
from operator import mul
from pathlib import Path

KINDS = ("fact", "person", "project", "decision", "preference", "writing_sample", "episode")
ACTIONS = ("add", "update", "retire")
PREFETCH_CHARS = 9000  # ~2.3k tokens; a soft ceiling, normal turns use far less
FACTS_SOFT, FACTS_MAX = 5, 12  # past FACTS_SOFT only strong meaning hits get in
WIKI_MAX, WIKI_CHUNK = 5, 900  # wiki snippets per turn; max chars per wiki chunk (= snippet length)
PROJECT_FACTS, PROJECT_CHATS = 8, 4
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
# Meaning-hit rule for bge-small-en-v1.5, calibrated on the real brain. Its cosines are compressed (unrelated text
# scores 0.4-0.65 depending on the query), so a hit must stand out from the query's own background: z-score
# (cosine vs. the mean/sd over all items of that kind) >= Z_MIN and cosine >= SIM_MIN, or cosine >= SIM_HIGH.
# Past the soft fact limit only "strong" hits (z >= Z_STRONG or cosine >= SIM_HIGH) are added.
SIM_MIN, SIM_HIGH, Z_MIN, Z_STRONG = 0.60, 0.76, 2.6, 3.0
BASE_MEAN, BASE_SD = 0.47, 0.06  # background stats used while a kind has too few items (< 30) for its own
WIKI_SKIP = ("index.md", "log.md", "SCHEMA.md")  # wiki bookkeeping pages: indexed as pages, not recalled as passages
PICK_MIN, PICK_MARGIN, PICK_Z = 0.60, 0.04, 2.2  # thread auto-project: best project's cosine, lead, z-score
RRF_K = 60  # reciprocal rank fusion constant

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
CREATE TABLE IF NOT EXISTS wiki_chunks (ref TEXT PRIMARY KEY, path TEXT, title TEXT, heading TEXT, body TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_chunks_fts USING fts5(ref UNINDEXED, path UNINDEXED, title, heading, body);
CREATE TABLE IF NOT EXISTS notes (session_id TEXT PRIMARY KEY, project TEXT DEFAULT '', title TEXT DEFAULT '',
  summary TEXT DEFAULT '', refs TEXT DEFAULT '[]', updated TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(session_id UNINDEXED, title, summary, refs);
CREATE TABLE IF NOT EXISTS vectors (kind TEXT, ref TEXT, model TEXT, hash TEXT, vec BLOB, PRIMARY KEY(kind, ref));
"""

STOP = set("""a an and are as at be been but by can could did do does for from had has have how i if in into is it
its just me my no not of on or our please should so that the their them then there these they this to up us was we
were what when where which who why will with would you your about also any some like want need get got make know
tell yes ok okay hi hey thanks""".split())

URL_RE = re.compile(r"https?://[^\s<>(){}\"'`\]\[]+")
PATH_RE = re.compile(r"(?<![\w/.])(?:~|/home|/Users|/etc|/opt|/srv|/mnt|/var|\.{1,2})/[\w.\-/@+]+")
FILE_RE = re.compile(r"(?<![\w.@/-])[\w\-]+\.(?:py|md|ya?ml|json|pdf|docx?|xlsx?|pptx?|csv|sh|ts|js|txt|toml|ini|conf|html|ipynb)(?![\w@/-]|\.\w)")
OPAQUE_RE = re.compile(r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}")  # token-like segment


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


def overlap_ok(query_tokens: set, text: str, min_overlap: int, ratio: float = 0.0) -> bool:
    """``text`` shares at least ``min_overlap`` words with the query, or ``ratio`` of them (at most 4; never more
    than the query has)."""
    n = len(query_tokens)
    need = min(max(min_overlap, math.ceil(ratio * n)), max(min_overlap, 4), n)
    return len(query_tokens & set(tokens(text))) >= need


def find_refs(text: str, limit: int = 20) -> list:
    """Links, paths, repos and file names mentioned in ``text``. URL query strings/fragments are dropped and
    anything with a token-like segment is skipped, so secrets in links never get stored."""
    out = []
    for m in URL_RE.findall(text or "") + PATH_RE.findall(text or "") + FILE_RE.findall(text or ""):
        ref = clean_ref(m)
        if ref and ref not in out and not any(r.endswith("/" + ref) for r in out):  # bare name of a listed path
            out.append(ref)
    return out[:limit]


def clean_ref(ref) -> str:
    ref = re.split(r"[?#]", str(ref or "").strip(), maxsplit=1)[0].rstrip(".,;:!'\")")[:200]
    return "" if len(ref) < 4 or OPAQUE_RE.search(ref) or re.match(r"\w+://[^/]*@", ref) else ref  # no user:pass@


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


_settings_cache: dict = {}
_dir_cache: dict = {}


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


def parent_channel(chat_name: str) -> str:
    """Parent channel of a Discord thread from its chat name ("Guild / #parent / thread"): the channel id from
    $HERMES_HOME/channel_directory.json when listed, else "#parent". "" when the name has no #channel part."""
    m = re.search(r"(?:^|/ )#([^/]+?)(?: / |$)", chat_name or "")
    if not m:
        return ""
    name = m.group(1).strip()
    try:
        path = home() / "channel_directory.json"
        mtime = path.stat().st_mtime
        if _dir_cache.get("mtime") != mtime:
            data = json.loads(path.read_text())
            _dir_cache.update(mtime=mtime, ids={c.get("name"): str(c.get("id")) for chans in (data.get("platforms") or {}).values()
                                                for c in chans if c.get("type") == "channel"})
        return _dir_cache["ids"].get(name) or f"#{name}"
    except Exception:
        return f"#{name}"


def channel_setting(key: str, chat_id="", parent="") -> object:
    """Look up ``chat_id`` then the thread's ``parent`` channel in setting ``key`` (a dict or list). Keys may be
    channel ids or "#name"s. Returns the dict value / True for a list hit, else None."""
    table = settings().get(key) or {}
    names = [str(chat_id), str(parent)] + ([parent.lstrip("#")] if str(parent).startswith("#") else [])
    for k in filter(None, names):
        if isinstance(table, dict) and k in table:
            return table[k]
        if isinstance(table, (list, tuple)) and k in {str(x) for x in table}:
            return True
    return None


class Embedder:
    """Client for the local embedder daemon (brain_embed_server.py) on a Unix socket. Returns None on any
    problem so callers fall back to keywords; after a failure it stays quiet for RETRY seconds so a dead or
    slow daemon never slows a turn."""
    RETRY = 15.0

    def __init__(self, sock):
        self.sock, self.model, self.down_until = str(sock), EMBED_MODEL, 0.0

    def available(self) -> bool:
        return time.monotonic() >= self.down_until and os.path.exists(self.sock)

    def embed(self, texts, mode="passage", timeout=2.0):
        if not self.available():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(self.sock)
                s.sendall((json.dumps({"mode": mode, "texts": list(texts)}) + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    part = s.recv(1 << 20)
                    if not part:
                        break
                    buf += part
            out = json.loads(buf)
            vecs = [array("f", base64.b64decode(v)).tolist() for v in out["vecs"]]
            if len(vecs) != len(texts):
                raise ValueError("embedder returned the wrong number of vectors")
            self.model = out.get("model") or self.model
            return vecs
        except Exception:
            self.down_until = time.monotonic() + self.RETRY
            return None


def _h(text: str) -> str:
    return hashlib.sha1((text or "").encode()).hexdigest()[:16]


def wiki_chunks(body: str, size: int = WIKI_CHUNK) -> list:
    """Split a wiki page (frontmatter dropped) into (heading, text) pieces of at most ``size`` chars,
    breaking at headings, then paragraphs, then lines, then words."""
    body = re.sub(r"\A---\n.*?\n---\n", "", body or "", flags=re.S)
    out, heading, cur = [], "", ""

    def flush():
        nonlocal cur
        if cur.strip():
            out.append((heading, cur.strip()))
        cur = ""
    for para in re.split(r"\n\s*\n", body):
        lines = []
        for line in para.strip().splitlines():
            m = re.match(r"#{1,6}\s+(.+)", line)
            if m:  # a heading starts a new piece
                if lines:
                    cur, lines = _pack(cur, "\n".join(lines), size, out, heading), []
                flush()
                heading = m.group(1).strip()
            else:
                lines.append(line)
        if lines:
            cur = _pack(cur, "\n".join(lines), size, out, heading)
    flush()
    return out


def _pack(cur: str, para: str, size: int, out: list, heading: str) -> str:
    """Append ``para`` to the running piece ``cur``, emitting full pieces into ``out``; returns the new ``cur``."""
    if len(cur) + len(para) + 2 <= size:
        return f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        out.append((heading, cur.strip()))
    while len(para) > size:  # one oversized paragraph: cut at the last line break / space before ``size``
        cut = max(para.rfind("\n", 0, size), para.rfind(" ", 0, size))
        cut = cut if cut > size // 2 else size
        out.append((heading, para[:cut].strip()))
        para = para[cut:].strip()
    return para


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
        for col in ("project", "title"):  # chunks gained the chat's project/title (for chat notes)
            try:
                self.db.execute(f"ALTER TABLE chunks ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # already there
        self.embedder = Embedder(Path(path).parent / "embed.sock")  # the daemon's socket lives next to the DB
        self._vec_cache: dict = {}
        self._qvec: dict = {}

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
        for f in self.keyword_search(text, limit=10):
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
        self.embed_items("fact", [(fid, text.strip())], timeout=10)  # best effort; the worker catches up
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
        self.embed_items("fact", [(fid, text.strip())], timeout=10)
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

    def keyword_search(self, query, limit=10, project=None, min_overlap=1) -> list:
        """Active facts ranked by FTS5 bm25; ``min_overlap`` = query words a fact must share."""
        mq = fts_query(query)
        if not mq:
            return []
        sql = ("SELECT f.* FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid WHERE facts_fts MATCH ? "
               "AND f.status='active'" + (" AND f.project=?" if project else "") + " ORDER BY bm25(facts_fts) LIMIT ?")
        try:
            rows = self.q(sql, (mq, slug(project), limit * 3) if project else (mq, limit * 3))
        except sqlite3.OperationalError:
            return []
        qt = set(tokens(query))
        return [r for r in rows if overlap_ok(qt, r["text"] + " " + r["entity"] + " " + r["project"], min_overlap)][:limit]

    def search(self, query, limit=10, project=None, min_overlap=1) -> list:
        """Active facts matching ``query`` by keywords (``min_overlap`` shared words) or by meaning (see
        similar()), merged by reciprocal rank fusion. Keyword-only while the embedder is down. Each row also
        carries ``sim``/``z`` (cosine and its z-score, None if not a meaning hit) and ``kw`` (keyword hit)."""
        kw = self.keyword_search(query, limit * 3, project, min_overlap)
        allowed = {str(r["id"]) for r in self.q("SELECT id FROM facts WHERE status='active'" +
                                                (" AND project=?" if project else ""), (slug(project),) if project else ())}
        sem = self.similar("fact", query, allowed=allowed)[:limit * 3]

        def load(refs):
            marks = ",".join("?" * len(refs))
            return {str(r["id"]): r for r in self.q(f"SELECT * FROM facts WHERE id IN ({marks})", [int(x) for x in refs])}
        return fuse(kw, sem, lambda r: str(r["id"]), load, limit)

    def project_facts(self, project, limit=5) -> list:
        return self.q("SELECT * FROM facts WHERE status='active' AND project=? ORDER BY "
                      "(kind IN ('project','decision')) DESC, updated DESC LIMIT ?", (slug(project), limit))

    def projects(self) -> list:
        rows = self.q("SELECT project FROM facts WHERE project!='' AND status='active' UNION "
                      "SELECT project FROM sessions WHERE project!='' AND project IS NOT NULL")
        return sorted({r["project"] for r in rows})

    # -- meaning search (embeddings) -------------------------------------------------------------
    def qvec(self, query):
        """Unit vector for a search query (cached for repeated lookups in one turn); None if the embedder is down."""
        key = (query or "")[:2000]
        if key not in self._qvec:
            out = self.embedder.embed([key], "query", timeout=1.0) if key.strip() else None
            if not out:
                return None
            if len(self._qvec) > 64:
                self._qvec.clear()
            self._qvec[key] = out[0]
        return self._qvec[key]

    def vectors(self, kind) -> dict:
        """{ref: vector} for ``kind`` ("fact" | "wiki" | "note"), cached until the table changes."""
        model = self.embedder.model
        sig = tuple(self.q("SELECT count(*) n, max(rowid) m, total(rowid) t FROM vectors WHERE kind=? AND model=?",
                           (kind, model))[0].values()) + (model,)
        hit = self._vec_cache.get(kind)
        if not hit or hit[0] != sig:
            rows = self.q("SELECT ref, vec FROM vectors WHERE kind=? AND model=?", (kind, model))
            hit = self._vec_cache[kind] = (sig, {r["ref"]: array("f", r["vec"]).tolist() for r in rows})
        return hit[1]

    def similar(self, kind, query, min_sim=None, allowed=None) -> list:
        """[(ref, cosine, z)] of ``kind`` items that are meaning hits for ``query`` (rule above), best first;
        with ``min_sim`` every item at or above that cosine instead. [] if the embedder is down."""
        qv = self.qvec(query)
        if qv is None:
            return []
        vecs = self.vectors(kind)
        sims = {ref: sum(map(mul, qv, v)) for ref, v in vecs.items()}
        mean, sd = BASE_MEAN, BASE_SD
        if len(sims) >= 30:
            mean = sum(sims.values()) / len(sims)
            sd = max((sum((x - mean) ** 2 for x in sims.values()) / len(sims)) ** 0.5, 1e-6)
        out = []
        for ref, s in sims.items():
            z = (s - mean) / sd
            hit = s >= min_sim if min_sim is not None else s >= SIM_HIGH or (z >= Z_MIN and s >= SIM_MIN)
            if hit and (allowed is None or ref in allowed):
                out.append((ref, s, z))
        return sorted(out, key=lambda x: -x[1])

    def embed_items(self, kind, items, timeout=120.0) -> int:
        """Embed and store [(ref, text)]; returns how many were stored (0 if the embedder is down)."""
        items = [(str(r), t) for r, t in items if (t or "").strip()]
        vecs = self.embedder.embed([t for _, t in items], "passage", timeout) if items else None
        if not vecs:
            return 0
        with self.lock:
            for (ref, text), v in zip(items, vecs):
                self.x("INSERT OR REPLACE INTO vectors(kind,ref,model,hash,vec) VALUES(?,?,?,?,?)",
                       (kind, ref, self.embedder.model, _h(text), array("f", v).tobytes()))
        return len(items)

    def embed_missing(self, batch=64, max_items=None) -> int:
        """Backfill: embed active facts, wiki chunks and chat notes whose vector is missing or stale, and drop
        vectors of things that are gone. Returns how many were embedded; cheap when nothing changed."""
        if not self.embedder.available():
            return 0
        want = {("fact", str(f["id"])): f["text"] for f in self.q("SELECT id, text FROM facts WHERE status='active'")}
        want.update({("wiki", c["ref"]): wiki_embed_text(c) for c in self.q("SELECT * FROM wiki_chunks")})
        want.update({("note", n["session_id"]): note_embed_text(n) for n in self.q("SELECT * FROM notes")})
        have = {(r["kind"], r["ref"]): r["hash"] for r in self.q("SELECT kind, ref, hash FROM vectors WHERE model=?",
                                                                  (self.embedder.model,))}
        with self.lock:
            for kind, ref in set(have) - set(want):
                self.x("DELETE FROM vectors WHERE kind=? AND ref=?", (kind, ref))
        todo = [(k, r, t) for (k, r), t in want.items() if have.get((k, r)) != _h(t)][:max_items]
        done = 0
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            for kind in {k for k, _, _ in part}:
                n = self.embed_items(kind, [(r, t) for k, r, t in part if k == kind])
                if not n:
                    return done  # embedder went away; the next pass continues
                done += n
        return done

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

    def queue_messages(self, session_id, messages, source, max_chars=12000, project="", title="") -> int:
        """Queue unseen user/assistant text from ``messages`` as extraction chunks, tagged with the chat's
        ``project`` and ``title`` at queue time (used for its chat note). Idempotent."""
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
            self.x("INSERT INTO chunks(session_id,text,source,created,project,title) VALUES(?,?,?,?,?,?)",
                   (session_id, c, source, now(), slug(project), (title or "")[:200]))
        return len(chunks)

    # -- chat notes (per-session summary + files/links, shared across a project) -------------------
    def set_note(self, session_id, summary="", refs=(), project="", title="") -> None:
        """Create/refresh a chat's note: a new non-empty summary replaces the old one; refs accumulate."""
        if not session_id:
            return
        old = (self.q("SELECT * FROM notes WHERE session_id=?", (session_id,)) or [{}])[0]
        merged = []
        for r in json.loads(old.get("refs") or "[]") + [clean_ref(r) for r in refs or []]:
            if r and r not in merged:
                merged.append(r)
        note = {"session_id": session_id, "project": slug(project) or old.get("project") or "",
                "title": (title or old.get("title") or "")[:200],
                "summary": (summary or "").strip()[:1200] or old.get("summary") or "", "refs": json.dumps(merged[-25:])}
        if not (note["summary"] or merged):
            return
        with self.lock:
            self.x("INSERT OR REPLACE INTO notes(session_id,project,title,summary,refs,updated) VALUES(?,?,?,?,?,?)",
                   (*note.values(), now()))
            self.x("DELETE FROM notes_fts WHERE session_id=?", (session_id,))
            self.x("INSERT INTO notes_fts(session_id,title,summary,refs) VALUES(?,?,?,?)",
                   (session_id, note["title"], note["summary"], " ".join(merged)))
        self.embed_items("note", [(session_id, note_embed_text(note))], timeout=10)

    def chats(self, query, project="", exclude=(), limit=PROJECT_CHATS, min_overlap=2) -> list:
        """Chat notes relevant to ``query`` (keywords + meaning), optionally within one project."""
        where, args = "", []
        if project:
            where, args = " AND n.project=?", [slug(project)]
        kw = []
        mq = fts_query(query)
        if mq:
            try:
                kw = self.q("SELECT n.* FROM notes_fts JOIN notes n ON n.session_id=notes_fts.session_id WHERE notes_fts "
                            f"MATCH ?{where} ORDER BY bm25(notes_fts) LIMIT ?", [mq, *args, limit * 4])
            except sqlite3.OperationalError:
                pass
        qt = set(tokens(query))
        kw = [n for n in kw if n["session_id"] not in exclude and overlap_ok(qt, f"{n['title']} {n['summary']} {n['refs']}", min_overlap)]
        allowed = {r["session_id"] for r in self.q("SELECT session_id FROM notes n WHERE 1=1" + where, args)} - set(exclude)
        sem = self.similar("note", query, allowed=allowed)

        def load(refs):
            marks = ",".join("?" * len(refs))
            return {r["session_id"]: r for r in self.q(f"SELECT * FROM notes WHERE session_id IN ({marks})", list(refs))}
        return fuse(kw, sem, lambda n: n["session_id"], load, limit)

    def recent_chats(self, project, exclude=(), limit=2) -> list:
        rows = self.q("SELECT * FROM notes WHERE project=? ORDER BY updated DESC LIMIT ?", (slug(project), limit + len(exclude)))
        return [r for r in rows if r["session_id"] not in exclude][:limit]

    # -- projects -----------------------------------------------------------------------------
    def set_project(self, session_id, project, title=None):
        self.x("INSERT INTO sessions(session_id,project,title,updated) VALUES(?,?,?,?) ON CONFLICT(session_id) DO "
               "UPDATE SET project=excluded.project, title=COALESCE(excluded.title, sessions.title), updated=excluded.updated",
               (session_id, slug(project), title, now()))

    def get_project(self, session_id) -> str:
        rows = self.q("SELECT project FROM sessions WHERE session_id=?", (session_id,))
        return (rows[0]["project"] or "") if rows else ""

    def resolve_project(self, session_id="", session_key="", chat_id="", parent="", chat_name="") -> str:
        """Manual /project for this chat, else the thread's auto-picked project, else the extractor's guess for
        the session, else the channel default (a thread inherits its parent channel's; ``parent`` is the parent
        channel id, else it is read from ``chat_name``)."""
        for sid in (f"key:{session_key}" if session_key else "", f"thread:{chat_id}" if chat_id else "", session_id):
            project = self.get_project(sid) if sid else ""
            if project:
                return project
        if not chat_id:
            return ""
        return slug(channel_setting("channel_projects", chat_id, parent or parent_channel(chat_name)) or "")

    def pick_project(self, text) -> tuple:
        """Best existing project for a new thread's title/first message: (project, score), project "" when not
        confident. By meaning: cosine to each project's centroid (mean of its facts' vectors; projects with >= 2
        facts); the best must reach PICK_MIN, lead the runner-up by PICK_MARGIN and, with enough projects to
        judge, stand out from the rest (z >= PICK_Z). Otherwise (or with the embedder down) a single project
        whose full name appears in the text, e.g. "home assistant"."""
        qv, vecs, groups = self.qvec(text), self.vectors("fact"), {}
        for r in self.q("SELECT id, project FROM facts WHERE status='active' AND project!=''"):
            if str(r["id"]) in vecs:
                groups.setdefault(r["project"], []).append(vecs[str(r["id"])])
        scores = []
        for project, vs in groups.items() if qv is not None else ():
            if len(vs) >= 2:
                c = [sum(col) / len(vs) for col in zip(*vs)]
                scores.append((sum(map(mul, qv, c)) / max(sum(x * x for x in c) ** 0.5, 1e-9), project))
        scores.sort(reverse=True)
        if scores:
            best, project = scores[0]
            second = scores[1][0] if len(scores) > 1 else 0.0
            mean = sum(x for x, _ in scores) / len(scores)
            sd = max((sum((x - mean) ** 2 for x, _ in scores) / len(scores)) ** 0.5, 1e-6)
            if best >= PICK_MIN and best - second >= PICK_MARGIN and (len(scores) < 8 or (best - mean) / sd >= PICK_Z):
                return project, round(best, 3)
        words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
        named = sorted((p for p in self.projects() if set(p.split("-")) <= words), key=len, reverse=True)
        if named and (len(named) == 1 or len(named[0]) > len(named[1])):
            return named[0], 1.0
        return "", round(scores[0][0], 3) if scores else 0.0

    def auto_project(self, chat_id, text, max_tries=3) -> str:
        """Assign a project to a thread in a mixed channel from its title/first messages (see pick_project).
        Remembered per thread (sessions row "thread:<id>"); after ``max_tries`` unconfident looks it stays
        unassigned. Returns the project or ""."""
        key = f"thread:{chat_id}"
        row = (self.q("SELECT project, title FROM sessions WHERE session_id=?", (key,)) or [{}])[0]
        if row.get("project"):
            return row["project"]
        tries = int(re.sub(r"\D", "", row.get("title") or "") or 0)
        if tries >= max_tries:
            return ""
        project, score = self.pick_project(text)
        self.set_project(key, project, title=f"auto-pick {score}" if project else f"auto-pick tries {tries + 1}")
        return project

    # -- wiki ---------------------------------------------------------------------------------
    def index_wiki(self, wiki=None, force=False) -> int:
        wiki = Path(wiki or home() / "wiki")
        wiki.mkdir(parents=True, exist_ok=True)
        files = [p for p in sorted(wiki.rglob("*.md"))
                 if not any(part.startswith((".", "_")) or part == "raw" for part in p.relative_to(wiki).parts[:-1])]
        sig = json.dumps([(str(p), p.stat().st_mtime) for p in files])
        old = self.q("SELECT value FROM meta WHERE key='wiki_sig'")
        chunked = self.q("SELECT 1 FROM wiki_chunks LIMIT 1") or not files  # pre-upgrade index has no chunks yet
        if not force and old and old[0]["value"] == sig and chunked:
            return 0
        with self.lock:
            self.x("DELETE FROM wiki_fts")
            self.x("DELETE FROM wiki_chunks")
            self.x("DELETE FROM wiki_chunks_fts")
            for p in files:
                body, rel = p.read_text(errors="replace"), str(p.relative_to(wiki))
                m = re.search(r"^#\s+(.+)$", body, re.M)
                title = m.group(1).strip() if m else p.stem
                self.x("INSERT INTO wiki_fts(path,title,body) VALUES(?,?,?)", (rel, title, body))
                for heading, text in ([] if p.name in WIKI_SKIP else wiki_chunks(body)):
                    ref = _h(f"{rel}|{heading}|{text}")
                    self.x("INSERT OR IGNORE INTO wiki_chunks(ref,path,title,heading,body) VALUES(?,?,?,?,?)",
                           (ref, rel, title, heading, text))
            self.x("INSERT INTO wiki_chunks_fts(ref,path,title,heading,body) SELECT ref,path,title,heading,body FROM wiki_chunks")
            self.x("INSERT OR REPLACE INTO meta(key,value) VALUES('wiki_sig',?)", (sig,))
        return len(files)

    def wiki_search(self, query, limit=2, min_overlap=1, ratio=0.0) -> list:
        """Best wiki passage per page for ``query`` (keywords + meaning): [{path, title, snip, sim, kw}].
        ``snip`` is the whole passage (heading + up to WIKI_CHUNK chars)."""
        kw = []
        mq = fts_query(query)
        if mq:
            try:
                kw = self.q("SELECT c.* FROM wiki_chunks_fts f JOIN wiki_chunks c ON c.ref=f.ref WHERE wiki_chunks_fts "
                            "MATCH ? ORDER BY bm25(wiki_chunks_fts) LIMIT ?", (mq, limit * 8))
            except sqlite3.OperationalError:
                pass
        qt = set(tokens(query))
        kw = [c for c in kw if overlap_ok(qt, f"{c['title']} {c['heading']} {c['body']}", min_overlap, ratio)]
        sem = self.similar("wiki", query)

        def load(refs):
            marks = ",".join("?" * len(refs))
            return {r["ref"]: r for r in self.q(f"SELECT * FROM wiki_chunks WHERE ref IN ({marks})", list(refs))}
        out, pages = [], set()
        for c in fuse(kw, sem, lambda c: c["ref"], load, limit * 8):
            if c["path"] not in pages:
                pages.add(c["path"])
                head = f"{c['heading']}: " if c["heading"] and c["heading"] != c["title"] else ""
                out.append({"path": c["path"], "title": c["title"], "snip": head + " ".join(c["body"].split()),
                            "sim": c["sim"], "z": c["z"], "kw": c["kw"]})
        return out[:limit]

    # -- recall -------------------------------------------------------------------------------
    def prefetch(self, query, project="", cap=PREFETCH_CHARS, session_id="") -> str:
        """Context block for one turn. "" if nothing is relevant. Sections, in priority order:
        the chat's project (key + relevant project facts, relevant other chats of the project with their
        session ids, files/links), relevant facts (FACTS_SOFT, up to FACTS_MAX if meaning-confirmed), wiki
        passages (up to WIKI_MAX). Lines are never cut; whatever does not fit ``cap`` (or past the limits)
        is counted in a closing line that points Hermes to brain_search."""
        sections, shown, more = [], set(), 0
        if project:
            key = self.project_facts(project, 3)
            rel = [f for f in self.search(query, PROJECT_FACTS, project=project, min_overlap=2)
                   if f["id"] not in {k["id"] for k in key}]
            proj = key + rel[:PROJECT_FACTS - len(key)]
            shown |= {f["id"] for f in proj}
            excl = {session_id} if session_id else set()
            chats = self.chats(query, project, excl)
            label = "Other chats in this project, most relevant first (open one with session_search session_id=...):"
            if not chats:
                chats = self.recent_chats(project, excl)
                label = "Recent other chats in this project (open one with session_search session_id=...):"
            lines = [f"- [#{f['id']} {f['kind']}] {f['text']}" for f in proj]
            sections.append((f"Current project: {project}", lines))
            if chats:
                sections.append((label, [chat_line(n) for n in chats]))
            refs = project_refs(self.q("SELECT refs FROM notes WHERE project=? ORDER BY updated DESC LIMIT 20",
                                       (slug(project),)), query, {r for n in chats for r in json.loads(n["refs"] or "[]")})
            if refs:
                sections.append(("Project files & links:", ["- " + ", ".join(refs)]))
        hits = [f for f in self.search(query, limit=40, min_overlap=2) if f["id"] not in shown]
        keep = [f for i, f in enumerate(hits) if i < FACTS_SOFT or strong(f)][:FACTS_MAX]
        more += len(hits) - len(keep)
        if keep:
            sections.append(("Relevant facts:", [f"- [#{f['id']} {f['kind']}{'/' + f['project'] if f['project'] else ''}] "
                                                 f"{f['text']}" for f in keep]))
        wiki = self.wiki_search(query, WIKI_MAX + 3, min_overlap=2, ratio=0.6)  # passages are long: keyword hits need more
        more += max(0, len(wiki) - WIKI_MAX)
        if wiki[:WIKI_MAX]:
            sections.append(("Wiki:", [f"- {w['path']} ({w['title']}): {w['snip']}" for w in wiki[:WIKI_MAX]]))
        if not any(lines for _, lines in sections):
            return ""
        text = "## Brain (auto-recalled background; may be outdated)\n"
        for header, lines in sections:
            block = ""
            for line in lines:
                if len(text) + len(header) + len(block) + len(line) + 2 > cap - 120:  # keep room for the "more" line
                    more += 1
                else:
                    block += line + "\n"
            if block:
                text += header + "\n" + block
        if more:
            text += f"(+{more} more brain matches not shown; call brain_search for them.)\n"
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
                "wiki_pages": one("SELECT COUNT(*) n FROM wiki_fts"),
                "chat_notes": one("SELECT COUNT(*) n FROM notes"),
                "vectors": one("SELECT COUNT(*) n FROM vectors")}


def fuse(kw_rows, sem, key, load, limit) -> list:
    """Reciprocal rank fusion of keyword rows and [(ref, cosine, z)] meaning hits; rows found by both rank highest.
    ``load(refs)`` fetches {ref: row} for meaning-only hits. Rows gain ``sim``, ``z`` and ``kw``."""
    score, rows = {}, {}
    sims, zs = {r: s for r, s, _ in sem}, {r: z for r, _, z in sem}
    kws = {key(r) for r in kw_rows}
    for i, r in enumerate(kw_rows):
        rows[key(r)] = r
        score[key(r)] = 1 / (RRF_K + i + 1)
    for i, (ref, _, _) in enumerate(sem):
        score[ref] = score.get(ref, 0) + 1 / (RRF_K + i + 1)
    order = sorted(score, key=lambda k: -score[k])[:limit]
    missing = [k for k in order if k not in rows]
    if missing:
        rows.update(load(missing))
    return [dict(rows[k], sim=sims.get(k), z=zs.get(k), kw=k in kws) for k in order if k in rows]


def strong(row: dict) -> bool:
    """A meaning hit clearly above the background (used to go past the soft limits)."""
    return row.get("sim") is not None and (row["sim"] >= SIM_HIGH or row["z"] >= Z_STRONG)


def wiki_embed_text(c: dict) -> str:
    return f"{c['title']} - {c['heading']}\n{c['body']}" if c["heading"] else f"{c['title']}\n{c['body']}"


def note_embed_text(n: dict) -> str:
    return f"{n.get('title') or ''}\n{n.get('summary') or ''}".strip()


def chat_line(n: dict) -> str:
    refs = json.loads(n.get("refs") or "[]")
    title = f' "{n["title"]}"' if n.get("title") else ""
    return (f"- session {n['session_id']} ({(n.get('updated') or '')[:10]}{title}): {n.get('summary') or '(no summary yet)'}"
            + (f" [files/links: {', '.join(refs[-6:])}]" if refs else ""))


def project_refs(rows, query, already, limit=10) -> list:
    """Files/links from the project's chats not already shown, ones sharing words with ``query`` first."""
    qt, out = set(tokens(query)), []
    for row in rows:
        for r in json.loads(row["refs"] or "[]"):
            if r not in already and r not in out:
                out.append(r)
    return sorted(out, key=lambda r: -len(qt & set(tokens(r))))[:limit]


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
If you don't set one, the channel decides: some channels have a fixed project, threads inherit their channel's, and a new thread in a mixed channel (#projects, #research, ...) gets the project that clearly matches its first message, or none.
Chats in the same project share memory: Hermes sees short summaries and files/links of the project's other chats."""


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


def cmd_project(store: Store, raw: str, session_key: str, session_id: str = "", chat_id: str = "", parent: str = "",
                chat_name: str = "") -> str:
    name = (raw or "").strip()
    key = session_key or "cli"
    if name.lower() in ("help", "?"):
        return BRAIN_HELP.split("**Projects**", 1)[1].strip()
    if not name:
        cur = store.resolve_project(session_id, key, chat_id, parent, chat_name)
        known = ", ".join(store.projects()) or "none yet"
        return (f"Current project: {cur or '(none: Hermes guesses automatically)'}\nKnown projects: {known}\n"
                "Set with `/project <name>`, clear with `/project none`, more with `/brain help`.")
    if name.lower() in ("none", "clear", "-"):
        store.set_project(f"key:{key}", "")
        return "Project cleared for this chat."
    store.set_project(f"key:{key}", name)
    return f"This chat is now mapped to project: {slug(name)}"
