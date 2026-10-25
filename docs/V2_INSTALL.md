# StrategyForge v2 — installing the validation layer on Matrix

What this adds to `strategyforge`:

- `app/validation.py` — deflated Sharpe, embargoed walk-forward, Monte Carlo, plateau, cost stress, look-ahead test
- `app/adversary.py` — attacks a candidate with 8 tests, writes a verdict
- `app/lineage.py` — strategy family tree and the 5-rung promotion ladder
- `app/pine_export.py` — candidate → Pine Script v6
- `app/engine.py` — one additive change: `cfg["fill_delay_bars"]`, default 0 (existing behaviour unchanged)
- `app/analyze.py`, `app/main.py`, `app/optimizer.py` — one line each: `DB_PATH` can now be overridden with the `FORGE_DB` env var (default unchanged)
- `sf.sh` — runs any CLI from the share against the live DB, no rebuild
- `tests/test_validation.py` — 22 tests
- `docs/V2_PROMPT.md` — the scoped build prompt for the next phases

**No rebuild and no restart of the collector container is needed.** The running
`strategyforge` container was built from the old `app/` and still has it baked in;
that is fine, because the only thing it runs is `main.py`, which did not change in
any way that matters. The new modules are one-shot CLIs and run from the share.

## Steps

All commands run in a terminal on Matrix (SSH as root, or the Unraid web terminal).

1. Confirm the new files landed on the appdata share.

```
ls -la /mnt/user/appdata/strategyforge/ /mnt/user/appdata/strategyforge/app/ /mnt/user/appdata/strategyforge/tests/ /mnt/user/appdata/strategyforge/docs/
```

Success: `sf.sh` in the root; `validation.py`, `adversary.py`, `lineage.py`, `pine_export.py` in `app/`; `test_validation.py` in `tests/`; `V2_PROMPT.md` in `docs/`.
Failure: any file missing → re-copy from the chat attachment to `W:\strategyforge\` on your laptop.

2. Make the runner executable.

```
chmod +x /mnt/user/appdata/strategyforge/sf.sh
```

Success: no output.

3. Run the unit tests (no database needed).

```
/mnt/user/appdata/strategyforge/sf.sh -m unittest discover -s /src/tests
```

Success: `Ran 22 tests` then `OK`. The first run pulls nothing — `python:3.12-slim` is already on Matrix from the original build.
Failure: `Unable to find image` → run `docker pull python:3.12-slim` once, then retry. Any `FAIL` → paste the output into the chat.

4. Attack your best candidate.

```
/mnt/user/appdata/strategyforge/sf.sh adversary.py --candidate 1033
```

Success: a report ending in `VERDICT: REJECTED   hard fails ['A1', 'A2']` (yes — rejected is the correct result; see `docs/V2_PROMPT.md` §1 for why). About 20 s the first time (it caches 150 trial Sharpes in a new `sr_trials` table), ~8 s after.
Failure: `database is locked` → the collector was mid-commit; just run it again.

5. Attack the top ten at once.

```
/mnt/user/appdata/strategyforge/sf.sh adversary.py --top 10 --tf 1h
```

Success: a SUMMARY block listing 10 candidates with verdicts.

6. Register your two lead candidates in the lineage tree.

```
/mnt/user/appdata/strategyforge/sf.sh lineage.py register --candidate 1033 --name smrt-v41-btc-1033
```

```
/mnt/user/appdata/strategyforge/sf.sh lineage.py register --candidate 88 --name smrt-v41-btc-88
```

```
/mnt/user/appdata/strategyforge/sf.sh lineage.py tree
```

Success: two root strategies, each showing `backtest_winner` and `adv#N=REJECTED`.

7. Export a Pine script and run the parity check in TradingView.

```
/mnt/user/appdata/strategyforge/sf.sh pine_export.py --candidate 1033 --out /data/exports
```

Success: `wrote /data/exports/SF_c1033_BTC-USD_1h.pine (162 lines)`.
The file is at `W:\strategyforge\data\exports\SF_c1033_BTC-USD_1h.pine` on your laptop. Open TradingView → Pine Editor → paste → Add to chart (COINBASE:BTCUSD, 1h) → Strategy Tester → List of Trades. Compare against:

```
/mnt/user/appdata/strategyforge/sf.sh engine.py --pair BTC-USD --start 2025-01-01 --end 2026-10-01
```

Success: most entry timestamps and directions line up (the original V4.1 matched 73 of 87). Record the count in `docs/PINE_PARITY.md`.
Failure: if TradingView rejects the script with a compile error, paste the error line into the chat — Pine v6 cannot be compiled outside TradingView, so this is the first real compile.

8. Hand off to Claude Code for Phase 2.

Open Claude Code in `/mnt/user/appdata/strategyforge` and start with:

```
Read docs/V2_PROMPT.md and follow section 11.
```

## When a rebuild IS needed

Only when `app/main.py` changes (Phase 4 adds the scheduler to it). Then, on Matrix:

```
cd /mnt/user/appdata/strategyforge && docker build -t strategyforge:latest .
```

and in the Unraid Docker tab, edit the `strategyforge` container and click **Apply** without changing anything — that recreates the container from the new image. `docker restart` alone does **not** pick up a rebuilt image.

## Undo

Every new module is additive. To roll back, delete the four new `app/*.py` files and `sf.sh`, restore `engine.py` from the tarball you sent (`strategyforge_src.tgz`). The new tables (`sr_trials`, `adversarial_reports`, `strategies`, `promotions`) are harmless if left in place.
