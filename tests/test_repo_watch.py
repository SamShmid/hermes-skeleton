"""Offline tests for repo_watch parsing, state and rendering logic (no network)."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_CANDIDATES = [HERE.parent / "repo_watch.py", HERE.parent / "scripts" / "repo_watch.py"]
SCRIPT = Path(os.environ.get("REPO_WATCH_PATH") or next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0]))
spec = importlib.util.spec_from_file_location("repo_watch", SCRIPT)
rw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rw)

NOW = dt.datetime(2026, 10, 12, 13, 0, tzinfo=dt.timezone.utc)


def cfg(**over):
    c = rw.deep_merge(rw.DEFAULT_CONFIG, over)
    return c


def repo(key, host="github", **kw):
    r = {"key": key, "host": host, "full_name": key.split("/", 1)[1], "name": key.split("/")[-1],
         "pushed_at": "2026-10-10T00:00:00Z", "default_branch": "main", "open_prs": 0, "open_issues": 0,
         "private": False}
    r.update(kw)
    return r


def report(repos=(), findings=(), ci=None, errors=None, monitors=None, scanned=None, gl_ignore=None):
    return {"repos": list(repos), "findings": list(findings), "ci": ci or {}, "errors": errors or {},
            "monitors": monitors, "coverage": {}, "scanned": scanned or {}, "gitleaks_ignore": gl_ignore or {}}


def finding(fid, repo_key="gh/o/a", sev="high", source="osv", **kw):
    f = {"id": fid, "repo": repo_key, "severity": sev, "source": source, "title": f"pkg 1.0 {fid}",
         "aliases": []}
    f.update(kw)
    return f


class ConfigTests(unittest.TestCase):
    def test_merge_and_normalize(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"github": {"owners": ["x"]}, "alert_severity": "Moderate"}))
            c = rw.load_config(p)
        self.assertEqual(c["github"]["owners"], ["x"])
        self.assertTrue(c["github"]["enabled"])           # default kept
        self.assertEqual(c["alert_severity"], "medium")   # normalized

    def test_bad_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"severity_threshold": "spicy"}))
            with self.assertRaises(ValueError):
                rw.load_config(p)

    def test_secret_env_first(self):
        os.environ["RW_TEST_SECRET"] = "abc"
        try:
            self.assertEqual(rw.get_secret("RW_TEST_SECRET", cfg(secret_command=["false"])), "abc")
        finally:
            del os.environ["RW_TEST_SECRET"]
        self.assertEqual(rw.get_secret("RW_MISSING_X", cfg()), "")


class HelperTests(unittest.TestCase):
    def test_severity(self):
        self.assertEqual(rw.normalize_severity("MODERATE"), "medium")
        self.assertEqual(rw.normalize_severity("error"), "high")
        self.assertEqual(rw.normalize_severity(None), "unknown")
        self.assertEqual(rw.cvss_to_severity("9.8"), "critical")
        self.assertEqual(rw.cvss_to_severity("7.0"), "high")
        self.assertEqual(rw.cvss_to_severity("5.3"), "medium")
        self.assertEqual(rw.cvss_to_severity(""), "unknown")
        self.assertTrue(rw.sev_at_least("critical", "high"))
        self.assertFalse(rw.sev_at_least("low", "medium"))

    def test_ignores(self):
        pats = ["Old-*", "fj/sam/scratch", "o/exact"]
        self.assertTrue(rw.repo_ignored("gh/o/Old-thing", pats))
        self.assertTrue(rw.repo_ignored("fj/sam/scratch", pats))
        self.assertTrue(rw.repo_ignored("gh/o/exact", pats))
        self.assertFalse(rw.repo_ignored("gh/o/keep", pats))
        f = finding("osv:gh/o/a:npm/x@1:GHSA-1", aliases=["CVE-2026-1"])
        self.assertTrue(rw.finding_ignored(f, ["CVE-2026-1"]))
        self.assertTrue(rw.finding_ignored(f, ["osv:gh/o/a:*"]))
        self.assertFalse(rw.finding_ignored(f, ["GHSA-2"]))

    def test_paginate(self):
        blocks = ["a" * 50, "b" * 50, "\n".join(["c" * 30] * 10)]
        msgs = rw.paginate(blocks, 120)
        self.assertTrue(all(len(m) <= 120 for m in msgs))
        self.assertEqual("".join(msgs).count("c"), 300)

    def test_wants_local_scan(self):
        c = cfg()
        fj = repo("fj/s/a", host="forgejo")
        self.assertEqual(rw.wants_local_scan(fj, {}, c), (True, True))
        gh = repo("gh/o/a")
        self.assertEqual(rw.wants_local_scan(gh, {"dependabot": "on", "secret-scanning": "off"}, c), (False, True))
        self.assertEqual(rw.wants_local_scan(dict(gh, empty=True), {}, c), (False, False))
        self.assertEqual(rw.wants_local_scan(gh, {}, cfg(local_scan={"github": False})), (False, False))
        self.assertEqual(rw.wants_local_scan(dict(gh, size_kb=10**7), {}, c), (False, False))

    def test_prune_cache(self):
        with tempfile.TemporaryDirectory() as d:
            c = cfg(cache_dir=d, cache_prune_days=30)
            old, fresh, used = (Path(d) / n for n in ("old", "fresh", "used"))
            for p in (old, fresh, used):
                p.mkdir()
            t = time.time() - 40 * 86400
            os.utime(old, (t, t))
            os.utime(used, (t, t))
            removed = rw.prune_cache(c, {used})
            self.assertEqual(removed, ["old"])
            self.assertTrue(fresh.exists() and used.exists())


class ParserTests(unittest.TestCase):
    def test_latest_ci_github(self):
        runs = [
            {"name": "CI", "head_branch": "main", "status": "in_progress", "conclusion": None, "event": "push"},
            {"name": "CI", "head_branch": "main", "status": "completed", "conclusion": "failure", "event": "push"},
            {"name": "CI", "head_branch": "main", "status": "completed", "conclusion": "success", "event": "push"},
            {"name": "Lint", "head_branch": "dev", "status": "completed", "conclusion": "failure", "event": "push"},
            {"name": "Deploy", "head_branch": "main", "status": "completed", "conclusion": "timed_out",
             "event": "push"},
        ]
        self.assertEqual(rw.latest_ci(runs, "main", "github"), {"CI": "failure", "Deploy": "failure"})

    def test_latest_ci_forgejo(self):
        runs = [
            {"workflow_id": "ci.yml", "prettyref": "main", "status": "running", "event": "push"},
            {"workflow_id": "ci.yml", "prettyref": "main", "status": "success", "event": "push"},
            {"workflow_id": "ci.yml", "prettyref": "main", "status": "failure", "event": "push"},
            {"workflow_id": "release.yml", "prettyref": "v1.0", "status": "failure", "event": "push"},
            {"workflow_id": "pr.yml", "prettyref": "main", "status": "failure", "event": "pull_request"},
        ]
        self.assertEqual(rw.latest_ci(runs, "main", "forgejo"), {"ci.yml": "success"})

    def test_parse_osv(self):
        data = {"results": [{"source": {"path": "/cache/r/web/package-lock.json"}, "packages": [{
            "package": {"name": "lodash", "version": "4.17.20", "ecosystem": "npm"},
            "vulnerabilities": [
                {"id": "GHSA-aaaa", "summary": "proto pollution", "database_specific": {"severity": "HIGH"}},
                {"id": "CVE-2026-1"},
                {"id": "GHSA-bbbb", "database_specific": {"severity": "MODERATE"}},
            ],
            "groups": [{"ids": ["CVE-2026-1", "GHSA-aaaa"], "max_severity": "9.1"},
                       {"ids": ["GHSA-bbbb"], "max_severity": ""}],
        }]}]}
        fs = rw.parse_osv(data, "fj/s/r", root="/cache/r")
        self.assertEqual(len(fs), 2)
        self.assertEqual(fs[0]["id"], "osv:fj/s/r:npm/lodash@4.17.20:GHSA-aaaa")
        self.assertEqual(fs[0]["severity"], "critical")
        self.assertIn("web/package-lock.json", fs[0]["title"])
        self.assertEqual(fs[1]["severity"], "medium")   # falls back to database_specific
        self.assertIn("CVE-2026-1", fs[0]["aliases"])

    def test_parse_gitleaks(self):
        items = [{"RuleID": "generic-api-key", "File": ".env", "StartLine": 3, "Commit": "abcdef1234567890",
                  "Fingerprint": "abcdef1234567890:.env:generic-api-key:3", "Secret": "REDACTED"}]
        f = rw.parse_gitleaks(items, "gh/o/a")[0]
        self.assertEqual(f["id"], "gitleaks:gh/o/a:generic-api-key:.env:abcdef123456")
        self.assertEqual(f["severity"], "high")
        self.assertNotIn("REDACTED", json.dumps(f))

    def test_parse_github_alerts(self):
        dep = rw.parse_dependabot([{"number": 4, "html_url": "u", "security_advisory": {
            "ghsa_id": "GHSA-x", "cve_id": "CVE-1", "summary": "bad", "severity": "moderate"},
            "security_vulnerability": {"package": {"name": "requests"}, "severity": "moderate"}}], "gh/o/a")
        self.assertEqual(dep[0]["severity"], "medium")
        self.assertEqual(rw.dependabot_title("tar", "tar: path traversal"), "tar: path traversal")
        self.assertEqual(rw.dependabot_title("tar", ""), "tar")
        self.assertEqual(dep[0]["id"], "dependabot:gh/o/a:4")
        self.assertEqual(set(dep[0]["aliases"]), {"GHSA-x", "CVE-1"})
        cs = rw.parse_code_scanning([{"number": 2, "rule": {"id": "py/sql", "security_severity_level": "critical"},
                                      "most_recent_instance": {"location": {"path": "app.py"}}}], "gh/o/a")
        self.assertEqual(cs[0]["severity"], "critical")
        ss = rw.parse_secret_scanning([{"number": 1, "secret_type_display_name": "AWS key"}], "gh/o/a")
        self.assertEqual(ss[0]["title"], "AWS key")

    def test_uptime(self):
        beats = [{"status": 1}] * 98 + [{"status": 0}] * 2 + [{"status": 3}] * 50
        self.assertEqual(rw.uptime_from_beats(beats), 98.0)
        self.assertIsNone(rw.uptime_from_beats([]))
        beats = [{"status": 1, "time": "2026-10-01 00:00:00"}, {"status": 0, "time": "2026-10-02 10:00:00"},
                 {"status": 0, "time": "2026-10-02 10:01:00"}]
        self.assertEqual(rw.status_since(beats, 0), "2026-10-02 10:00")


class StateTests(unittest.TestCase):
    def test_first_run_is_baseline(self):
        rep = report([repo("gh/o/a")], [finding("f1")], ci={"gh/o/a": {"CI": "failure"}})
        state, diff = rw.update_state(None, rep, cfg(), NOW)
        self.assertTrue(diff["first_run"])
        self.assertEqual(state["baseline"], state["last_run"])
        out = rw.render_daily(rep, state, diff, cfg(), NOW)
        self.assertIn("baseline", out)
        weekly = rw.render_weekly(rep, state, diff, cfg(), NOW)
        self.assertNotIn("🆕", weekly)

    def test_new_and_resolved(self):
        rep1 = report([repo("gh/o/a")], [finding("f1"), finding("f2")])
        s1, _ = rw.update_state(None, rep1, cfg(), NOW)
        later = NOW + dt.timedelta(days=1)
        rep2 = report([repo("gh/o/a")], [finding("f2"), finding("f3", sev="critical")])
        s2, d2 = rw.update_state(s1, rep2, cfg(), later)
        self.assertEqual([f["id"] for f in d2["new_findings"]], ["f3"])
        self.assertEqual([f["id"] for f in d2["resolved_findings"]], ["f1"])
        self.assertEqual(s2["findings"]["f2"]["first_seen"], s1["findings"]["f2"]["first_seen"])
        self.assertEqual(s2["baseline"], s1["baseline"])
        out = rw.render_daily(rep2, s2, d2, cfg(), later)
        self.assertIn("f3", out)
        weekly = rw.render_weekly(rep2, s2, d2, cfg(), later)
        self.assertIn("🆕 1", weekly)

    def test_daily_silent_when_nothing_new(self):
        rep = report([repo("gh/o/a")], [finding("f1")])
        s1, _ = rw.update_state(None, rep, cfg(), NOW)
        s2, d2 = rw.update_state(s1, rep, cfg(), NOW + dt.timedelta(days=1))
        self.assertEqual(rw.render_daily(rep, s2, d2, cfg(), NOW), "")

    def test_daily_respects_alert_severity(self):
        rep = report([repo("gh/o/a")])
        s1, _ = rw.update_state(None, rep, cfg(), NOW)
        rep2 = report([repo("gh/o/a")], [finding("low1", sev="medium")])
        s2, d2 = rw.update_state(s1, rep2, cfg(alert_severity="high"), NOW)
        self.assertEqual(rw.render_daily(rep2, s2, d2, cfg(alert_severity="high"), NOW), "")

    def test_ci_and_monitors(self):
        mon_up = {"monitors": [{"name": "Web", "project": "Box", "status": 1, "uptime": 100.0, "msg": ""}]}
        mon_down = {"monitors": [{"name": "Web", "project": "Box", "status": 0, "uptime": 90.0, "msg": "timeout"}]}
        s1, _ = rw.update_state(None, report([repo("gh/o/a")], ci={"gh/o/a": {"CI": "success"}}, monitors=mon_up),
                                cfg(), NOW)
        rep2 = report([repo("gh/o/a")], ci={"gh/o/a": {"CI": "failure"}}, monitors=mon_down)
        s2, d2 = rw.update_state(s1, rep2, cfg(), NOW)
        self.assertEqual(d2["ci_new"], ["gh/o/a::CI"])
        self.assertEqual(d2["down_new"], ["Web"])
        out = rw.render_daily(rep2, s2, d2, cfg(), NOW)
        self.assertIn("CI newly failing", out)
        self.assertIn("timeout", out)
        # Kuma unreachable: keep last known down state, don't report recovery
        rep3 = report([repo("gh/o/a")], ci={"gh/o/a": {"CI": "success"}}, monitors=None,
                      errors={"uptime_kuma": "boom"})
        s3, d3 = rw.update_state(s2, rep3, cfg(), NOW)
        self.assertEqual(s3["down"], s2["down"])
        self.assertEqual(d3["down_recovered"], [])
        self.assertEqual(d3["ci_fixed"], ["gh/o/a::CI"])
        self.assertEqual(d3["errors_new"], ["uptime_kuma"])

    def test_gitleaks_sticky_and_ignorable(self):
        gl = finding("gitleaks:gh/o/a:r:.env:abc", source="gitleaks", fingerprint="abc:.env:r:1")
        s1, _ = rw.update_state(None, report([repo("gh/o/a")], [gl]), cfg(), NOW)
        s2, d2 = rw.update_state(s1, report([repo("gh/o/a")], []), cfg(), NOW)
        self.assertIn(gl["id"], s2["findings"])      # rolled out of window: still reported
        self.assertEqual(d2["resolved_findings"], [])
        s3, d3 = rw.update_state(s2, report([repo("gh/o/a")], [], gl_ignore={"gh/o/a": ["abc:.env:r:1"]}),
                                 cfg(), NOW)
        self.assertNotIn(gl["id"], s3["findings"])   # listed in .gitleaksignore
        s4, _ = rw.update_state(s2, report([repo("gh/o/a")], []), cfg(ignore_findings=["gitleaks:gh/o/a:*"]), NOW)
        self.assertNotIn(gl["id"], s4["findings"])

    def test_errored_repo_keeps_findings(self):
        s1, _ = rw.update_state(None, report([repo("fj/s/b", "forgejo")], [finding("x", "fj/s/b")]), cfg(), NOW)
        s2, d2 = rw.update_state(s1, report([repo("fj/s/b", "forgejo")], [], errors={"osv:fj/s/b": "timeout"}),
                                 cfg(), NOW)
        self.assertIn("x", s2["findings"])
        self.assertEqual(d2["resolved_findings"], [])

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "state.json"
            s, _ = rw.update_state(None, report([repo("gh/o/a")], [finding("f1")]), cfg(), NOW)
            rw.save_state(p, s)
            self.assertEqual(rw.load_state(p), s)
            self.assertIsNone(rw.load_state(Path(d) / "missing.json"))


class RenderTests(unittest.TestCase):
    def test_weekly_fits_and_groups(self):
        repos = [repo(f"gh/o/r{i}", pushed_at="2026-10-11T00:00:00Z", open_prs=i) for i in range(40)]
        fs = [finding(f"osv:gh/o/r{i % 15}:npm/p{i}@1:GHSA-{i}", f"gh/o/r{i % 15}", sev=["critical", "high", "medium",
              "low"][i % 4]) for i in range(400)]
        fs.append(finding("gitleaks:gh/o/r1:k:.env:a", "gh/o/r1", source="gitleaks", title="k in .env:3 @a"))
        mon = {"monitors": [{"name": f"Box — svc{i}", "project": "Box", "status": 1 if i else 0,
                             "uptime": 99.5 - i, "msg": "", "since": "2026-10-11 00:00"} for i in range(10)]}
        rep = report(repos, fs, ci={"gh/o/r1": {"CI": "failure"}}, monitors=mon)
        s, d = rw.update_state(None, rep, cfg(), NOW)
        out = rw.render_weekly(rep, s, d, cfg(), NOW)
        self.assertIn("🔑", out)
        self.assertIn("…and", out)
        self.assertIn("down since 2026-10-11 00:00", out)
        self.assertLess(len(out), 4000)   # at most two Discord messages
        for line in out.splitlines():
            self.assertLess(len(line), 300)


if __name__ == "__main__":
    unittest.main()
