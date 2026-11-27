"""
Tests for the dashboard's glossary and provenance (app/static/glossary.json):
every panel has an entry naming where its data comes from, every (i) key the
page uses resolves, entries are short and plain, and none reads as a trade call.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lineage  # noqa: E402
from test_market_context import TRADE_CALL  # noqa: E402

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def load(name):
    with open(os.path.join(STATIC, name)) as f:
        return json.load(f)


class TestGlossary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g, cls.e = load("glossary.json"), load("explain.json")
        with open(os.path.join(STATIC, "index.html")) as f:
            cls.page = f.read()

    def test_every_panel_has_an_entry_with_provenance(self):
        panels = re.findall(r'<h2 data-g="([a-z_]+)"', self.page)
        self.assertEqual(len(re.findall(r"<h2", self.page)), len(panels), "a panel heading has no glossary key")
        for k in panels:
            self.assertIn(k, self.g)
            if k != "panel_glossary":
                pv = self.g[k].get("provenance") or {}
                self.assertEqual(set(pv), {"from", "made_by", "updated"}, k)
                self.assertTrue(all(pv.values()), k)

    def test_every_key_the_page_uses_resolves(self):
        # literal keys; a prefix such as "st_" + status is checked in full below
        keys = {k for k in re.findall(r'infoBtn\("([A-Za-z0-9_]+)"', self.page) if not k.endswith("_")}
        header = re.search(r"const HEADER_KEYS = \{(.*?)\};", self.page, re.S).group(1)
        keys |= set(re.findall(r':\s*"([a-z0-9_]+)"', header))
        keys |= {"st_" + s for s in lineage.LADDER + [lineage.TERMINAL]}                  # lineage status words
        keys |= {"pat_" + s for s in ("supported", "suggestive", "not_supported", "opposite", "too_few")}
        keys |= {"bull", "bear", "flat"} | {f"A{i}" for i in range(1, 10)}
        missing = {k for k in keys if k not in self.g and k not in self.e}
        self.assertEqual(missing, set())

    def test_short_plain_and_no_trade_calls(self):
        for k, v in self.g.items():
            self.assertTrue({"term", "remember", "plain"} <= set(v), k)
            self.assertTrue(v["term"] and v["remember"] and v["plain"], k)
            self.assertLessEqual(len(v["remember"].split()), 20, k)
            self.assertLessEqual(len(v["plain"].split()), 60, k)
            for text in (v["term"], v["remember"], v["plain"], json.dumps(v.get("provenance", {}))):
                self.assertIsNone(TRADE_CALL.search(text), f"{k}: {text}")
            if "explain" in v:
                self.assertIn(v["explain"], self.e, k)


if __name__ == "__main__":
    unittest.main()
