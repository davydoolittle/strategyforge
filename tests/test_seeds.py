"""
Tests for per-pair seeds and trial DNA (P2.2). No real DB needed.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import random
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import lineage  # noqa: E402
import optimizer  # noqa: E402

PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD"]


class TestSeedForPair(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(optimizer.seed_for_pair(42, "BTC-USD", PAIRS), 42000)
        self.assertEqual(optimizer.seed_for_pair(42, "SOL-USD", PAIRS), 42002)

    def test_unique_across_pairs_and_bases(self):
        seeds = [optimizer.seed_for_pair(b, p, PAIRS) for b in (1, 2, 42, 1337) for p in PAIRS]
        self.assertEqual(len(seeds), len(set(seeds)))

    def test_pairs_draw_different_parameter_sets(self):
        """The pre-P2.2 bug: trial k was identical on every pair."""
        dnas = {}
        for p in PAIRS:
            rng = random.Random(optimizer.seed_for_pair(42, p, PAIRS))
            dnas[p] = [lineage.dna_of(optimizer.sample_cfg(rng)) for _ in range(50)]
        for a in PAIRS:
            for b in PAIRS:
                if a < b:
                    same = sum(x == y for x, y in zip(dnas[a], dnas[b]))
                    self.assertEqual(same, 0, f"{a} and {b} share trials")

    def test_seed_stable_when_pairs_appended(self):
        before = optimizer.seed_for_pair(7, "SOL-USD", PAIRS[:3])
        after = optimizer.seed_for_pair(7, "SOL-USD", PAIRS)
        self.assertEqual(before, after)

    def test_unknown_pair_refused(self):
        with self.assertRaises(SystemExit):
            optimizer.seed_for_pair(42, "DOGE-USD", PAIRS)


class TestDnaColumn(unittest.TestCase):
    def test_backfill_dna_and_duplicates_visible(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        optimizer.ensure_tables(conn)
        cfg = optimizer.sample_cfg(random.Random(1))
        for pair in ("BTC-USD", "ETH-USD"):                   # old same-seed behaviour
            conn.execute("INSERT INTO candidates (pair, tf, params_json) VALUES (?, '1h', ?)",
                         (pair, json.dumps(cfg)))
        self.assertEqual(optimizer.backfill_dna(conn), 2)
        dnas = [r[0] for r in conn.execute("SELECT dna FROM candidates")]
        self.assertEqual(dnas, [lineage.dna_of(cfg)] * 2)
        self.assertEqual(optimizer.backfill_dna(conn), 0)     # idempotent


if __name__ == "__main__":
    unittest.main()
