#!/usr/bin/env python3
"""Topic model: a tiny local daemon that scores "does this message start a new topic?" for the topic router.

Model: bge-small fine-tuned on the owner's chats (round 6, see TOPIC-ROUTER-LOCAL-MODEL.md), exported to ONNX, plus a
logistic head over 8 similarity features and a Platt calibration (router_head.json). Runs in the brain embedder's
venv (onnxruntime + tokenizers, CPU only) and listens ONLY on a Unix socket (no TCP port).

Protocol: one JSON line in, one JSON line out.
  {"context": [{"role", "content"}, ...], "message": "..."}  ->  {"p_new": 0.12, "verdict": "continue"|"new"|"unsure", "ms": 85}
  {"mode": "ping"}                                            ->  {"model": "..."}

  topic_model_server.py [--socket PATH] [--model DIR] [--threads N] [--check]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socketserver
import threading
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
FEATS = ["max_turn", "last_turn", "joined", "max_user", "mean_turn", "last_user"]


class Model:
    def __init__(self, path: Path, threads: int):
        self.head = json.loads((path / "router_head.json").read_text())
        self.tok = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.tok.enable_truncation(512)
        self.tok.enable_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads, opts.inter_op_num_threads = threads, 1
        self.sess = ort.InferenceSession(str(path / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.lock = threading.Lock()

    def embed(self, texts):
        enc = self.tok.encode_batch(texts)
        feed = {"input_ids": np.array([e.ids for e in enc], np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], np.int64),
                "token_type_ids": np.array([e.type_ids for e in enc], np.int64)}
        with self.lock:
            cls = self.sess.run(None, feed)[0][:, 0]  # CLS pooling, as in the sentence-transformers config
        return cls / np.maximum(np.linalg.norm(cls, axis=1, keepdims=True), 1e-9)

    def score(self, context, message):
        """Same features as the training eval (eval_model.py 'feat' + conf5.py head)."""
        h = self.head
        ctx = [{"role": str(t.get("role") or ""), "content": str(t.get("content") or "")} for t in context][-h["ctx_turns"]:]
        ctx = [t for t in ctx if t["content"].strip()] or [{"role": "user", "content": ""}]
        joined = "\n".join(f"{t['role']}: {t['content']}" for t in ctx)[-h["ctx_chars"]:]
        v = self.embed([message[:h["msg_chars"]]] + [t["content"] for t in ctx] + [joined])
        msg, tv = v[0], v[1:-1]
        sims = tv @ msg
        ui = [i for i, t in enumerate(ctx) if t["role"] == "user"]
        f = {"max_turn": sims.max(), "last_turn": sims[-1], "joined": v[-1] @ msg,
             "max_user": sims[ui].max() if ui else sims.max(), "mean_turn": sims.mean(),
             "last_user": sims[ui[-1]] if ui else sims[-1]}
        n = len(message.split())
        x = [float(f[k]) for k in FEATS] + [float(n < 6), math.log1p(n)]
        z = sum(c * xi for c, xi in zip(h["coef"], x)) + h["intercept"]
        p = 1 / (1 + math.exp(-(h["platt_a"] * z + h["platt_b"])))
        verdict = "unsure" if max(p, 1 - p) < h["ask_below"] else ("new" if p >= 0.5 else "continue")
        return p, verdict


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        for line in self.rfile:
            try:
                req = json.loads(line)
                out = {"model": srv.model_name}
                if req.get("mode") != "ping":
                    t = time.perf_counter()
                    p, verdict = srv.model.score(req.get("context") or [], str(req.get("message") or ""))
                    out.update(p_new=round(p, 4), verdict=verdict, ms=round((time.perf_counter() - t) * 1000))
            except Exception as exc:  # bad request: answer, keep serving
                out = {"error": str(exc)[:200]}
            self.wfile.write((json.dumps(out) + "\n").encode())
            self.wfile.flush()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("--socket", default=str(HOME / "topic-model" / "topic.sock"))
    a.add_argument("--model", default=str(HOME / "topic-model" / "topic-bi-bge-r6"))
    a.add_argument("--threads", type=int, default=2)
    a.add_argument("--check", action="store_true", help="compare against reference.json and exit")
    args = a.parse_args(argv)
    path = Path(args.model)
    model = Model(path, args.threads)
    if args.check:
        ref = json.loads((path / "reference.json").read_text())
        diff = np.abs(model.embed(ref["texts"]) - np.array(ref["vecs"])).max()
        print(f"max diff vs sentence-transformers: {diff:.2e}")
        raise SystemExit(0 if diff < 1e-3 else 1)
    model.score([{"role": "user", "content": "warm up"}], "warm up")
    sock = Path(args.socket)
    sock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock.unlink(missing_ok=True)
    old = os.umask(0o177)  # socket readable/writable by this user only
    try:
        srv = Server(str(sock), Handler)
    finally:
        os.umask(old)
    srv.model, srv.model_name = model, path.name
    print(f"topic model: {path.name} on {sock}", flush=True)
    try:
        srv.serve_forever()
    finally:
        sock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
