"""Offline tests for research_digest parsing, scoring, dedupe, LLM-rerank fallback and seen state (no network)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_CANDIDATES = [HERE.parent / "research_digest.py", HERE.parent / "scripts" / "research_digest.py"]
SCRIPT = Path(os.environ.get("RESEARCH_DIGEST_PATH") or next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0]))
spec = importlib.util.spec_from_file_location("research_digest", SCRIPT)
rd = importlib.util.module_from_spec(spec)
sys.modules["research_digest"] = rd  # dataclasses need the module registered
spec.loader.exec_module(rd)


ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>http://arxiv.org/abs/2610.00001v2</id><published>2026-10-06T17:00:00Z</published>
  <title>Sparse Attention for Small Language Models</title>
  <summary>We propose a sparse attention architecture. It is fast.</summary>
  <category term="cs.CL"/><category term="cs.LG"/></entry>
 <entry><id>http://arxiv.org/abs/2610.00002v1</id><published>2026-10-06T17:00:00Z</published>
  <title>Protein folding with diffusion</title><summary>Biology.</summary></entry>
</feed>"""

RSS_XML = """<rss><channel>
<item><title>Zero-day exploited in popular VPN</title><link>https://sec.example/a?utm_source=rss</link>
<description>Attackers exploit a zero-day.</description><pubDate>Wed, 07 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Popular VPN zero-day exploited</title><link>https://sec.example/b</link>
<description>Same story.</description><pubDate>Wed, 07 Oct 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""

CONFIG = {
    'topics': [
        {'name': 'LLM architecture', 'weight': 1.0, 'applies_to': ['paper', 'AI'],
         'keywords': {'sparse attention': 3, 'small language model': 3, 'agent': 1}},
        {'name': 'Security', 'keywords': ['zero-day', 'exploit']},
        {'name': 'Fingerprinting', 'keywords': ['fingerprinting'], 'exclude': ['radio frequency']},
    ],
    'negative_keywords': ['webinar'],
    'papers': {'min_score': 2.5, 'max_age_days': 30},
    'news': {'min_score': 1, 'max_age_hours': 1000, 'quotas': {'Security': 1}},
    'llm_rerank': {'enabled': True, 'task': 'research_rank'},
}


class TestResearchDigest(unittest.TestCase):
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

    def test_parse_sources(self):
        papers = rd.parse_arxiv(ARXIV_XML)
        self.assertEqual(papers[0].key, 'arxiv:2610.00001')
        self.assertEqual(papers[0].url, 'https://arxiv.org/abs/2610.00001')
        self.assertEqual(papers[0].category, 'cs.CL, cs.LG')
        hf = rd.parse_hf_daily([{'paper': {'id': '2610.00001', 'title': 'Sparse Attention for Small Language Models',
                                           'upvotes': 40, 'summary': 's'}}, {'paper': {'id': 'bad'}}])
        self.assertEqual([(h.key, h.upvotes) for h in hf], [('arxiv:2610.00001', 40)])
        news = rd.parse_feed(RSS_XML, {'name': 'SecFeed', 'category': 'Security', 'base_score': 1})
        self.assertEqual(news[0].key, 'url:https://sec.example/a')
        self.assertEqual(news[0].base, 1.0)

    def test_scoring_title_double_applies_to_and_exclude(self):
        scorer = rd.Scorer(CONFIG)
        p = rd.Item('paper', 'k', 'Sparse attention', '', 'arXiv', summary='')
        q = rd.Item('paper', 'k2', 'Something', '', 'arXiv', summary='uses sparse attention')
        scorer.score(p, self.now), scorer.score(q, self.now)
        self.assertEqual(p.score, 6.0)
        self.assertEqual(q.score, 3.0)
        world = rd.Item('news', 'k3', 'Police agent arrested', '', 'NYT', category='World')
        self.assertEqual(scorer.score(world, self.now), 0)                  # LLM topic doesn't apply to World
        rf = rd.Item('paper', 'k4', 'RF fingerprinting', '', 'arXiv', summary='radio frequency devices')
        self.assertEqual(scorer.score(rf, self.now), 0)                     # topic exclude
        promo = rd.Item('news', 'k5', 'Zero-day webinar', '', 'X', category='Security')
        self.assertLess(scorer.score(promo, self.now), 0)                   # negative keyword

    def test_merge_dedupe_and_seen(self):
        papers = rd.parse_arxiv(ARXIV_XML) + rd.parse_hf_daily(
            [{'paper': {'id': '2610.00001', 'title': 'Sparse Attention for Small Language Models', 'upvotes': 40}}])
        merged = rd.merge_and_dedupe(papers, {})
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].upvotes, 40)
        self.assertIn('HF Daily Papers', merged[0].source)
        news = rd.merge_and_dedupe(rd.parse_feed(RSS_XML, {'name': 'S', 'category': 'Security'}), {})
        self.assertEqual(len(news), 1)                                       # near-identical titles collapse
        seen = rd.mark_seen({}, merged[:1], self.now)
        self.assertEqual([p.key for p in rd.merge_and_dedupe(papers, seen)], ['arxiv:2610.00002'])

    def test_select_quota_and_per_topic(self):
        items = [rd.Item('news', f'k{i}', f't{i}', '', 's', category='AI', score=10 - i, topics=['LLM']) for i in range(4)]
        items.append(rd.Item('news', 'sec', 'sec', '', 's', category='Security', score=1.5, topics=['Security']))
        chosen = rd.select(items, 3, 1, quotas={'Security': 1})
        self.assertIn('sec', [c.key for c in chosen])
        chosen = rd.select(items, 3, 1, per_topic=2)
        self.assertEqual([c.key for c in chosen], ['k0', 'k1', 'sec'])

    def test_llm_rerank_validates_and_falls_back(self):
        cands = [rd.Item('paper', f'arxiv:{i}', f'Paper {i}', f'https://arxiv.org/abs/{i}', 'arXiv', summary='s')
                 for i in range(3)]
        fake = lambda task, system, user, timeout: ('{"picks": [{"id": "p2", "why": "Big result."}, '
                                                    '{"id": "zzz", "why": "x"}, {"id": "p0", "why": ""}]}')
        out = rd.llm_rerank(cands, 2, 'papers', CONFIG, llm=fake)
        self.assertEqual([o.title for o in out], ['Paper 2', 'Paper 0'])
        self.assertEqual(out[0].why, 'Big result.')

        def boom(*a):
            raise RuntimeError('no runtime')
        self.assertIsNone(rd.llm_rerank(cands, 2, 'papers', CONFIG, llm=boom))
        self.assertIsNone(rd.llm_rerank(cands, 2, 'papers', CONFIG, llm=lambda *a: 'not json'))
        self.assertIn('"picks"', rd.RERANK_PROMPT)
        seen_prompts = []

        def capture(task, system, user, timeout):
            seen_prompts.append((task, system, json.loads(user)))
            return '{"picks": [{"id": "n0", "why": "ok"}]}'
        news = [rd.Item('news', 'url:a', 'A', 'https://a', 'S', category='World', summary='x')]
        rd.llm_rerank(news, 1, 'news stories', CONFIG, llm=capture, quotas={'World': 1, 'Security': 2})
        task, system, user = seen_prompts[0]
        self.assertEqual(task, 'research_rank')
        self.assertIn('include at least 1 from World', system)
        self.assertNotIn('from Security', system)                          # no Security candidates -> not asked
        self.assertEqual(user[0]['category'], 'World')

    def test_build_digest_end_to_end_and_render(self):
        fetchers = {'arxiv': lambda cfg, t, w: rd.parse_arxiv(ARXIV_XML),
                    'hf': lambda cfg, t, w: [],
                    'feeds': lambda feeds, t, w: rd.parse_feed(RSS_XML, {'name': 'SecFeed', 'category': 'Security'})}
        papers, news, warnings = rd.build_digest(CONFIG, {}, n_papers=5, n_news=5, use_llm=False, now=self.now,
                                                 fetchers=fetchers)
        self.assertEqual([p.key for p in papers], ['arxiv:2610.00001'])     # protein paper scores 0 -> dropped
        self.assertEqual(len(news), 1)
        self.assertTrue(papers[0].why.startswith('LLM architecture: We propose'))
        text = rd.render(papers, news, 'discord')
        self.assertIn('## 🔬 Research & news picks', text)
        self.assertIn('[**Sparse Attention for Small Language Models**](<https://arxiv.org/abs/2610.00001>)', text)
        self.assertIn('_· SecFeed_', text)
        data = json.loads(rd.render(papers, news, 'json'))
        self.assertEqual(data['papers'][0]['key'], 'arxiv:2610.00001')

    def test_seen_state_roundtrip_prunes_old(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'seen.json'
            old = (self.now - timedelta(days=rd.SEEN_KEEP_DAYS + 5)).date().isoformat()
            rd.save_seen(path, {'arxiv:old': old, 'arxiv:new': self.now.date().isoformat()}, self.now)
            self.assertEqual(list(rd.load_seen(path)), ['arxiv:new'])
            self.assertEqual(rd.load_seen(Path(d) / 'missing.json'), {})

    def test_dry_run_main_writes_no_state(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / 'interests.json'
            cfg.write_text(json.dumps(dict(CONFIG, papers={'arxiv_queries': [], 'huggingface_daily': {'enabled': False}},
                                           news={'feeds': []})))
            state = Path(d) / 'seen.json'
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rd.main(['--config', str(cfg), '--state', str(state), '--dry-run', '--no-llm'])
            self.assertEqual(rc, 0)
            self.assertFalse(state.exists())
            self.assertIn('Nothing new that matches your interests today.', buf.getvalue())



class TestExampleConfig(unittest.TestCase):
    def test_example_config_loads_and_scores(self):
        cfg = rd.load_config(HERE.parent / 'config' / 'research_interests.example.json')
        scorer = rd.Scorer(cfg)
        item = rd.Item('paper', 'k', 'Canvas fingerprinting in the wild', '', 'arXiv')
        self.assertGreater(scorer.score(item, datetime.now(timezone.utc)), 3)
        self.assertTrue(all(f['url'].startswith('https://') for f in cfg['news']['feeds']))


if __name__ == '__main__':
    unittest.main()
