"""Tests for code_task.py: naming, PR text, repo parsing, and the full pipeline against a local bare repo
with a local-directory sandbox and the built-in dummy agent (no network, no real agent)."""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
_CANDIDATES = [HERE.parent / "code_task.py", HERE.parent / "scripts" / "code_task.py"]
SCRIPT = Path(os.environ.get("CODE_TASK_PATH") or next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0]))
spec = importlib.util.spec_from_file_location("code_task", SCRIPT)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)


def git(*a, cwd=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@localhost", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@localhost")
    return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class Naming(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(ct.slugify("Fix the login bug in auth.py!"), "fix-the-login-bug-in-auth")
        self.assertEqual(ct.slugify("   "), "task")
        self.assertEqual(ct.slugify("\n# Task: Fix login\nmore words here"), "fix-login")
        self.assertEqual(ct.slugify("Goal: add CSV export"), "add-csv-export")
        self.assertEqual(ct.slugify("ÜBER  café -- naïve"), "ber-caf-na-ve")
        long = ct.slugify("a" * 100)
        self.assertEqual(len(long), 40)
        self.assertLessEqual(len(ct.slugify("word " * 50)), 40)

    def test_branch_name_and_collisions(self):
        when = dt.datetime(2026, 10, 7, 12, 0)
        self.assertEqual(ct.branch_name("Add dark mode", when), "hermes/add-dark-mode-20261007")
        taken = {"hermes/add-dark-mode-20261007", "hermes/add-dark-mode-20261007-2"}
        self.assertEqual(ct.branch_name("Add dark mode", when, taken=taken), "hermes/add-dark-mode-20261007-3")
        self.assertEqual(ct.branch_name("x", when, prefix="bot/"), "bot/x-20261007")

    def test_job_id(self):
        jid = ct.new_job_id(dt.datetime(2026, 10, 7, 1, 2, 3))
        self.assertRegex(jid, r"^20261007-010203-[0-9a-f]{6}$")
        self.assertEqual(ct.check_job_id(jid), jid)
        for bad in ("../etc", "20261007-010203-zzzzzz", "", "x; rm -rf /"):
            with self.assertRaises(SystemExit):
                ct.check_job_id(bad)


class Text(unittest.TestCase):
    def test_pr_title(self):
        self.assertEqual(ct.pr_title("fix bug\nmore detail"), "Fix bug")
        self.assertEqual(ct.pr_title("## Task: add CSV export\n..."), "Add CSV export")
        t = ct.pr_title("x" * 200)
        self.assertEqual(len(t), 72)
        self.assertTrue(t.endswith("…"))

    def test_pr_body(self):
        body = ct.pr_body(task="Do X\nand Y", summary="## What changed\nstuff", agent_label="Codex",
                          job_id="20261007-010203-abcdef", diffstat=" a.py | 2 +-\n 1 file changed",
                          log_excerpt="line ```1```", elapsed_s=125)
        self.assertIn("> Do X\n> and Y", body)
        self.assertIn("## What changed", body)
        self.assertIn("1 file changed", body)
        self.assertIn("never auto-merged", body)
        self.assertIn("2m05s", body)
        self.assertNotIn("```1```", body)          # log fences neutralised
        empty = ct.pr_body(task="t", summary="", agent_label="Codex", job_id="j", diffstat="", log_excerpt="",
                           elapsed_s=1)
        self.assertIn("did not write HERMES_SUMMARY.md", empty)
        self.assertNotIn("Agent log", empty)

    def test_result_line(self):
        line = ct.result_line("done", "J", pr="https://x/pr/1", branch="hermes/b", note="1 file\nchanged")
        self.assertEqual(line, "CODE_TASK_RESULT status=done job=J pr=https://x/pr/1 branch=hermes/b note=1 file changed")
        self.assertIn("pr=- branch=- note=-", ct.result_line("failed", "J"))

    def test_tail_and_clean(self):
        self.assertEqual(ct.tail("\n".join(str(i) for i in range(100)), 3), "97\n98\n99")
        self.assertEqual(ct.clean_log("\x1b[31mred\x1b[0m\r\n"), "red\n")


class RepoParsing(unittest.TestCase):
    FJ = "https://git.example.com"

    def test_prefixes_and_urls(self):
        r = ct.parse_repo("forgejo:me/proj", self.FJ)
        self.assertEqual((r.kind, r.full, r.clone_url), ("forgejo", "me/proj", "https://git.example.com/me/proj.git"))
        self.assertEqual(ct.parse_repo("github:me/proj", self.FJ).clone_url, "https://github.com/me/proj.git")
        self.assertEqual(ct.parse_repo("https://github.com/me/proj.git", self.FJ).kind, "github")
        self.assertEqual(ct.parse_repo("https://git.example.com/me/proj", self.FJ).kind, "forgejo")
        with self.assertRaises(ValueError):
            ct.parse_repo("https://evil.example.org/me/proj", self.FJ)
        with self.assertRaises(ValueError):
            ct.parse_repo("me/proj/extra", self.FJ)
        with self.assertRaises(ValueError):
            ct.parse_repo("forgejo:me/proj", "")

    def test_bare_name_resolution(self):
        self.assertEqual(ct.parse_repo("me/p", self.FJ, lambda o, n: True).kind, "forgejo")
        self.assertEqual(ct.parse_repo("me/p", self.FJ, lambda o, n: False).kind, "github")
        self.assertEqual(ct.parse_repo("me/p", "").kind, "github")
        self.assertEqual(ct.parse_repo("/tmp/x.git").kind, "local")

    def test_git_env_keeps_token_out_of_argv(self):
        env = ct.git_env("Authorization: token SECRET")
        vals = [env[k] for k in env if k.startswith("GIT_CONFIG_VALUE_")]
        self.assertIn("Authorization: token SECRET", vals)
        self.assertIn("/dev/null", vals)                       # hooks disabled
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")


@unittest.skipUnless(shutil.which("rsync") and shutil.which("timeout") and shutil.which("git"),
                     "needs rsync, GNU timeout and git")
class Pipeline(unittest.TestCase):
    """Full run: clone -> sandbox (local dir) -> dummy agent -> pull back -> commit (-> push/PR mocked)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.bare = t / "origin.git"
        git("init", "-q", "--bare", "-b", "main", str(self.bare))
        seed = t / "seed"
        git("clone", "-q", str(self.bare), str(seed))
        (seed / "README.md").write_text("hello\n")
        (seed / ".gitignore").write_text("build/\n")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "init", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.cfg = ct.deep_merge(ct.DEFAULT_CONFIG, {
            "sandbox": f"local:{t / 'sandbox'}", "work_dir": str(t / "work"), "state_dir": str(t / "state"),
            "gitleaks": "off", "forgejo": {"url": "", "url_var": "", "token_var": ""},
        })
        (t / "sandbox").mkdir()
        self.t = t

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, cfg=None):
        cfgp = self.t / "cfg.json"
        cfgp.write_text(json.dumps(cfg or self.cfg))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ct.main(["--config", str(cfgp), *argv])
        lines = out.getvalue().strip().splitlines()
        self.assertTrue(lines and lines[-1].startswith("CODE_TASK_RESULT "), out.getvalue() + err.getvalue())
        return rc, lines[-1], out.getvalue()

    def job(self, line):
        jid = line.split(" job=")[1].split()[0]
        return jid, json.loads((self.t / "state" / jid / "job.json").read_text())

    def test_dummy_run_commits_without_push(self):
        rc, line, _ = self.run_cli("run", "--repo", str(self.bare), "--task", "Add a dummy file", "--agent", "dummy")
        self.assertEqual(rc, 0, line)
        self.assertIn("status=committed", line)
        jid, data = self.job(line)
        self.assertTrue(data["branch"].startswith("hermes/add-a-dummy-file-"))
        repo = self.t / "work" / jid / "repo"
        files = git("show", "--name-only", "--format=%an <%ae>%n%s", "HEAD", cwd=repo)
        self.assertIn("Hermes (via Dummy) <hermes@localhost>", files)
        self.assertIn("HERMES_DUMMY.md", files)
        self.assertNotIn("HERMES_SUMMARY.md", files)                     # summary not committed
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip(), data["branch"])
        self.assertIn("Pipeline test.", (self.t / "state" / jid / "HERMES_SUMMARY.md").read_text())
        self.assertIn("dummy agent: got", (self.t / "state" / jid / "agent.log").read_text())
        self.assertIn("Add a dummy file", (self.t / "state" / jid / "PROMPT.md").read_text())
        self.assertFalse((self.t / "sandbox" / jid).exists())             # sandbox cleaned
        self.assertEqual(git("branch", "-a", "--format=%(refname:short)", cwd=self.bare).split(), ["main"])
        self.assertNotIn("extraheader", (repo / ".git" / "config").read_text().lower())

    def test_timeout(self):
        rc, line, _ = self.run_cli("run", "--repo", str(self.bare), "--task", "slow", "--agent", "dummy",
                                   "--timeout", "1", "--dummy-sleep", "20")
        self.assertEqual(rc, 3)
        self.assertIn("status=timeout", line)
        jid, data = self.job(line)
        self.assertEqual(data["status"], "timeout")
        self.assertFalse((self.t / "sandbox" / jid).exists())

    def _custom_agent(self, name, script):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["agents"][name] = {"label": name.title(), "command": ["bash", "-c", script]}
        return cfg

    def test_no_changes_and_failure(self):
        rc, line, _ = self.run_cli("run", "--repo", str(self.bare), "--task", "nothing", "--agent", "noop",
                                   cfg=self._custom_agent("noop", "cat >/dev/null; echo nothing to do"))
        self.assertEqual((rc, "status=no_changes" in line), (0, True), line)
        rc, line, _ = self.run_cli("run", "--repo", str(self.bare), "--task", "boom", "--agent", "boom",
                                   cfg=self._custom_agent("boom", "echo not logged in; exit 7"))
        self.assertEqual(rc, 4)
        self.assertIn("status=failed", line)
        self.assertIn("not logged in", line)

    def test_preflight_failure_is_fast_and_clear(self):
        cfg = self._custom_agent("pf", "true")
        cfg["agents"]["pf"]["preflight"] = ["false"]
        cfg["agents"]["pf"]["preflight_hint"] = "log in first"
        rc, line, _ = self.run_cli("run", "--repo", str(self.bare), "--task", "x", "--agent", "pf", cfg=cfg)
        self.assertEqual(rc, 1)
        self.assertIn("log in first", line)

    def test_sandbox_cannot_touch_git_or_escape(self):
        evil = ("cat >/dev/null; echo 'touch /tmp/pwned' > .git/hooks/pre-commit; chmod +x .git/hooks/pre-commit; "
                "ln -s /etc/passwd leak; mkdir -p build; echo x > build/out; echo ok > real.txt")
        rc, line, _ = self.run_cli("run", "--repo", str(self.bare), "--task", "evil", "--agent", "evil",
                                   cfg=self._custom_agent("evil", evil))
        self.assertEqual(rc, 0, line)
        jid, _ = self.job(line)
        repo = self.t / "work" / jid / "repo"
        self.assertFalse((repo / ".git" / "hooks" / "pre-commit").exists())
        self.assertFalse((repo / "leak").exists())                       # unsafe symlink not pulled
        self.assertFalse((repo / "build").exists())                      # .gitignore honoured
        self.assertEqual(git("show", "--name-only", "--format=", "HEAD", cwd=repo).split(), ["real.txt"])

    def test_push_and_pr_go_to_new_branch_only(self):
        spec = ct.RepoSpec("forgejo", "me", "proj", clone_url=str(self.bare), web_base="https://git.example.com")
        calls = {}

        def fake_pr(repo, cfg, **kw):
            calls.update(kw)
            return "https://git.example.com/me/proj/pulls/1"

        with mock.patch.object(ct, "parse_repo", return_value=spec), mock.patch.object(ct, "open_pr", fake_pr):
            rc, line, out = self.run_cli("run", "--repo", "me/proj", "--task", "Ship it", "--agent", "dummy")
        self.assertEqual(rc, 0, out)
        self.assertIn("status=done", line)
        self.assertIn("pr=https://git.example.com/me/proj/pulls/1", line)
        jid, data = self.job(line)
        heads = git("branch", "--format=%(refname:short)", cwd=self.bare).split()
        self.assertEqual(sorted(heads), sorted(["main", data["branch"]]))
        self.assertEqual(git("log", "--format=%s", "main", cwd=self.bare).split("\n")[0], "init")  # base untouched
        self.assertEqual((calls["head"], calls["base"], calls["title"]), (data["branch"], "main", "Ship it"))
        self.assertIn("Pipeline test.", calls["body"])
        self.assertFalse((self.t / "work" / jid).exists())               # workdir removed on success
        # second job with the same task gets a fresh branch name
        with mock.patch.object(ct, "parse_repo", return_value=spec), mock.patch.object(ct, "open_pr", fake_pr):
            rc, line2, _ = self.run_cli("run", "--repo", "me/proj", "--task", "Ship it", "--agent", "dummy")
        self.assertTrue(self.job(line2)[1]["branch"].endswith("-2"))

    def test_status_and_list(self):
        _, line, _ = self.run_cli("run", "--repo", str(self.bare), "--task", "Add a dummy file", "--agent", "dummy")
        jid, _ = self.job(line)
        cfgp = self.t / "cfg.json"
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ct.main(["--config", str(cfgp), "status", jid]), 0)
            self.assertEqual(ct.main(["--config", str(cfgp), "list"]), 0)
        self.assertIn("committed", out.getvalue())
        self.assertIn(jid, out.getvalue())


if __name__ == "__main__":
    unittest.main()
