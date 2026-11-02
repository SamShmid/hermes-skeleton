#!/usr/bin/env python3
"""Brain embedder: a tiny local daemon that turns text into vectors for the brain's meaning search.

Runs in its own venv (fastembed + onnxruntime, CPU only) so Hermes's runtime stays untouched, and listens
ONLY on a Unix socket next to brain.db (no TCP port). The brain plugin talks to it via brain_store.Embedder
and falls back to keyword search whenever it is not running.

Protocol: one JSON line in, one JSON line out.
  {"mode": "query"|"passage", "texts": [...]}  ->  {"model": "...", "dim": 384, "vecs": ["<base64 float32>", ...]}
  {"mode": "ping"}                             ->  {"model": "...", "dim": 384}

  brain_embed_server.py [--socket PATH] [--model NAME] [--cache DIR] [--threads N]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socketserver
import threading
import time
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
MAX_TEXTS, MAX_CHARS, SLICE = 256, 4000, 2


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        for line in self.rfile:
            try:
                req = json.loads(line)
                out = {"model": srv.model_name, "dim": srv.dim}
                if req.get("mode") != "ping":
                    texts = [str(t)[:MAX_CHARS] for t in (req.get("texts") or [])][:MAX_TEXTS]
                    query = req.get("mode") == "query"
                    fn = srv.model.query_embed if query else srv.model.passage_embed
                    parts = []
                    for i in range(0, len(texts), SLICE):  # small slices, so a per-turn query never waits long
                        while not query and srv.queries_waiting:  # queries (a user is waiting) go first
                            time.sleep(0.005)
                        srv.queries_waiting += query
                        try:
                            with srv.lock:  # one ONNX run at a time
                                parts += list(fn(texts[i:i + SLICE]))
                        finally:
                            srv.queries_waiting -= query
                    vecs = np.array(parts, dtype=np.float32).reshape(len(texts), srv.dim)
                    vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)  # unit length: dot = cosine
                    out["vecs"] = [base64.b64encode(v.tobytes()).decode() for v in vecs]
            except Exception as exc:  # bad request: answer, keep serving
                out = {"error": str(exc)[:200]}
            self.wfile.write((json.dumps(out) + "\n").encode())
            self.wfile.flush()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("--socket", default=str(HOME / "brain" / "embed.sock"))
    a.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    a.add_argument("--cache", default=str(HOME / "brain" / "models"))
    a.add_argument("--threads", type=int, default=2)
    args = a.parse_args(argv)
    model = TextEmbedding(args.model, cache_dir=args.cache, threads=args.threads)
    dim = len(next(iter(model.query_embed(["warm up"]))))
    sock = Path(args.socket)
    sock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock.unlink(missing_ok=True)
    old = os.umask(0o177)  # socket readable/writable by this user only
    try:
        srv = Server(str(sock), Handler)
    finally:
        os.umask(old)
    srv.model, srv.model_name, srv.dim, srv.lock, srv.queries_waiting = model, args.model, dim, threading.Lock(), 0
    print(f"brain embedder: {args.model} ({dim}d) on {sock}", flush=True)
    try:
        srv.serve_forever()
    finally:
        sock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
