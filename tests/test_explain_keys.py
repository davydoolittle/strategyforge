"""
Every explanation key the dashboard can ask for must exist in
app/static/explain.json, so no (i) button ever shows "no explanation for ...".
The keys come from tests/explain_keys.py: literal ones in index.html, the ones
it builds at runtime (statuses, verdicts, trend words, attacks, tracker
columns and screens) and the ones web.py's panels emit (market context rows,
regime states).
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import explain_keys as K  # noqa: E402


class TestEveryKeyHasAnEntry(unittest.TestCase):
    def test_no_referenced_key_is_missing(self):
        missing = sorted(K.referenced_keys() - set(K.explain()))
        self.assertEqual(missing, [], f"explain.json has no entry for {missing}")

    def test_the_collector_finds_the_known_kinds(self):
        ref = K.referenced_keys()
        for k in ("panel_lineage", "panel_tracker", "A2", "st_backtest_winner", "pat_suggestive", "bull",
                  "t_pe", "t_screen_momentum", "vix", "trend_up_calm", "col_wf", "sizing_context"):
            self.assertIn(k, ref)


if __name__ == "__main__":
    unittest.main()
