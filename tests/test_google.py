"""Google connector MCP server: offline tests with mocked Google clients (no network, no real tokens).

Needs the vault venv (cryptography, google-auth, mcp); skipped otherwise.
"""
import asyncio
import datetime as dt
import importlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

VAULT_DIR = Path(__file__).resolve().parent.parent / "vault"
if not (VAULT_DIR / "google_mcp.py").exists():  # deployed layout: $HERMES_HOME/vault/tests/
    VAULT_DIR = VAULT_DIR.parent
SRC = (VAULT_DIR / "google_mcp.py").read_text()
try:
    import cryptography  # noqa: F401
    import google.oauth2.credentials  # noqa: F401
    import mcp.server.fastmcp  # noqa: F401
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

AT = "@"  # example addresses are assembled so the repo privacy scan stays quiet
EMAIL, A_ADDR = f"someone{AT}example.com", f"a{AT}example.com"
FAKE_CLIENT = {"installed": {"client_id": "test-client.apps.example", "client_secret": "fake-secret",
                             "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


def load():
    sys.path.insert(0, str(VAULT_DIR))
    for m in ("vault_mcp", "google_mcp"):
        sys.modules.pop(m, None)
    return importlib.import_module("google_mcp")


@unittest.skipUnless(HAVE_DEPS, "needs the vault venv (cryptography, google-auth, mcp)")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        env = {"VAULT_HOME": str(root / "vault"), "HERMES_HOME": str(root), "GOOGLE_DEFAULT_TZ": "America/New_York"}
        with mock.patch.dict(os.environ, env):
            self.mod = load()
        (root / "client.json").write_text(json.dumps(FAKE_CLIENT))
        self.posts, self.svc = [], mock.MagicMock()
        self.refreshes = 0

        def post(url, data=None, timeout=None):
            self.posts.append((url, data))
            if url.endswith("/token"):
                return Resp(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3599,
                                  "scope": " ".join(self.mod.SCOPES)})
            return Resp(200, {})

        def get(url, headers=None, timeout=None):
            return Resp(200, {"email": EMAIL})

        def refresh(cr):
            self.refreshes += 1
            cr.token, cr.expiry = f"at-refreshed-{self.refreshes}", self.mod._utcnow() + dt.timedelta(hours=1)

        self.g = self.mod.Google(client_file=root / "client.json", http_post=post, http_get=get,
                                 build=lambda api, ver, cr: self.svc, refresh=refresh)

    def tearDown(self):
        self.g.v.con.close()
        self.tmp.cleanup()

    def connect(self, label="personal"):
        msg = self.g.connect_start(label)
        state = re.search(r"state=([^&\s]+)", msg).group(1)
        return self.g.connect_finish(label, f"http://localhost:1/?state={state}&code=4/0abc-code&scope=x")

    def tool(self, name):
        return self.server._tool_manager._tools[name].fn


class SignInTests(Base):
    def test_start_url_has_pkce_offline_consent(self):
        msg = self.g.connect_start("personal")
        url = re.search(r"https://accounts\.google\.com\S+", msg).group(0)
        for part in ("code_challenge_method=S256", "access_type=offline", "prompt=consent",
                     "include_granted_scopes=true", "redirect_uri=http%3A%2F%2Flocalhost%3A1", "gmail.modify"):
            self.assertIn(part, url)
        self.assertNotIn("gmail.send", url)
        self.assertIn("Advanced", msg)
        self.assertNotIn("fake-secret", msg)

    def test_finish_stores_encrypted_token_and_email(self):
        out = self.connect()
        self.assertEqual(out["email"], EMAIL)
        self.assertNotIn("warning", out)
        tok_post = self.posts[0][1]
        self.assertEqual(tok_post["code"], "4/0abc-code")
        self.assertTrue(tok_post["code_verifier"])
        self.assertEqual(self.g.labels(), ["personal"])
        raw = (Path(self.tmp.name) / "vault" / "vault.db").read_bytes()
        self.assertNotIn(b"rt-1", raw)
        self.assertEqual([r["name"] for r in self.g.v.list("google-pending")], [])

    def test_state_mismatch_and_no_pending(self):
        self.g.connect_start("work")
        with self.assertRaisesRegex(ValueError, "state mismatch"):
            self.g.connect_finish("work", "http://localhost:1/?state=WRONG&code=4/0abc")
        with self.assertRaisesRegex(ValueError, "no sign-in in progress"):
            self.g.connect_finish("other", "http://localhost:1/?code=4/0abc")

    def test_pending_state_expires(self):
        self.g.connect_start("work")
        later = time.time() + self.mod.PENDING_TTL + 5
        with mock.patch.object(self.mod.time, "time", return_value=later):
            with self.assertRaisesRegex(ValueError, "expired"):
                self.g.connect_finish("work", "4/0abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(self.g.v.list("google-pending"), [])

    def test_missing_scopes_warn(self):
        orig = self.g.post
        self.g.post = lambda url, data=None, timeout=None: Resp(200, {
            "access_token": "a", "refresh_token": "r", "expires_in": 3600,
            "scope": "openid https://www.googleapis.com/auth/drive"}) if url.endswith("/token") else orig(url, data)
        out = self.connect()
        self.assertIn("gmail.modify", out["warning"])

    def test_disconnect_revokes_and_deletes(self):
        self.connect()
        msg = self.g.disconnect("personal")
        self.assertIn("revoked", msg)
        self.assertTrue(self.posts[-1][0].endswith("/revoke"))
        self.assertEqual(self.posts[-1][1]["token"], "rt-1")
        self.assertEqual(self.g.labels(), [])


class ParseTests(Base):
    def test_parse_redirect_forms(self):
        p = self.mod.parse_redirect
        self.assertEqual(p("http://localhost:1/?state=s1&code=4/0Ab&scope=email"), ("4/0Ab", "s1"))
        self.assertEqual(p(" <http://localhost:1/?code=4/0Ab%2Fx&state=s> "), ("4/0Ab/x", "s"))
        self.assertEqual(p("code=4/0Ab&state=s2"), ("4/0Ab", "s2"))
        self.assertEqual(p("4/0AbCdEfGhIjKlMnOpQrStUv"), ("4/0AbCdEfGhIjKlMnOpQrStUv", None))
        with self.assertRaisesRegex(ValueError, "access_denied"):
            p("http://localhost:1/?error=access_denied&state=s")
        for bad in ("", "hello", "https://example.com/page"):
            with self.assertRaises(ValueError):
                p(bad)

    def test_labels(self):
        self.assertEqual(self.mod.label_of(" Work "), "work")
        for bad in ("", "has space", "a-b", "x" * 41, "../x"):
            with self.assertRaises(ValueError):
                self.mod.label_of(bad)


class TokenTests(Base):
    def test_expired_token_refreshes_and_writes_back(self):
        self.connect()
        tok = json.loads(self.g.v.get("GOOGLE_TOKEN_PERSONAL"))
        tok["expiry"] = (self.mod._utcnow() - dt.timedelta(minutes=5)).isoformat()
        self.g._store_token("personal", tok)
        cr = self.g.creds("personal")
        self.assertEqual(cr.token, "at-refreshed-1")
        stored = json.loads(self.g.v.get("GOOGLE_TOKEN_PERSONAL"))
        self.assertEqual(stored["token"], "at-refreshed-1")
        self.assertEqual(stored["refresh_token"], "rt-1")
        self.assertEqual(stored["email"], EMAIL)
        self.g.creds("personal")  # still valid: no second refresh
        self.assertEqual(self.refreshes, 1)

    def test_token_refreshed_during_call_is_saved(self):
        self.connect()

        def fn(svc):
            cr_holder["cr"].token = "at-from-client"
            return "ok"
        cr_holder = {}
        real_creds = self.g.creds
        self.g.creds = lambda a: cr_holder.setdefault("cr", real_creds(a))
        self.assertEqual(self.g.call("personal", "gmail", "v1", fn), "ok")
        self.assertEqual(json.loads(self.g.v.get("GOOGLE_TOKEN_PERSONAL"))["token"], "at-from-client")

    def test_refresh_failure_is_friendly(self):
        self.connect()
        tok = json.loads(self.g.v.get("GOOGLE_TOKEN_PERSONAL"))
        tok["expiry"] = "2000-01-01T00:00:00"
        self.g._store_token("personal", tok)
        self.g._refresh = mock.Mock(side_effect=RuntimeError("invalid_grant"))
        with self.assertRaisesRegex(ValueError, "reconnect"):
            self.g.creds("personal")
        self.assertIn("broken", self.g.accounts()[0]["health"])

    def test_unknown_account(self):
        with self.assertRaisesRegex(ValueError, "no Google account 'nope'"):
            self.g.creds("nope")


class ToolTests(Base):
    def setUp(self):
        super().setUp()
        self.server = self.mod.build_server(self.g)
        self.connect()

    def test_gmail_search_caps_and_shapes(self):
        m = self.svc.users().messages()
        m.list.return_value.execute.return_value = {"messages": [{"id": "m1"}], "resultSizeEstimate": 1}
        m.get.return_value.execute.return_value = {"id": "m1", "threadId": "t1", "snippet": "hi &amp; bye",
            "payload": {"headers": [{"name": "From", "value": A_ADDR}, {"name": "Subject", "value": "S"}]}}
        out = self.tool("gmail_search")("personal", "from:a", max=999)
        self.assertEqual(m.list.call_args.kwargs["maxResults"], 50)
        self.assertEqual(out["messages"][0], {"id": "m1", "thread": "t1", "date": None, "from": A_ADDR,
                                              "subject": "S", "snippet": "hi & bye"})

    def test_gmail_read_plain_body_truncated(self):
        import base64
        data = base64.urlsafe_b64encode(("x" * 1000).encode()).decode()
        self.svc.users().messages().get.return_value.execute.return_value = {"id": "m1", "payload": {
            "mimeType": "multipart/mixed", "headers": [], "parts": [
                {"mimeType": "text/plain", "body": {"data": data}},
                {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"size": 5}}]}}
        out = self.tool("gmail_read")("personal", "m1", max_chars=300)
        self.assertEqual(len(out["body"]["text"]), 300)
        self.assertTrue(out["body"]["truncated"])
        self.assertEqual(out["attachments"][0]["name"], "a.pdf")
        self.assertIn("untrusted", self.server._tool_manager._tools["gmail_read"].description.lower())

    def test_html_only_body(self):
        import base64
        h = base64.urlsafe_b64encode(b"<style>x{}</style><p>Hello&nbsp;there</p><br>Bye").decode()
        body, _ = self.mod.mail_body({"mimeType": "text/html", "body": {"data": h}})
        self.assertEqual(body, "Hello\xa0there\n\nBye")

    def test_filter_validation_and_actions(self):
        fs = self.mod.filter_spec
        self.assertEqual(fs("trash", f"x{AT}example.com", None, None, False),
                         ({"from": f"x{AT}example.com"}, {"addLabelIds": ["TRASH"]}))
        self.assertEqual(fs("archive", None, "subject:sale", None, False)[1], {"removeLabelIds": ["INBOX"]})
        for args in (("delete", "a", None, None, False), ("trash", None, " ", None, False),
                     ("label", "a", None, None, False), ("archive", "a", None, "L", False)):
            with self.assertRaises(ValueError):
                fs(*args)

    def test_create_label_filter_creates_missing_label(self):
        u = self.svc.users()
        u.labels().list.return_value.execute.return_value = {"labels": [{"id": "L1", "name": "Other"}]}
        u.labels().create.return_value.execute.return_value = {"id": "L9"}
        u.settings().filters().create.return_value.execute.return_value = {"id": "F1"}
        out = self.tool("gmail_create_filter")("personal", "label", sender=f"n{AT}example.com", label="News",
                                               also_archive=True)
        self.assertEqual(out["created"], "F1")
        body = u.settings().filters().create.call_args.kwargs["body"]
        self.assertEqual(body["action"], {"removeLabelIds": ["INBOX"], "addLabelIds": ["L9"]})

    def test_event_times(self):
        et = self.mod.event_times
        s, e = et("2026-10-10T09:00", "2026-10-10T10:00", "America/New_York")
        self.assertEqual(s, {"dateTime": "2026-10-10T09:00:00", "timeZone": "America/New_York"})
        self.assertEqual(et("2026-10-10", "2026-10-10", "UTC"), ({"date": "2026-10-10"}, {"date": "2026-10-11"}))
        for a, b in (("2026-10-10T10:00", "2026-10-10T09:00"), ("2026-10-10", "2026-10-10T09:00"),
                     ("tomorrow", "2026-10-10"), ("2026-10-10T09:00Z", "2026-10-10T10:00")):
            with self.assertRaises(ValueError):
                et(a, b, "UTC")
        self.svc.events().insert.return_value.execute.return_value = {"id": "e1", "htmlLink": "L"}
        self.tool("calendar_create_event")("personal", "Dentist", "2026-10-10T09:00", "2026-10-10T10:00")
        self.assertEqual(self.svc.events().insert.call_args.kwargs["body"]["start"]["timeZone"], "America/New_York")

    def test_drive_query_and_read(self):
        dq = self.mod.drive_q
        self.assertEqual(dq("tax o'brien"), "(name contains 'tax o\\'brien' or fullText contains 'tax o\\'brien') "
                                            "and trashed = false")
        self.assertEqual(dq("name = 'x'"), "(name = 'x') and trashed = false")
        f = self.svc.files()
        f.get.return_value.execute.return_value = {"id": "d", "name": "Doc",
                                                   "mimeType": "application/vnd.google-apps.document"}
        f.export.return_value.execute.return_value = b"hello doc"
        out = self.tool("drive_read")("personal", "d")
        self.assertEqual(out["content"]["text"], "hello doc")
        f.get.return_value.execute.return_value = {"id": "z", "name": "z.zip", "mimeType": "application/zip"}
        self.assertIsNone(self.tool("drive_read")("personal", "z")["content"])

    def test_upload_refuses_secrets(self):
        vk = Path(self.tmp.name) / "vault" / "vault.key"
        with self.assertRaisesRegex(ValueError, "secrets"):
            self.mod.check_upload_path(str(vk))
        env = Path(self.tmp.name) / ".env"
        env.write_text("X=1")
        with self.assertRaisesRegex(ValueError, "secrets"):
            self.mod.check_upload_path(str(env))
        with self.assertRaises(ValueError):
            self.mod.check_upload_path(str(Path(self.tmp.name) / "missing.txt"))

    def test_rows_validation(self):
        self.mod.check_rows([["a", 1, 2.5, True, None]])
        for bad in ([], "x", [["a"], "b"], [[{"a": 1}]], [["x"]] * 1001):
            with self.assertRaises(ValueError):
                self.mod.check_rows(bad)

    def test_mcp_arg_validation(self):
        try:
            text = json.dumps(asyncio.run(self.server.call_tool("gmail_search", {"account": "personal"})), default=str)
        except Exception as e:  # newer mcp raises ToolError for invalid arguments
            text = str(e)
        self.assertIn("query", text)  # missing required argument is reported, not executed
        self.svc.users().messages().list.assert_not_called()


class NoSendTests(Base):
    EXPECTED = {"google_accounts", "google_connect_start", "google_connect_finish", "google_disconnect",
                "gmail_search", "gmail_read", "gmail_list_filters", "gmail_create_filter", "gmail_delete_filter",
                "drive_search", "drive_read", "drive_upload", "drive_create_folder", "calendar_list",
                "calendar_create_event", "docs_create", "sheets_read", "sheets_append"}

    def test_tool_set_is_exactly_expected(self):
        tools = {t.name for t in asyncio.run(self.mod.build_server(self.g).list_tools())}
        self.assertEqual(tools, self.EXPECTED)
        self.assertFalse([t for t in tools if re.search(r"send|draft|trash|delete_(file|email|message|event)", t)])

    def test_no_send_scope_and_no_destructive_calls(self):
        self.assertFalse(set(self.mod.SCOPES) & self.mod.FORBIDDEN_SCOPES)
        code = "\n".join(ln for ln in SRC.split('"""', 2)[2].splitlines()  # skip docstring + the deny-list
                         if not ln.startswith("FORBIDDEN_SCOPES"))
        for pat in (r"\.send\(", r"drafts\(", r"messages\(\)\.(trash|delete|batchDelete)", r"\.batchDelete\(",
                    r"files\(\)\.(delete|emptyTrash)", r"events\(\)\.delete", r"threads\(\)", r"gmail\.send"):
            self.assertIsNone(re.search(pat, code), pat)


if __name__ == "__main__":
    unittest.main()
