#!/usr/bin/env python3
"""Bulk API/CLI for the brain ($HERMES_HOME/brain/brain.db).

  brain_cli.py stats | search <query> | reindex-wiki | seed
  brain_cli.py add-fact --text "..." [--kind K --entity E --project P --source S]   (direct, no approval)
  brain_cli.py add-fact --json facts.json          (list of {text, kind?, entity?, project?, source?}; '-' = stdin)
  brain_cli.py add-candidate --json cands.json     (list of {text, kind?, entity?, project?, source?, action?,
                                                    target_fact_id?, evidence?}; the approver decides them)
  brain_cli.py set-session <session_id> <project> [--title T]
  brain_cli.py process [--max-chunks N]            (one extract/approve pass now, using auxiliary.brain_*)

Python: sys.path.insert(0, "$HERMES_HOME/plugins/brain"); from brain_store import Store
"""
import argparse
import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")

def _brain_plugin_dir() -> Path:
    """$BRAIN_PLUGIN_DIR, else $HERMES_HOME/plugins/brain, else this repo's plugins/brain."""
    for d in (os.environ.get("BRAIN_PLUGIN_DIR"), HOME / "plugins" / "brain",
              Path(__file__).resolve().parent.parent / "plugins" / "brain"):
        if d and (Path(d) / "brain_store.py").exists():
            return Path(d)
    return HOME / "plugins" / "brain"


sys.path.insert(0, str(_brain_plugin_dir()))
import brain_pipeline  # noqa: E402
from brain_store import Store  # noqa: E402


def _load(path):
    data = json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())
    return data if isinstance(data, list) else [data]


def hermes_llm(task, system, user):
    """Pipeline LLM for use outside the gateway: Hermes's auxiliary client with the task's config slot."""
    sys.path.insert(0, str(HOME / "hermes-agent"))
    from agent.auxiliary_client import call_llm
    resp = call_llm(task=task, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    timeout=180)
    content = resp.choices[0].message.content
    if isinstance(content, list):  # text-part list
        content = "".join(p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "") for p in content)
    return content or ""


def main(argv=None, store=None):
    p = argparse.ArgumentParser(description="Brain bulk CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    sub.add_parser("reindex-wiki")
    sub.add_parser("seed")
    s = sub.add_parser("search")
    s.add_argument("query", nargs="+")
    f = sub.add_parser("add-fact")
    for a in ("--text", "--kind", "--entity", "--project", "--json"):
        f.add_argument(a)
    f.add_argument("--source", default="import")
    c = sub.add_parser("add-candidate")
    c.add_argument("--json", required=True)
    ss = sub.add_parser("set-session")
    ss.add_argument("session_id")
    ss.add_argument("project")
    ss.add_argument("--title")
    pr = sub.add_parser("process")
    pr.add_argument("--max-chunks", type=int, default=3)
    a = p.parse_args(argv)
    store = store or Store.open(HOME / "brain" / "brain.db")

    if a.cmd == "stats":
        out = store.stats()
    elif a.cmd == "search":
        out = store.search(" ".join(a.query), limit=20)
    elif a.cmd == "reindex-wiki":
        out = {"indexed": store.index_wiki(HOME / "wiki", force=True)}
    elif a.cmd == "seed":
        out = {"candidates": brain_pipeline.seed_from_memory_files(store, HOME / "memories")}
    elif a.cmd == "add-fact":
        rows = _load(a.json) if a.json else [{"text": a.text, "kind": a.kind, "entity": a.entity, "project": a.project}]
        out = {"fact_ids": [store.add_fact(r["text"], r.get("kind") or "fact", r.get("entity") or "",
                                           r.get("project") or "", r.get("source") or a.source) for r in rows]}
    elif a.cmd == "add-candidate":
        out = {"candidate_ids": [store.add_candidate(r.get("text", ""), r.get("kind") or "fact", r.get("entity") or "",
                                                     r.get("project") or "", r.get("source") or "import",
                                                     r.get("action") or "add", r.get("target_fact_id"),
                                                     r.get("evidence") or "") for r in _load(a.json)]}
    elif a.cmd == "set-session":
        store.set_project(a.session_id, a.project, a.title)
        out = {"ok": True}
    else:  # process
        out = {"work_done": brain_pipeline.run_once(store, hermes_llm, max_chunks=a.max_chunks), **store.stats()}
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return out


if __name__ == "__main__":
    main()
