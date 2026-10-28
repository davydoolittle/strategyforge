"""
Regression harness (P5.1): the engine must keep reproducing what is stored.

Runs against a SCRATCH COPY of the real forge.db (sqlite backup API into a
temp dir), never the live file:
  * every candidate with passed=1 reproduces its stored is_trades, is_net,
    oos_trades, oos_net to the cent on its pinned windows (windows.verify)
  * strategy #1 (candidate 3214) reproduces its exact trade list, IS and OOS,
    against tests/fixtures/c3214_trades.json (written once, before P5.2)
  * every stored candidate's DNA still equals lineage.dna_of(its params)

The DB is FORGE_DB, else /data/forge.db, else data/forge.db in the repo. If
none exists the tests are skipped with a message saying so: this harness
needs the real data, a synthetic DB cannot stand in for it. The copy goes in
SF_SCRATCH_DIR if set (e.g. data/scratch on the share when the laptop's
temp drive is full), else the system temp dir.

  python -m unittest tests.test_regression -v
  python tests/test_regression.py --write-fixture   # only to (re)create the fixture
"""

import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import engine  # noqa: E402
import lineage  # noqa: E402
import optimizer  # noqa: E402
import strategies  # noqa: E402
import windows as W  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "c3214_trades.json")
STRATEGY_ID = 1
CANDIDATE_ID = 3214


def source_db():
    for p in (os.environ.get("FORGE_DB"), "/data/forge.db",
              os.path.join(HERE, "..", "data", "forge.db")):
        if p and os.path.exists(p):
            return p
    return None


def scratch_dir():
    base = os.environ.get("SF_SCRATCH_DIR")
    if base:
        os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="sf-regression-", dir=base)


def scratch_copy(src, dst_dir):
    dst = os.path.join(dst_dir, "forge_scratch.db")
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)
    s.close()
    d.close()
    return dst


def trade_list(conn, cid):
    pair, tf, pj = conn.execute("SELECT pair, tf, params_json FROM candidates WHERE id=?",
                                (cid,)).fetchone()
    tf_s = W.tf_seconds(tf)
    s, sp, e = W.for_candidate(conn, cid)
    cfg = W.pin_cfg(json.loads(pj), s, tf_s)
    return {"candidate_id": cid, "pair": pair, "tf": tf,
            "windows": [s, sp, e],
            "is": engine.backtest(conn, pair, cfg, s, sp, tf_s)["trades"],
            "oos": engine.backtest(conn, pair, cfg, sp, e, tf_s)["trades"]}


class RegressionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        src = source_db()
        if src is None:
            raise unittest.SkipTest("no forge.db found (FORGE_DB, /data/forge.db, data/forge.db): "
                                    "the regression harness needs the real data")
        cls.tmp = scratch_dir()
        cls.conn = sqlite3.connect(scratch_copy(src, cls.tmp))
        # family columns (P5.2) on the scratch copy, as the server's first run will
        optimizer.ensure_tables(cls.conn)
        lineage.ensure_tables(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_passed_candidates_reproduce_to_the_cent(self):
        ok, bad = W.verify(self.conn)
        n = self.conn.execute("SELECT COUNT(*) FROM candidates WHERE passed=1").fetchone()[0]
        self.assertGreater(n, 0)
        self.assertEqual(ok, n)
        self.assertEqual(bad, [], f"{len(bad)} of {n} passed candidates no longer reproduce: {bad[:5]}")

    def test_strategy_1_exact_trade_list(self):
        row = self.conn.execute("SELECT candidate_id, params_json FROM strategies WHERE id=?",
                                (STRATEGY_ID,)).fetchone()
        self.assertIsNotNone(row, "strategy #1 is missing")
        self.assertEqual(row[0], CANDIDATE_ID)
        cand = self.conn.execute("SELECT params_json FROM candidates WHERE id=?",
                                 (CANDIDATE_ID,)).fetchone()[0]
        self.assertEqual(lineage.dna_of(json.loads(row[1])), lineage.dna_of(json.loads(cand)))
        with open(FIXTURE) as f:
            want = json.load(f)
        got = json.loads(json.dumps(trade_list(self.conn, CANDIDATE_ID)))
        self.assertEqual(got["windows"], want["windows"])
        for w in ("is", "oos"):
            self.assertEqual(len(got[w]), len(want[w]), w)
            for k, (a, b) in enumerate(zip(got[w], want[w])):
                self.assertEqual(a, b, f"{w} trade {k} differs")

    def test_seeded_trials_regenerate(self):
        """Every optimizer row with a base_seed is trial k of its seed's stream
        from its family's sampler (P5.2 moved the samplers). No backtests."""
        rows = self.conn.execute(
            "SELECT seed, trial, tf, family, params_json FROM candidates"
            " WHERE base_seed IS NOT NULL AND trial IS NOT NULL ORDER BY seed, trial").fetchall()
        self.assertGreater(len(rows), 0)
        streams, bad = {}, []
        for seed, trial, tf, family, pj in rows:
            fam = strategies.get(family)
            drawn = streams.setdefault((seed, tf, family), [random.Random(seed), []])
            while len(drawn[1]) <= trial:
                drawn[1].append(fam.for_timeframe(fam.sample_cfg(drawn[0]), W.tf_seconds(tf)))
            if drawn[1][trial] != json.loads(pj):
                bad.append((seed, trial))
        self.assertEqual(bad, [], f"{len(bad)} of {len(rows)} seeded rows differ, e.g. {bad[:5]}")

    def test_stored_dna_is_stable(self):
        bad = [cid for cid, pj, dna in
               self.conn.execute("SELECT id, params_json, dna FROM candidates")
               if lineage.dna_of(json.loads(pj)) != dna]
        self.assertEqual(bad, [], f"{len(bad)} candidates' DNA changed, e.g. {bad[:5]}")


def write_fixture():
    src = source_db()
    if src is None:
        raise SystemExit("no forge.db found")
    tmp = scratch_dir()
    try:
        conn = sqlite3.connect(scratch_copy(src, tmp))
        data = trade_list(conn, CANDIDATE_ID)
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", newline="\n") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    print(f"wrote {FIXTURE}: {len(data['is'])} IS + {len(data['oos'])} OOS trades")


if __name__ == "__main__":
    if "--write-fixture" in sys.argv:
        write_fixture()
    else:
        unittest.main()
