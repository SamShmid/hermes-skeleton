#!/usr/bin/env python3
"""Google sign-in return page for Hermes.

Listens on 127.0.0.1 only; Tailscale Serve publishes it over HTTPS inside the tailnet. Google sends the browser
to GOOGLE_REDIRECT_URI with ?state=&code=; this finishes the sign-in (google_mcp.Google.connect_finish), stores
the token encrypted in the vault, and shows a simple result page. Nothing else is served.
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import google_mcp  # noqa: E402

PORT = int(os.environ.get("HERMES_OAUTH_PORT", "8791"))
PATH = urlparse(google_mcp.REDIRECT).path or "/oauth/google"

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hermes · Google sign-in</title>
<style>body{{margin:0;font:17px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#fbfbf9;color:#1d1d1b;display:grid;place-items:center;min-height:100vh}}
@media(prefers-color-scheme:dark){{body{{background:#161615;color:#ececea}}}}
main{{max-width:520px;padding:32px 20px;text-align:center}}h1{{font-size:1.6rem;margin:0 0 8px}}
p{{margin:6px 0;opacity:.85}}</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""


def page(title: str, *lines: str) -> bytes:
    return PAGE.format(title=html.escape(title),
                       body="".join(f"<p>{html.escape(l)}</p>" for l in lines)).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesOAuth/1.0"

    def log_message(self, fmt, *args):  # no query strings (codes) in logs
        sys.stderr.write("oauth-callback: %s %s\n" % (self.command, urlparse(self.path).path))

    def _send(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") != PATH.rstrip("/"):
            return self._send(404, page("Not found"))
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        state = q.get("state", "")
        if q.get("error"):
            notify(state, ok=False, error=f"cancelled at Google ({q['error']})")
            return self._send(400, page("Sign-in cancelled", f"Google said: {q['error']}.",
                                        "Nothing was changed. Ask Hermes for a new link to try again."))
        g = google_mcp.Google()
        label = g.label_for_state(state)
        if not label or not q.get("code"):
            return self._send(400, page("Link expired or already used",
                                        "Ask Hermes for a new sign-in link (links last 30 minutes)."))
        try:
            out = g.connect_finish(label, f"https://callback/?state={state}&code={q['code']}")
        except Exception as exc:  # show a plain reason, never the code
            notify(state, ok=False, error=str(exc)[:300])
            return self._send(400, page("Sign-in didn't finish", str(exc)[:300]))
        notify(state, ok=True, email=out["email"], account=out["account"], warning=out.get("warning", ""))
        lines = [f"Hermes can now use {out['email']} (account “{out['account']}”).", "You can close this tab."]
        if out.get("warning"):
            lines.insert(1, "⚠️ " + out["warning"])
        return self._send(200, page("✅ Connected", *lines))


EVENTS = google_mcp.HERMES_HOME / "google" / "events"


def notify(state: str, **result):
    """Drop a small result file; the google-notify plugin in the gateway tells the originating chat."""
    try:
        EVENTS.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = EVENTS / f".{state[:16]}.tmp"
        tmp.write_text(json.dumps({"state": state, "ts": time.time(), **result}))
        tmp.rename(EVENTS / f"{state[:16] or 'unknown'}-{int(time.time())}.json")
    except Exception as exc:
        sys.stderr.write(f"oauth-callback: could not write notify event: {exc}\n")


def main():
    if not google_mcp.AUTO_CALLBACK:
        raise SystemExit("GOOGLE_REDIRECT_URI must be an https:// address for the callback page")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
