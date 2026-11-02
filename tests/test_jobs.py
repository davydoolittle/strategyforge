"""
Tests for P4.5: main.JobRunner (the jobs table and the scheduler the
collector loop calls). Jobs are real child processes running tiny scripts
in a temp app dir against a temp database file.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import main  # noqa: E402

SCRIPTS = {
    "ok.py": "import os, sys, sqlite3\n"
             "c = sqlite3.connect(os.environ['FORGE_DB'])\n"
             "c.execute('CREATE TABLE IF NOT EXISTS ran (args TEXT)')\n"
             "c.execute('INSERT INTO ran VALUES (?)', (' '.join(sys.argv[1:]),))\n"
             "c.commit()\nprint('all good')\n",
    "fail.py": "print('working...')\nraise ValueError('boom in the job')\n",
    "slow.py": "import time\ntime.sleep(60)\n",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = os.path.join(self.dir, "app")
        os.makedirs(self.app)
        for name, body in SCRIPTS.items():
            with open(os.path.join(self.app, name), "w") as f:
                f.write(body)
        self.db_path = os.path.join(self.dir, "forge.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("CREATE TABLE poll_errors (ts INTEGER, source TEXT, error TEXT)")
        self.runners = []

    def tearDown(self):
        for r in self.runners:
            for proc, _, fh in r.running.values():
                proc.kill()
                proc.wait()
                fh.close()
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def runner(self, specs):
        r = main.JobRunner(self.conn, specs, app_dir=self.app,
                           log_dir=os.path.join(self.dir, "jobs"), db_path=self.db_path)
        self.runners.append(r)
        return r

    def settle(self, r, now, timeout=20):
        """step() until no job is running (real time passes; `now` is fixed)."""
        end = time.time() + timeout
        while r.running and time.time() < end:
            time.sleep(0.1)
            r.step(now)
        self.assertEqual(r.running, {}, "job did not finish")

    def errors(self):
        return self.conn.execute("SELECT source, error FROM poll_errors").fetchall()

    def job(self, name):
        cur = self.conn.execute("SELECT * FROM jobs WHERE name=?", (name,))
        return dict(zip([c[0] for c in cur.description], cur.fetchone()))


class TestInterval(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(main.parse_interval("15m"), 900)
        self.assertEqual(main.parse_interval("1h"), 3600)
        self.assertEqual(main.parse_interval("7d"), 604800)
        for bad in ("", "h", "0h", "1w", "1.5h", "-1h"):
            with self.assertRaises(ValueError):
                main.parse_interval(bad)

    def test_real_specs(self):
        names = {n: (iv, cmd) for n, iv, cmd, _ in main.JOB_SPECS}
        self.assertEqual(names["paper"], ("1h", "paper.py"))
        self.assertEqual(names["health"], ("1d", "health.py"))
        self.assertEqual(names["feature_evals"], ("1d", "feature_evals.py --annotate-new"))
        self.assertEqual(names["adversary"], ("7d", "adversary.py --top 20"))
        self.assertEqual(names["tsp"], ("6h", "tsp_report.py --update --notify"))
        self.assertEqual(names["screen"], ("mon@06:00", "screen.py"))
        self.assertEqual(names["patterns"], ("sun@22:00", "patterns.py --refresh"))
        # the ETF bar refresh is scheduled to finish (timeout included) before the screen
        sched = {n: (main.parse_schedule(iv)[1], t) for n, iv, _, t in main.JOB_SPECS}
        (pd, ph, pm), ptimeout = sched["patterns"]
        (sd, sh, sm), _ = sched["screen"]
        gap = ((sd - pd) % 7) * 86400 + (sh - ph) * 3600 + (sm - pm) * 60
        self.assertGreater(gap, ptimeout)
        app = os.path.join(os.path.dirname(__file__), "..", "app")
        for _, iv, cmd, timeout in main.JOB_SPECS:
            main.parse_schedule(iv)
            self.assertTrue(os.path.exists(os.path.join(app, cmd.split()[0])), cmd)
            self.assertGreater(timeout, 0)


class TestWeekly(unittest.TestCase):
    MON_0600 = 1793599200.0          # Mon 2026-11-02 06:00:00 UTC

    def test_parse(self):
        self.assertEqual(main.parse_schedule("mon@06:00"), (604800, (0, 6, 0)))
        self.assertEqual(main.parse_schedule("sun@23:59"), (604800, (6, 23, 59)))
        self.assertEqual(main.parse_schedule("7d"), (604800, None))
        for bad in ("monday@06:00", "mon@6", "mon@24:00", "mon@06:60", "@06:00", "mon@"):
            with self.assertRaises(ValueError, msg=bad):
                main.parse_schedule(bad)

    def test_last_slot(self):
        wk = (0, 6, 0)
        self.assertEqual(main.last_weekly_slot(self.MON_0600, wk), self.MON_0600)
        self.assertEqual(main.last_weekly_slot(self.MON_0600 - 1, wk), self.MON_0600 - 7 * 86400)
        self.assertEqual(main.last_weekly_slot(self.MON_0600 + 3 * 86400, wk), self.MON_0600)
        self.assertEqual(main.last_weekly_slot(self.MON_0600, (2, 0, 0)), self.MON_0600 - 5 * 86400 - 6 * 3600)


class TestRunner(Base):
    def test_weekly_job_runs_once_per_slot(self):
        mon = TestWeekly.MON_0600
        r = self.runner([("wk", "mon@06:00", "ok.py", 60)])
        r.step(mon + 2 * 86400)                              # first ever (a Wednesday): runs at once
        self.assertIn("wk", r.running)
        self.settle(r, mon + 2 * 86400)
        for t in (mon + 3 * 86400, mon + 7 * 86400 - 1):     # rest of the week: not due
            r.step(t)
            self.assertEqual(r.running, {}, t)
        r.step(mon + 7 * 86400)                              # next Monday 06:00: due
        self.assertIn("wk", r.running)
        self.settle(r, mon + 7 * 86400)
        r.step(mon + 7 * 86400 + 3600)
        self.assertEqual(r.running, {})
        r.step(mon + 21 * 86400 + 60)                        # two slots missed: runs once, now
        self.assertIn("wk", r.running)
        self.settle(r, mon + 21 * 86400 + 60)
        self.assertEqual(self.job("wk")["cron_like_interval"], "mon@06:00")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ran").fetchone()[0], 3)

    def test_due_jobs_run_and_wait_their_interval(self):
        r = self.runner([("ok", "1h", "ok.py --x 1", 60)])
        r.step(1000.0)
        self.assertIn("ok", r.running)
        self.settle(r, 1000.0)
        j = self.job("ok")
        self.assertEqual((j["last_run"], j["last_exit"], j["last_error"]), (1000, 0, None))
        self.assertEqual(self.conn.execute("SELECT args FROM ran").fetchall(), [("--x 1",)])
        with open(os.path.join(self.dir, "jobs", "ok.log")) as f:
            self.assertIn("all good", f.read())
        r.step(1000.0 + 3599)
        self.assertEqual(r.running, {})                      # not due yet
        r.step(1000.0 + 3600)
        self.assertIn("ok", r.running)                       # due again
        self.settle(r, 1000.0 + 3600)
        self.assertEqual(self.errors(), [])

    def test_failure_goes_to_poll_errors_and_waits_an_interval(self):
        r = self.runner([("bad", "1h", "fail.py", 60), ("ok", "1h", "ok.py", 60)])
        r.step(1000.0)
        self.settle(r, 1000.0)
        errs = self.errors()
        self.assertEqual([s for s, _ in errs], ["job-bad"])
        self.assertIn("exit 1", errs[0][1])
        self.assertIn("boom in the job", errs[0][1])
        self.assertEqual(self.job("bad")["last_exit"], 1)
        self.assertEqual(self.job("ok")["last_exit"], 0)     # the other job is unaffected
        r.step(1000.0 + 60)
        self.assertEqual(r.running, {})                      # no retry storm

    def test_timeout_kills(self):
        r = self.runner([("slow", "1h", "slow.py", 5)])
        r.step(1000.0)
        r.step(1004.0)
        self.assertIn("slow", r.running)
        r.step(1006.0)
        self.assertEqual(r.running, {})
        self.assertEqual(self.job("slow")["last_exit"], -9)
        self.assertIn("timed out", self.errors()[0][1])

    def test_running_job_is_not_started_twice(self):
        r = self.runner([("slow", "1m", "slow.py", 3600)])
        r.step(1000.0)
        proc = r.running["slow"][0]
        r.step(1000.0 + 600)                                 # due again, still running
        self.assertIs(r.running["slow"][0], proc)

    def test_missing_script_and_start_failure(self):
        r = self.runner([("ghost", "1h", "nope.py", 60)])
        r.step(1000.0)
        self.settle(r, 1000.0)
        self.assertEqual(self.errors()[0][0], "job-ghost")  # python exits 2
        r.specs["ghost"]["command"] = "ok.py"
        r.app_dir = os.path.join(self.dir, "missing-dir")    # Popen raises
        r.step(1000.0 + 3600)
        self.assertEqual(r.running, {})
        self.assertIn("could not start", self.errors()[-1][1])

    def test_disabled_job_is_skipped_and_survives_restart(self):
        specs = [("ok", "1h", "ok.py", 60)]
        r = self.runner(specs)
        self.conn.execute("UPDATE jobs SET enabled=0 WHERE name='ok'")
        self.conn.commit()
        r2 = self.runner([("ok", "2h", "ok.py --new", 60)])  # restart with a changed spec
        r2.step(1000.0)
        self.assertEqual(r2.running, {})
        j = self.job("ok")
        self.assertEqual((j["enabled"], j["cron_like_interval"], j["command"]),
                         (0, "2h", "ok.py --new"))

    def test_step_never_raises(self):
        r = self.runner([("ok", "1h", "ok.py", 60)])
        self.conn.execute("DROP TABLE jobs")
        self.conn.execute("DROP TABLE poll_errors")          # cannot even record the error
        r.step(1000.0)                                       # logs, does not raise
        self.assertEqual(r.running, {})


if __name__ == "__main__":
    unittest.main()
