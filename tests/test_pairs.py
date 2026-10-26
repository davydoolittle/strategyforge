"""
Tests for PAIRS as the single, append-only source of pairs (P2.4.2).
Run from the repo root:   python -m unittest discover -s tests -v
"""

import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import main  # noqa: E402
import optimizer  # noqa: E402

# Every pair ever added, in order. When a pair is appended to main.PAIRS,
# append it here too. Never edit or reorder the existing entries: their
# positions are the per-pair seed offsets of every candidate already stored.
FROZEN_PREFIX = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"]


class TestPairsList(unittest.TestCase):
    def test_append_only(self):
        self.assertEqual(main.PAIRS[:len(FROZEN_PREFIX)], FROZEN_PREFIX)

    def test_no_duplicates(self):
        self.assertEqual(len(main.PAIRS), len(set(main.PAIRS)))

    def test_xrp_seed(self):
        self.assertEqual(optimizer.seed_for_pair(42, "XRP-USD"), 42003)
        self.assertEqual(optimizer.seed_for_pair(42, "BTC-USD"), 42000)  # unchanged


class TestEnvIgnored(unittest.TestCase):
    def test_env_var_does_not_change_pairs(self):
        with mock.patch.dict(os.environ, {"PAIRS": "SOL-USD,BTC-USD"}):
            reloaded = importlib.reload(main)
            self.assertEqual(reloaded.PAIRS[:len(FROZEN_PREFIX)], FROZEN_PREFIX)
        importlib.reload(main)

    def test_warning_logged_only_when_set(self):
        with mock.patch.object(main, "log") as log:
            with mock.patch.dict(os.environ, {"PAIRS": "BTC-USD"}):
                self.assertTrue(main.warn_if_pairs_env())
            self.assertIn("IGNORED", log.call_args[0][0])
            log.reset_mock()
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertFalse(main.warn_if_pairs_env())
            log.assert_not_called()


class TestOptimizerDefault(unittest.TestCase):
    def test_default_pairs_come_from_main(self):
        self.assertEqual(optimizer.parse_args([]).pairs.split(","), main.PAIRS)

    def test_explicit_pairs_still_work(self):
        self.assertEqual(optimizer.parse_args(["--pairs", "BTC-USD"]).pairs, "BTC-USD")


if __name__ == "__main__":
    unittest.main()
