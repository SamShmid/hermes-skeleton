#!/usr/bin/env python3
"""Cron wrapper: repo_watch.py --mode weekly (cron jobs can't pass script arguments).

Uses <scripts dir>/../config/repo_watch.json when it exists (e.g. ~/.hermes/config/), so it
works even if HERMES_HOME isn't set in the cron environment.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_watch  # noqa: E402

args = ["--mode", "weekly"]
cfg = HERE.parent / "config" / "repo_watch.json"
if cfg.exists() and "--config" not in sys.argv:
    args += ["--config", str(cfg)]
sys.exit(repo_watch.main(args + sys.argv[1:]))
