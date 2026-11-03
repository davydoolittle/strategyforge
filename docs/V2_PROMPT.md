# StrategyForge v2 — Development Prompt

> Hand this file to a coding agent working in `/mnt/user/appdata/strategyforge` on Matrix
> (mounted at `/work` inside the `claude-dev` container; see §10.9 and `docs/V2_INSTALL.md`).
> It replaces the "APEX" master prompt. Everything APEX wanted that survives contact
> with a 16 GB box and 2 years of hourly data is in here; everything that doesn't is in
> §9 "Not in scope", with the reason.

---

## 0. Your role

You are extending an **existing, working** quant research pipeline, not starting one.
Read §1 before writing a line of code. Every module you add must plug into the tables
and conventions already there. Pure Python, stdlib first; add a dependency only when
it replaces more than ~200 lines or does something stdlib cannot (Optuna is the one
pre-approved addition). No React, no Postgres, no Redis, no Celery. One container,
one SQLite file, one Dockerfile.

The system's purpose is to tell its owner the truth about whether a trading strategy
has edge. Its most important output is the word **REJECTED**.

---

## 1. What already exists (ground truth as of 2026-10-19)

### Containers on Matrix (Unraid, i7, 16 GB)
| container | role | port | notes |
|---|---|---|---|
| `strategyforge` | data collector + research CLI | — | `python -u main.py` loop; DB at `/data/forge.db` |
| `davesbridge` | TradingView webhook receiver | 8090 | FastAPI; `DRY_RUN=true`; dedup by sha256(payload+minute); logs to `/data/signals.db`; ntfy |

Public ingress: `davesbridge.xyz` → Cloudflare Tunnel → Matrix:8090.
Alerts: ntfy topic `sforge-alert-8js5ck`.
Outbound SMTP is blocked on Matrix; anything that emails must use an HTTPS API.

### Modules in `strategyforge/app/`
| file | lines | what it does |
|---|---|---|
| `main.py` | 290 | Collector loop. Hourly Coinbase OHLCV for BTC/ETH/SOL (730-day backfill → `candles`). Every 15 min: Kraken Futures funding/predicted funding/OI/perp spread/mark, Deribit DVOL (BTC, ETH), Coinbase order-book imbalance ±1%, spot spread. Hourly: Fear & Greed, BTC dominance (→ `context`). Errors → `poll_errors`. |
| `engine.py` | 465 | SMRT Trap-Aware V4.1 as a parameterised backtester. Validated 73/87 exact trade matches vs TradingView. Signal on bar close, fill at next open, ATR stop + R:R target or ATR trail, risk-% sizing, reverse on opposite. 1h native, 4h by aggregation. `backtest(conn, pair, cfg, start_iso, end_iso, tf_seconds)` → dict with stats + trade list. **New in v2:** `cfg["fill_delay_bars"]` (default 0) for the look-ahead test. |
| `optimizer.py` | 225 | Random search over 13 numeric + 8 toggle params. 70/30 IS/OOS split. IS gate (≥20 trades, PF ≥ 1.05) then OOS gate (≥8 trades, PF ≥ 1.2, net > 0). Every trial → `candidates`. Top 3 survivors per pair get shadow-feature annotation. |
| `features.py` | 300 | ~31 shadow features per trade at the signal bar: OHLCV-derived (ATR%, vol regime percentile, trend age, BB width, realized vol, vol-of-vol, autocorr, session, ETH/BTC RS…) and context-joined (funding z-score, OI Δ24h, DVOL, IV–RV spread, OB imbalance, F&G, dominance). → `trade_features`. |
| `analyze.py` | 214 | Three honesty checks: survivor parameter clustering, feature AUC winners-vs-losers (with the 31-feature multiple-comparison warning), cross-pair transfer. |
| `validation.py` | **new** | Per-trade Sharpe/Sortino, skew/kurtosis, PSR, **deflated Sharpe** (Bailey & López de Prado), **embargoed walk-forward** folds, Monte Carlo (shuffle for drawdown, bootstrap for outcome), parameter-plateau sensitivity, cost stress, look-ahead delay test, cached trial-SR variance. Stdlib only. |
| `adversary.py` | **new** | Runs 8 attacks on a candidate, writes `adversarial_reports`, prints verdict REJECTED / FRAGILE / SURVIVED. A1 A2 A6 are hard fails. ~8–20 s per candidate. |
| `lineage.py` | **new** | `strategies` + `promotions` tables. DNA = sha256 of trading-logic params (costs/capital excluded). Ladder `hypothesis → backtest_winner → oos_validated → paper_validated → approved`, one rung at a time, evidence required, `oos_validated` only on a SURVIVED report with matching DNA, `approved` only with `--i-am-a-human`. |
| `pine_export.py` | **new** | Candidate or strategy → Pine Script v6 `strategy()` with all params as inputs, non-repainting HTF via `request.security(..., close[1], lookahead_on)`, alert() JSON for davesbridge. |
| `tests/test_validation.py` | **new** | 22 unit tests, no DB needed. `python -m unittest discover -s tests`. |

### Tables in `/data/forge.db` (counts as of today)
| table | rows | key columns |
|---|---|---|
| `candles` | 55,974 | (pair, ts) PK; 1h bars from 2024-09-02; 3 pairs × 18,658 |
| `context` | 107,170 | (ts, metric, pair); since 2026-09-01 |
| `candidates` | 2,700 | 1,800 @1h (86 passed) + 900 @4h (11 passed); params_json, IS/OOS stats |
| `trade_features` | 1,505 | candidate_id, signal_ts, pnl, features_json |
| `backtests` | 2 | engine CLI runs |
| `poll_errors` | 30 | |
| `sr_trials` | new | cached per-trial IS Sharpe for DSR variance |
| `adversarial_reports` | new | candidate_id, verdict, report_json |
| `strategies`, `promotions` | new | lineage |

### What the data says (findings from running the new adversary on 2026-10-19)
These are facts the next phases must be designed around, not opinions:

1. **Every top survivor is REJECTED on sample size and deflated Sharpe.** Top-10 at 1h and top-5 at 4h, all 15. The best candidate (#1033) is structurally excellent — 100% parameter plateau, transfers to ETH and SOL, survives 3× costs, decays correctly when delayed — and still cannot be distinguished from the best of 600 random draws, because it has 31 trades. Per-trade Sharpe 0.21 vs a 600-trial noise ceiling of ~1.0.
2. **The 1,800 1h trials are 600 unique parameter sets × 3 pairs.** `optimizer.py` uses the same seed for every pair, so trial *k* is identical across pairs. The DNA hash caught this on the first registration. Cross-pair "transfer" is therefore partly the same draw scored three times.
3. **Stored OOS stats drift.** #1033's OOS PF was 2.86 when stored and reads 1.73 now, because the collector added six weeks of candles and the 70/30 split point moved. `candidates` does not record its windows.
4. **Candidate #590 improves when its fills are delayed one bar** (+2,942 → +3,368). That is the signature of noise or leakage. It is in the top 10 by OOS PF.
5. **The trial-SR variance is dominated by degenerate trials.** With all trials included, var = 54.8; excluding trials with < 10 trades, var = 0.105. `validation.MIN_TRADES_FOR_SR_VARIANCE = 10` is the documented choice.
6. Mean per-trade Sharpe across all trials is **−0.25**. Most of the parameter space loses money after costs.

**Conclusion that drives the roadmap:** the bottleneck is not optimisation technique; it is *trades per unit of evidence*. More optimizer trials make validation harder (they raise the DSR bar). The only ways forward are more independent observations (more pairs, more history, finer timeframe) and fewer, hypothesis-driven trials.

---

## 2. Non-negotiable principles (unchanged from APEX, now enforced in code)

- **No future-data leakage.** Every signal uses only bars with `ts < fill bar`. HTF data is the previous *completed* HTF bar. `adversary.py` A6 tests this; keep it passing.
- **Deflated, not raw.** No Sharpe, PF or win-rate is reported without the trial count that produced it. `A2` is a hard fail. Never loosen `MIN_DSR` without a written reason in the commit.
- **Promotion is evidence-gated and one rung at a time.** `lineage.promote()` is the only path. Do not add a bypass. Do not let any scheduled job call it with `--i-am-a-human`.
- **The optimizer records its windows.** Every candidate stores `is_start, split, oos_end` so results are reproducible after the collector adds data.
- **Simplicity wins ties.** When two candidates survive, the one with fewer active toggles ranks higher.
- **Human-controlled deployment.** Nothing in this repo places orders. `davesbridge` stays `DRY_RUN=true` until a human flips it, and v2 does not touch that flag.
- **No fabricated capability.** TradingView has no API for pushing scripts. `pine_export.py` writes a file; the human pastes it. Say so in every doc that mentions export.

---

## 3. Hardware budget

Matrix: Intel i7, 16 GB, Unraid, shared with ~15 other containers.

- StrategyForge gets **≤ 2 CPU cores and ≤ 2 GB RAM** (`deploy.resources.limits` in compose). One engine backtest is 0.14 s single-threaded; an adversarial battery is ~100 backtests. Budget accordingly: a nightly job may run ≤ 4,000 backtests (~10 min).
- SQLite stays. At current growth (3 pairs × 24 bars/day) `candles` adds 26k rows/year. Adding 10 pairs at 15 m adds ~350k rows/year. Still trivial. Enable WAL mode on first connect in v2.
- No local LLM. A 7B model at Q4 wants 5–6 GB and reasons poorly about statistics. If a research assistant is built (Phase 8), it calls the Claude API with read-only tools over the DB; cost is cents per question.

---

## 4. Phase 1 — Validation layer  ✅ DONE 2026-10-19

Delivered and tested in this repo: `validation.py`, `adversary.py`, `lineage.py`, `pine_export.py`, `tests/`. Remaining Phase 1 item for the agent:

- [x] **P1.1** *(done 2026-10-25: #1033 BTC-USD 1h, 30/30 trades matched, see `docs/PINE_PARITY.md`)* Paste `exports/SF_c1033_BTC-USD_1h.pine` into TradingView on COINBASE:BTCUSD 1h and compare its List of Trades with `python engine.py --pair BTC-USD --start 2025-01-01 --end 2026-10-01`. Record the match rate in `docs/PINE_PARITY.md`. If below 70/87-class agreement, fix the Pine, not the engine.

---

## 5. Phase 2 — Optimizer v2 and more evidence  (build this next)

Goal: make it *possible* for something to survive the adversary, by changing what is searched and how much data it is searched on. Definition of done: at least one candidate reaches `oos_validated` **or** a written finding that the V4.1 family has no validatable edge at 1h/4h on these pairs.

- [x] **P2.1 Window pinning.** *(done 2026-10-25: `app/windows.py`; backfill reproduces stored IS+OOS stats for 97/97 passed candidates)* Add `is_start, split_ts, oos_end` columns to `candidates`; populate on insert; `adversary.py` and `analyze.py` read them instead of recomputing from `MAX(ts)`. Backfill the 2,700 existing rows from `run_at` + the candle range at that time (approximate; mark `windows_backfilled=1`).
- [x] **P2.2 Seed per pair.** *(done 2026-10-25: index = position in `main.PAIRS`, append-only; `candidates.base_seed` + `dna`; existing 2,700 rows = 900 DNAs × 3 pairs, `base_seed` NULL)* `seed_for_pair = base_seed * 1000 + index_of(pair)`. Store `base_seed`. Log the DNA of every trial so duplicate parameter sets across runs are visible.
- [x] **P2.3 Store per-trial Sharpe.** *(done 2026-10-25: all 2,700 rows backfilled on pinned windows, 0 unreproducible; DSR variance now uses every trial; `sr_trials` no longer read or written — table left in the DB, drop when convenient)* `is_sharpe, oos_sharpe, is_skew, is_kurt` in `candidates`. Retire `sr_trials` once backfilled.
- [ ] **P2.4 More data, in this order, each one measured by "trades per survivor":**
  1. [x] *(done 2026-10-25: backward backfill in `update_candles`; history now starts 2021-05-05 on BTC and ETH)* Extend history: Coinbase has BTC-USD hourly back to 2015. Backfill to **2,000 days** (`BACKFILL_DAYS=2000`). This alone roughly triples trade counts.
  2. Add pairs from Coinbase with ≥ $50 M daily volume and ≥ 2 years of history: start with `AVAX-USD, LINK-USD, DOGE-USD, ADA-USD, DOT-USD, LTC-USD`. Make `PAIRS` the only place this list lives. *(2026-10-25: none of those six clears $50 M/day on Coinbase (30-day avg $2.5–11 M). Of 403 USD pairs only BTC, ETH, XRP, SOL and ZEC do; ZEC only for the last ~5 months. Added **XRP-USD** only. `PAIRS` in `main.py` is now the single, append-only source: env var ignored, optimizer default derived from it.)*
  3. **Only then** consider 15 m candles (`granularity=900`). It multiplies rows 4× and the engine's `WARMUP_BARS`, `htf_seconds`, and `vwap_daily` all assume 1h; audit each before enabling.
- [x] **P2.5 Ablation, not random search.** *(done 2026-10-26: `app/ablation.py`, 8 toggles + 6 traps (`min_rel_vol, max_compression, max_dist_ema, max_big_bars, min_ema_sep, adx_threshold`), verdicts earns/hurts/decoration/inactive on pinned IS and OOS, `--register` stores the pruned cfg with `ablation_of`; results in `docs/FINDINGS.md` F2)* Add `ablation.py`: take one parameter set, run it with each of the 8 toggles flipped one at a time and each of the 6 "trap" thresholds removed; report which filters *earn their place* (PF or DSR drops when removed) and which are decoration. This is 14 backtests per candidate, not 600, so the DSR bar stays low. Feed survivors of ablation into the adversary.
- [ ] **P2.6 Optuna (optional).** Replace `sample_cfg(rng)` with an Optuna TPE study, ≤ 200 trials per pair, objective = **OOS deflated Sharpe**, with `n_trials` passed into the DSR so the objective deflates itself as the study grows. Store `study_name` on each candidate. Keep the random sampler as `--sampler random` for comparison.
- [ ] **P2.7 Ranking.** `rank.py`: score = DSR × walk-forward consistency × plateau score × transfer fraction, minus 0.02 per active toggle. Print top 20 with every component. Replace the "LEADERBOARD by OOS PF" block in `optimizer.py` with a call to it.
- [ ] **P2.8 Tests.** Window pinning round-trip; per-pair seed uniqueness; ablation on a known cfg flags `use_trailing` (extinct among survivors) as decoration.

---

## 6. Phase 3 — Shadow features → candidate filters, and event fingerprinting

Goal: turn `trade_features` from a report into a pipeline. Definition of done: a feature can be proposed, tested, promoted to a filter, and that filter's candidate goes through the adversary — with no step requiring a human to edit `engine.py`.

- [x] **P3.1 Filter hooks.** *(done 2026-10-26: `features.check_filters` + lazy `FeatureComputer` in `engine.backtest`, only consulted when a signal would fire; a None feature blocks, an unknown name raises; `app/filters.py --candidate N --filter "vol_of_vol>=12" [--register]` stores the child with `filter_of` and a lineage child; `pine_export` refuses filtered cfgs)* Add `cfg["filters"]: list of {"feature": name, "op": ">="|"<=", "value": x}` to the engine. `bar_signals()` evaluates them via a `FeatureComputer` lazily built per backtest. A candidate with a filter is a child in `lineage` of the candidate without it (`parent_id` set, `note="filter: vol_of_vol >= 12"`).
- [x] **P3.2 Feature promotion rule.** *(done 2026-10-26: `app/feature_evals.py`, table `feature_evals`; trades deduplicated on (pair, tf, signal bar, direction); holdout = signals after the gate sample's latest signal, >= 50 of them, decided once; `--annotate-new` extends annotated candidates to the newest candle. vol_of_vol: AUC 0.576 on 1,871 distinct signals (0.598 on 3,111 raw rows) — below the gate, not a candidate. FINDINGS F5)* A shadow feature becomes a *candidate filter* only when: AUC ≥ 0.60 or ≤ 0.40 on ≥ 200 annotated trades **and** the same direction holds on trades that did not exist when the AUC was first measured (store `measured_at` per feature in a new `feature_evals` table). `vol_of_vol` (AUC 0.611 today) is the first test case.
- [x] **P3.3 Event fingerprinting.** *(done 2026-10-26: `app/fingerprint.py`, table `fingerprints`; moves and non-moves labelled on one 24-bar grid; features at the window start + context deltas 24h/7d/30d; Table A = OHLCV features 2021-, Table B = context era. At K = 3 hourly ATRs 30-33% of windows are 'moves', so `--k` is exposed and the report warns; context era is ~8 weeks, not ~1 month. FINDINGS F6)* `fingerprint.py`: define a "move" as a close-to-close change ≥ 3 × ATR over 24 bars. For every move, collect the context vector 24 h / 7 d / 30 d before. For every *non*-move window of the same length, collect the same. Report per-feature AUC move-vs-non-move with the multiple-comparison warning. Nothing here affects trading; it is a research table (`fingerprints`) and a report. Note that `context` only begins 2026-09-01, so this will have ~1 month of usable data; say so in the output instead of pretending.
- [x] **P3.4 Regime tag.** *(done 2026-10-26: `features.RegimeTagger` on DAILY bars — last completed day's 50-EMA 5-day slope, ADX(14) < 20 = chop — because at 1h signal time V4.1's own entry rule forces ADX above threshold, so a 1h tag is never chop and repeats the trade direction; `trade_features.regime` column, `python features.py --backfill-regime`; A9 fails if the trades outside the most profitable regime net <= 0. FINDINGS F7)* Add `regime` to `trade_features`: `trend_up / trend_down / chop` from the slow EMA slope and ADX at signal time. `adversary.py` gains attack **A9 regime** (soft): fails if all profit comes from one regime.

---

## 7. Phase 4 — Paper-trading loop and decay monitor

Goal: close the loop between TradingView alerts and the engine, so `paper_validated` can actually be earned. Definition of done: a strategy registered in lineage receives live alerts via davesbridge, the engine's expected signals are reconciled against them, and a rolling health score is written daily.

- [x] **P4.1 Signal reconciliation.** *(done 2026-10-26: `app/paper.py`; `engine.backtest` now returns `entries` = every entry decision, the bar where the Pine export calls `strategy.entry()`/`alert()`; alert bar = the bar that closed before `received_at`; `paper_trades` adds `signal_id` (idempotence), `bar_ts`, `symbol`, `tf`, `note`; report also lists engine entries with no alert. `sf.sh` mounts davesbridge's data dir read-only at `/bridge`. signals.db holds one manual test alert and no strategy alerts: nothing is deployed yet)* `davesbridge` payloads now carry `"strategy"` (set by `pine_export.py`). New `paper.py` in strategyforge reads `/data/signals.db` (mount it read-only), matches each alert to the strategy by name, and checks: did the engine, run on the same candles up to that bar, also fire? Store `paper_trades (strategy_id, alert_ts, engine_fired, side, price_alert, price_engine)`.
- [x] **P4.2 Paper P&L.** *(done 2026-10-26: in `app/paper.py`, run after reconciliation. The paper position is the one TradingView holds: the strategy's own-symbol/tf alerts, agreed or not, drive `engine.backtest(signals=...)` (new kwarg replacing the entry signals; fills, sizing, stop/target/trail, flips, fees unchanged; `open_position` / `pending_entry` now returned). Rebuilt from scratch every run. `paper_positions` = one row per alert, closed / open (marked to newest hourly close, unrealised net of fees+slip) / pending / ignored (already on that side). `paper_summary` adds `rolling_30d_trades`, `rolling_30d_net`, `cum_net`; trades count on exit day; pf NULL = no losing trade. No live alerts yet)* Mark-to-market open paper positions hourly from `candles`; close them by the engine's own stop/target logic. `paper_summary (strategy_id, day, n_trades, net, pf, rolling_30d_pf)`.
- [x] **P4.3 Promotion evidence.** *(done 2026-10-26: `paper.evidence()`, called by `lineage promote`; `paper.py --evidence N` prints the checks read-only. Evidence must name the strategy being promoted. Agreement = engine-agreed / (reconciled alerts + engine entries in the alert span that never got an alert). Span = first to last reconciled alert bar. Rolling PF = 30-day PF of the alert-driven paper P&L, re-simulated at promote time; zero closed trades in 30 days fails, wins-only passes. The promotions row stores the measured numbers)* `lineage promote --to paper_validated --evidence paper:<strategy_id>` is accepted only if `paper_trades` for that strategy has ≥ 30 reconciled trades over ≥ 60 days with engine/alert agreement ≥ 90 % and rolling PF ≥ 1.2.
- [x] **P4.4 Decay monitor.** *(done 2026-10-26: `app/health.py`, CLI for now; P4.5 schedules it daily. Baseline = per-trade returns (pnl / equity before, %) of the engine on the pinned OOS window (split_ts..oos_end); >= 10 trades or no baseline. Forward = alert-driven paper trades when the strategy has alerts, else the engine's own trades with signal >= oos_end (`source` column). "CUSUM breaches -3 sigma" implemented as a lower tabular CUSUM of z = (r - mu)/sigma with slack K = 0.5, alarm at S <= -3, recomputed over the whole forward path each run so a breach that recovers between runs is kept. Simulated: no decay false-alarms after ~117 trades (normal) to ~390 (40%-win 2.4R mix); a decay to zero edge takes ~105 trades on average. ntfy pushes once per strategy (retried until delivered; no topic is printed, not silently skipped). `health` table adds source, n_baseline, mu, sigma, n_forward, min_cusum, breach_ts, notified, detail. 2026-10-31: strategies with `forward_test = 1` are watched too, at any status short of retired; the marker promotes nothing)*  `health.py`, run daily by the collector loop: for each strategy at `oos_validated` or above, compute a CUSUM of per-trade returns against its validation-window mean. Alert via ntfy when the CUSUM breaches −3 σ. Write `health (strategy_id, day, cusum, alert)`.
- [x] **P4.5 Scheduler.** *(done 2026-10-26, needs a container rebuild + recreate: `main.JobRunner`, called once per 30 s collector pass after the first candle top-up. `JOB_SPECS` in main.py is the source of truth: paper 1h, health 1d, `feature_evals --annotate-new` 1d, `adversary --top 20` 7d. The `jobs` table mirrors it (`cron_like_interval` = 15m/1h/1d/7d) and keeps `last_run` (set at start, so a failing job retries after one interval), last_exit, last_duration_s, last_error and an `enabled` switch that survives restarts. Each job is a child process (`python -u <script>`), so the loop never waits on it and a crash cannot stop it. One process per job; different jobs may overlap. Non-zero exit, a timeout kill (paper 30 min, health 1 h, feature_evals 4 h, adversary 12 h) or a failure to start -> `poll_errors` source `job-<name>` with the log tail. Latest output in `/data/jobs/<name>.log`. Hardening for the concurrent writers: the collector connects with a 60 s busy timeout and wraps each pair's candle top-up in record_error (it used to be able to crash the loop); paper.update_pnl, health.run and features.annotate_trades now compute before they write, so no job holds the write lock across a backtest. The collector container needs `-v /mnt/user/appdata/davesbridge/data:/bridge:ro` or the paper job fails hourly into poll_errors; health reads the container's NTFY_TOPIC)*  Extend `main.py`'s loop with a `JOBS` table of `(name, cron_like_interval, last_run)` and run `health`, `paper`, and a weekly `adversary --top 20` from it. No external scheduler.

---

## 8. Phase 5 — Pluggable engine and a second family

Goal: stop every module assuming V4.1. Definition of done: a second strategy
family runs through optimizer, adversary, ablation, lineage, paper, health and
Pine export with no V4.1-specific code outside its own file, and every stored
V4.1 result still reproduces to the cent. Rules in §10 apply: bulk writes to
forge.db commit in batches and hold the write lock briefly (§10.9).

- [x] **P5.1 Regression harness first.** *(done 2026-10-28: 127/127 passed candidates to the cent, #3214 = 112 IS + 50 OOS trades identical field for field against `tests/fixtures/c3214_trades.json` (written from the pre-split engine), plus every stored DNA equals `dna_of(params)` (3,900/3,900). Copies forge.db with the backup API into `SF_SCRATCH_DIR` or the temp dir; skips loudly if no forge.db exists. ~5.5 min, so the full suite now takes ~6 min)* `tests/test_regression.py`: against a scratch copy of forge.db, every candidate with `passed=1` must reproduce its stored `is_trades, is_net, oos_trades, oos_net` to the cent on its pinned windows, and strategy #1 (candidate 3214) must reproduce its exact trade list. This test must pass before and after every later step.
- [x] **P5.2 Split engine.py into execution and strategy.** *(done 2026-10-28: indicator maths in `app/indicators.py` (re-exported by engine); `app/strategies/` = `base.Strategy` interface + registry, `smrt_v41.py` holds V4.1's signals, sampler, ablation/sensitivity/analyze lists and Pine template verbatim. A cfg names its family in `cfg["family"]`; no key = V4.1, so stored params and DNAs are unchanged (DNA hashes the family only when it is not the default). `candidates.family`, `strategies.family`, `effective_trials.family` added by `ADD COLUMN ... DEFAULT 'smrt_v41'` (no row writes). engine looks the family up first, passes it `bars` + `from_i` and keeps execution; `use_trailing`/`trail_atr` are optional. A2's N and SR variance, N_eff, analyze section 1 and `adversary --top --family` are per family; the "all trials" DSR stays global, information only. paper/health check the strategies.family column against the cfg. Pine output byte-identical to the pre-split render for #3214, #2716, #1033 and the default cfg. P5.1 still passes, plus a new check that every seeded optimizer row (1,200) regenerates its exact params from its family's sampler)* Execution (fills at next open, slippage, commission, risk sizing, ATR stop / R:R target / trailing, reverse-on-opposite, `fill_delay_bars`, `history_start_ts`, forced `signals=`, filters hook, `entries` / `open_position` / `pending_entry` outputs) stays in `engine.py`. The V4.1 signal logic moves to `app/strategies/smrt_v41.py` as a class with: `name`, `default_cfg()`, `sample_cfg(rng)`, `dna_keys`, numeric/toggle param lists for ablation and sensitivity, `signals(bars, cfg)` -> per-bar (long, short), and `pine_template()`. `app/strategies/__init__.py` is a registry keyed by family name. `candidates` and `strategies` get a `family` column (default `'smrt_v41'`, backfilled); DNA includes family. optimizer, adversary, ablation, pine_export, lineage, paper, health dispatch by family. P5.1 must still pass.
- [x] **P5.3 Second family.** *(done 2026-10-28, run pending on Matrix: `app/strategies/core.py`. Long = EMA fast > mid > slow, ADX(14) > `adx_threshold`, volume / SMA(`vol_ma_len`) >= `min_rel_vol`, |close − EMA fast| <= `max_dist_ema` %, close > previous 20-bar high and > open and > previous high; short mirrored; fixed ATR(14) stop `stop_atr` and target `rr_ratio`. 10 searched parameters (3 EMAs, adx_threshold, vol_ma_len, min_rel_vol, max_dist_ema, stop_atr, rr_ratio, use_shorts); use_longs on, ADX 14, Donchian 20, 1% risk fixed. Dropped beyond the spec list: V4.1's RSI, 3-bar momentum, EMA separation/slope gates, HTF and its hardcoded ADX 22/26/28/30 extras; longs and shorts are symmetric. cfg carries `"family": "core"`. Ablation: toggle use_shorts, traps min_rel_vol / max_dist_ema / adx_threshold. Pine template = V4.1's minus the removed parts. The optimizer now sets each cfg's `tick_size` from `main.TICK_SIZE` (the F4 one-liner; first family run since F4, no stored row changes). Dry run on scratch, 5 trials/pair: ~3.7 s/trial over SMB; ablation, adversary (A2 = "E[max SR | 5 core trials]"), Pine export all ran on a core candidate. Command for Matrix: `sf.sh optimizer.py --family core --tf 1h --trials 300 --seed 5301`, after rebuilding the collector image)* `app/strategies/core.py` = the F2 earned-its-place subset: EMA stack (fast/mid/slow), ADX threshold, volume filter (`vol_ma_len`, `min_rel_vol`), late-entry distance trap (`max_dist_ema`), breakout confirmation, fixed ATR stop and R:R, longs and shorts toggles. No VWAP, no chop filter, no big-bar trap, no trailing. Aim for <= 10 parameters. Its own `sample_cfg` ranges. Pine template derived from the V4.1 one with the removed parts gone. Tests. Then hand over the `sf.sh` optimizer command for 300 trials per pair at 1h with a new base seed, with the note that A2's trial count for this family is its own search, not V4.1's 900.
- [x] **P5.4 Regime filter on V4.1, via `filters.py`.** *(done 2026-10-28 on a scratch copy: `regime` is a categorical filter feature (`==`/`!=`, values trend_up/trend_down/chop), evaluated by `FeatureComputer.regime()` with the backtest's own history bounds, i.e. A9's tag. Children #3901 (of #3214) and #3902 (of #2716) on scratch: SR/trade doubles, PSR vs 0 unchanged (0.95), trades fall 3–5×, A1 now fails on both; all four REJECTED. Not a filter. FINDINGS F8)* Register children of #3214 and #2716 with the filter `regime == chop` (daily ADX14 < 20 at signal time, the tag from P3.4), compare parent vs child on the pinned windows against a scratch copy, and run the adversary on both children. Write the result as FINDINGS F8 whatever it shows.

---

## 8a. Phase 6 — Portfolio view and a third family  (added 2026-10-29)

Goal: two questions FINDINGS F9 leaves open. (1) Does a candidate that fails on its home pair become validatable as a book across all pairs, i.e. does more data per hypothesis rescue it? (2) Does a family built from a measured pattern (F6: big moves follow low volatility, and start near the 30-day extreme in their direction), not from V4.1, have a higher per-trade SR? Definition of done: F10 and F11 written whatever they show. Rules in §10 apply; §10.9 for the live-DB run in P6.3.

- [x] **P6.1 Portfolio evaluation.** *(done 2026-10-29: `app/portfolio.py`, read-only. Each pair runs standalone through `engine.backtest`; the book re-sizes every trade as its return x the pool's realised equity at entry, because signals, stops and exits never depend on equity, only size does. An exit at the open (reverse) is realised before entries at that open; a stop/target exit comes after them. One pair = its own backtest to the cent. Daily returns spread each trade's P&L pro rata over its days (the `bucket_returns` convention), not mark-to-market. Book A2 = per-trade SR of the book's full-window trades against the home pair's own-family E[max SR], since the trials' SRs are per-trade; the daily Sharpe gets PSR vs 0 only. A trade's return is the same in the book as alone, so the book's per-trade SR is the pooled SR of the pairs. #4027 and #3214 on scratch: both books worse than the home pair alone, both still REJECTED. FINDINGS F10)* `app/portfolio.py --candidate N`: run the candidate's cfg on every pair in `main.PAIRS` with a verified tick (home pair on its pinned windows; other pairs on their windows as of the candidate's pin, as A7 cuts them), with engine execution unchanged and one shared capital pool at `risk_pct` per trade, and build one equity curve. Report per pair and for the book: trades, net, PF, max DD, per-trade Sharpe, annualised daily-return Sharpe, and the mean pairwise correlation of daily returns. A2 for the book: deflate by the trial count of the candidate's own family on its home pair (the selection happened once, there). Run on #4027 and #3214 against a scratch copy. FINDINGS F10: what the book does that the single pair doesn't, and whether any verdict changes. Tests.
- [x] **P6.2 Third family `squeeze`.** *(done 2026-10-29: `app/strategies/squeeze.py`. bb_bw_pctile is recomputed in the family exactly as `features.py` does (same sample std, previous 90 days, bar i excluded; equal on every bar in the tests), rolled with a sorted window and cached per price history because it does not depend on cfg. Taken at the signal (breakout) bar. No signal before 90 days of bars. 30-day range includes bar i, as `dist_30d_high/low`. extreme_bias = minimum position in the 30-day range, (close − low30)/(high30 − low30) for longs, mirrored for shorts; sampled 0.5..0.95, so every draw is at least "nearer the extreme". Ranges: compression_pctile 5–50, breakout_len 10–72 bars, stop_atr and rr_ratio 1.5–3.5, use_shorts 50/50. Ablation: toggle use_shorts; traps compression_pctile → 100, extreme_bias → 0. Pine uses `ta.percentrank(bbBw, 90 days)` and `ta.stdev(..., false)` to match. Regression harness passes)* `app/strategies/squeeze.py`, built from F6, not from V4.1. Long when `bb_bw_pctile` (90-day, as `features.py` computes it) is at or below `compression_pctile` AND close breaks above the highest high of the previous `breakout_len` bars AND price sits nearer the 30-day high than the 30-day low (`extreme_bias` in 0..1); shorts mirrored; fixed ATR stop and R:R target; longs/shorts toggles. Six searched parameters: `compression_pctile, breakout_len, extreme_bias, stop_atr, rr_ratio, use_shorts`. Its own `sample_cfg` ranges, ablation lists, Pine template. Tests, including the regression harness.
- [x] **P6.3 Squeeze search, run from the claude-dev container.** *(done 2026-10-29 on the live DB: candidates #5101–#6300, 6 min; optimizer now skips (seed, trial, tf, family, dna) rows already stored, so an interrupted run resumes by re-running it; one row per transaction as before. 44 passed the OOS gate, 14 with >= 30 OOS trades (5 ETH, 9 SOL; none on BTC or XRP); adversary reports #49–#62, all REJECTED on A2, SR 0.07–0.20 vs E[max] 0.38 on SOL. ETH's and XRP's E[max] (3.49, 7.81) are inflated by one 10-trade all-stops trial each; rechecked at a >= 20-trade variance, no result changes; the variance rule is left as is. FINDINGS F11)* On the live DB, following §10.9: `optimizer.py --family squeeze --tf 1h --trials 300 --seed 6101`, then `adversary.py` on every squeeze survivor with >= 30 OOS trades. FINDINGS F11 with the result, whatever it is.

---

## 8b. Phase 7 — Minimal dashboard  (renumbered from Phase 5 on 2026-10-28 and from Phase 6 on 2026-10-29; only after Phase 4 produces data worth looking at)

- [x] **P7.1** *(done 2026-10-29: `app/web.py`. **Standard-library `http.server`, not FastAPI** (decided with the owner 2026-10-29): the repo has no pip dependencies, the Dockerfile installs nothing, and stdlib runs in plain python:3.12-slim the way `sf.sh` does, with no image to build. Every request opens forge.db with SQLite `mode=ro`; only GET/HEAD, others 405. `/api/candidates` adds each candidate's latest report numbers (DSR, A3 consistency, A4 plateau, A8 p95 DD) and takes `reported=` and `limit=` too; reported candidates sort first. `/api/reports/{id}?equity=1` re-runs the candidate on its pinned windows (as the adversary does) for closed-trade equity and drawdown, cached per candidate since pinned windows make it deterministic. inf/NaN serialise as null. Tests: tests/test_web.py)* `web.py`: FastAPI, read-only, port 8091, LAN only (no Cloudflare route). Endpoints: `/api/strategies`, `/api/candidates?tf=&passed=`, `/api/reports/{id}`, `/api/health`, `/api/paper/{strategy_id}`.
- [x] **P7.2** *(done 2026-10-29: `app/static/index.html`, served by `web.py` at `/` with a CSP (scripts only from the page and cdnjs, fetches only to the same origin). Chart.js 4.4.1. Lineage tree (parent → child, verdict badge, status, promotions); candidate table, sortable on every column, default sort DSR, filters tf / family / OOS gate / adversary-reported; click a reported row → equity (IS blue, OOS purple) + drawdown charts and the nine attacks with their key number; paper vs engine table + latest paper P&L; one CUSUM sparkline per watched strategy. All DB strings are inserted as text nodes. On today's data health and paper are empty (one plumbing-test strategy, nothing promoted), and the page says so)* One static `index.html` (vanilla JS + Chart.js from cdnjs, dark theme): lineage tree with verdict badges, candidate table with sortable DSR / WF / plateau columns, equity + drawdown chart for a selected report, paper vs engine reconciliation table, health sparkline.
- [x] **P7.3** *(done 2026-10-29: every `/api/` request needs `Authorization: Bearer $WEB_TOKEN`, compared in constant time; 401 with `WWW-Authenticate` otherwise. `web.py` refuses to start without a WEB_TOKEN of >= 16 characters. The page at `/` holds no data and is served without a token (a browser cannot send a bearer header on navigation); on the first 401 it shows a token box and keeps the token in that browser's localStorage. Deployment, LAN + Tailscale only, no router forward or Cloudflare route: `docs/V2_INSTALL.md` "Dashboard")* Auth: single bearer token from env. Tailscale is the remote-access path; nothing public.
- [x] **P7.4 Jobs, forward tests, 1d.** *(done 2026-11-02: `/api/jobs` and a Jobs panel: each collector job's schedule, last start, duration, exit, next due (interval after last start, or the next weekly slot) and state (ok / failed / overdue more than 2 h past due / paused / never run), plus the newest crypto candle as a collector heartbeat, flagged after 3 h. Lineage shows a "forward test" badge with the forward-trade count from health.py's latest row. Candidates tf filter gained 1d. Read-only like the rest; needs only `docker restart strategyforge-web`.)*
- [x] **P7.5 TSP, patterns, insider panels.** *(done 2026-11-03: `/api/tsp` (latest report text, recorded IFTs, newest backtest batch on official prices if any, else proxies), `/api/patterns` (the F18 ledger, SUPPORTED > SUGGESTIVE > OPPOSITE > NOT SUPPORTED > TOO FEW, then p), `/api/insider` (clusters and open-market buys >= insider_feed.MIN_BUY_USD, last 90 days, newest 50; sales not shown). Each panel carries its caveat as static text in the page: TSP "analysis only, you place every IFT", patterns "history, not advice", insider "UNTESTED, no prediction". Read-only; `docker restart strategyforge-web`.)*

---

## 8c. Phase 9 — Equities and ETFs  (added 2026-11-01)

Goal: test the systematic-macro / CTA hypothesis on liquid ETFs under a declared trial budget, on data and execution that respect a trading session. Definition of done: F16 written whatever it shows. Rules in §10 apply throughout, the crypto regression harness (P5.1) must pass after every step, and work stops for review after each item.

- [x] **P9.1 Data layer.** *(done 2026-11-01: `app/equities.py`, tests/test_equities.py. Bars are in `candles` with `pair` = symbol and a new `session` column, added by `ADD COLUMN ... DEFAULT 'crypto'` (no crypto row rewritten; hash of the 171,735 crypto rows before a cutoff identical before and after). `ts` is 00:00 UTC of the trading date. `instruments` also records `data_start`, `data_end`, `n_bars`, `feed`, `adjustment`, `fetched_through`. Feed SIP, adjustment=all; a symbol is always re-fetched whole, because a new dividend re-bases the adjusted series. `engine.session_pairs()` scopes A7 and analyze's transfer list to the candidate's own session. Alpaca's history starts 2016-01-04 (10.82 years), so the owner added **Tiingo** (key in `data/tiingo.env`) for depth: bars are now Tiingo's adjusted series for every symbol, 19.67 years (UUP) to 33.75 years (SPY), all ten overlapping from 2007-03-01; `instruments.source` records the vendor and one symbol never mixes vendors. `--compare` on the 2,723 shared dates: same dates, 21 daily returns differ by > 0.5%, nearly all March 2020 closes, where Tiingo matches the index's real moves and Alpaca does not. Tiingo's series is the one to use. Market data is on data.alpaca.markets for paper and live keys alike; the paper endpoint is used only for GET /v2/assets.)* `app/equities.py`: daily, split- and dividend-adjusted bars from Alpaca's market data API (credentials from `/work/data/alpaca.env`, never from the repo; paper endpoint only) for a universe table `instruments` (symbol, asset_class, exchange, tick_size=0.01, session='us_equity', listed_from). Seed with 10 ETFs: SPY QQQ IWM EFA EEM TLT IEF GLD DBC UUP. Store bars in forge.db alongside crypto with a `session` column; candles for existing pairs are untouched. Backfill as far as Alpaca's free data allows and record the start date per symbol; if under 10 years, say so and propose Tiingo's free tier for deeper history (owner supplies a key). Do not scrape Yahoo. Batch commits per §10.9. Tests. Stop and report the history depth per symbol.
- [x] **P9.1b Momentum screen, read-only.** *(done 2026-11-02: `app/screen.py`, tests/test_screen.py. 1/3/6/12 months = 21/63/126/252 trading days of adjusted closes; vol = stdev of 252 daily log returns x sqrt(252); ranked by 12-month return / vol. "Top or bottom 10%" on 1-month return = the round(n x 10%) highest and lowest, at least one each: one each side with 10 ETFs. Fewer than 253 bars: skipped and listed. Rows more than 5 days behind the freshest are marked stale; the screen fetches nothing, ETF bars come from the `patterns` job, pinned to `sun@22:00` (was `7d`, which drifted) so Friday's bars are in before the screen. Output: stdout table and `screen.json` next to forge.db (atomic rename); dashboard panel "Screen" via `/api/screen`, with the caveat in the static page. Job `screen` at `mon@06:00`: `main.JobRunner` gained weekly fixed-time schedules (due once per slot; a missed slot runs at once). Takes effect after a collector rebuild. 2026-11-02 later: the job runs `screen.py --notify` and pushes a short ranking via ntfy (NTFY_TOPIC, default priority), once per data date; the date pushed is kept in `screen_notified.json` next to forge.db so the DB stays read-only; a failed push is retried next run. Also needs the rebuild.)* `app/screen.py`: for every instrument in the equities universe, total return over 1, 3, 6 and 12 months and 12-month return / 12-month realised volatility; rank by the vol-adjusted figure; flag a 1-month return in the top or bottom 10% as "short-term extreme (reversal risk)". Table to stdout, JSON for the dashboard, a "Screen" panel with the note "ranking, not a validated signal". Weekly in the collector JOBS, Monday 06:00 UTC. Tests.
- [x] **P9.1c Screen universe: top 50 US ETFs, re-ranked weekly.** *(done 2026-11-03, owner's choice of basis: `app/etf_universe.py`, tests/test_etf_universe.py. Listed = Tiingo's free supported_tickers.zip (assetType ETF, USD, NYSE/ARCA/MKT/NASDAQ/BATS, a bar in the file's last week) ∩ active tradable Alpaca assets (names). Excluded by name: ETNs, leveraged (2x/3X/Ultra/Bull/Bear/daily target), inverse ("Short" unless Short-Term/Treasury/Duration..., long/short funds kept). Ranked by median close×volume over the last 20 sessions (≥ 15), unadjusted, Alpaca SIP. Most liquid, not largest: not AUM. Every run kept in `etf_universe_runs` / `etf_universe`; the screen uses the latest list and reports who entered and left since the previous one. Members outside the research ten get Alpaca bars (2016 on, adjusted), one vendor per symbol; their `instruments` rows have no spread_bps, so engine.py refuses to backtest them. **The research universe is unchanged:** `equities.py --backfill/--snapshot/--report` and the patterns job now touch only `equities.UNIVERSE`, since a list chosen from funds that survived to today would bias any backtest. `store_bars` writes only rows that changed. `fetch_bars` ends at 21:00 UTC on the last date (a bare date was refused as recent SIP data until ~05:00 UTC). Job `etf_universe` at `sun@23:00`, timeout 1 h; the push now carries the top 15, flagged ETFs below them, and the list changes. Needs a collector rebuild.)*
- [x] **P9.2 Session-aware execution.** *(done 2026-11-01: `engine.instrument()` reads the `instruments` row; none = crypto, the old code path, and crypto never reads the cost columns. `us_equity`: daily bars only, weekend or unfinished bars refused, costs taken from the instrument whatever cfg says, so every caller (A5, A7, portfolio) gets them. Commission 0; slippage = `spread_bps` x `slippage_frac` (default 0.5) as a fraction of price, because adjusted prices make a fixed cent meaningless; spreads are set wide in `equities.SPREAD_BPS` to cover 1993-2026. `cfg["cost_x"]` carries A5's stress. Added beyond the brief: notional capped at `max_leverage` x equity (default 2, Reg-T). Gap-through stop fill at the open was already right in both sessions (`min(stop, open)`), now tested; a target gapped through still fills at the target, slightly understating winners. `windows.tf_seconds` knows 1d; optimizer, adversary and A7's tick gate are P9.3 wiring. Tests: tests/test_sessions.py.)* `engine.py`: a session model: 'crypto' (24/7, current behaviour, default) and 'us_equity' (no fills outside trading days; a stop gapped through at the open fills at the open price, not the stop price; no partial-day bars). Tick size and a cost model per instrument (commission 0, slippage as a fraction of spread; defaults documented). The crypto regression harness must reproduce to the cent. New tests for gap-through-stop and weekend/holiday handling. Stop and report.
- [x] **P9.3 First equities hypothesis under a declared budget.** *(done 2026-11-01: FINDINGS F16, all 8 testable candidates REJECTED on A2. Wiring: `--tf 1d` in optimizer, adversary, analyze; `equities.py --snapshot` writes `bar_snapshots`, and each ETF candidate names its snapshot in `cfg["bars_snapshot"]` (not DNA), so a refresh that re-bases adjusted prices cannot move its results; `optimizer.DECLARED_BUDGETS` refuses more than 100 trials or a second seed; ETF seeds come from `equities.UNIVERSE`; A7 reads ETF ticks from `instruments`; shadow features are not annotated for ETFs, which also keeps the collector's baked `annotate_new` off them. A2 for an ETF counts every core 1d trial (N = 1,000) as the brief says, stricter than crypto's per-pair count; `n_trials_home_pair` records the per-ETF 100.)* The existing `core` family on daily bars across the 10 ETFs (the systematic-macro / CTA test). Budget: 100 trials per ETF, seed 9101, 1d, declared here and not to be exceeded. Pinned IS/OOS windows per symbol by the same rule as crypto. Adversary on every survivor with >= 30 OOS trades, A2 deflated by this family's 1d trial count; A7 transfer across the other ETFs. Write FINDINGS F16 with per-trade SR, ceiling, verdicts, and how trades-per-hypothesis compares to crypto 1h. Stop and summarise.

---

## 8d. Phase 10 — Pre-registered ETF portfolio test  (added 2026-11-01, done 2026-11-01: FAIL, FINDINGS F17)

Goal: the test F16 says is the honest next one: a diversified, volatility-scaled book, scored against buy-and-hold so drift cannot pass for skill. Definition of done: F17 written whatever it shows. This rule is written down before any portfolio backtest exists. It is one hypothesis, N = 1: no search, no parameter changes after the first run. A fix to rule semantics is allowed only with a written reason, as in F12.

- [x] **P10.1 Portfolio evaluator.** *(done 2026-11-01: `app/etf_portfolio.py`, tests/test_etf_portfolio.py. Read-only; PSR is `validation.probabilistic_sharpe`. A sleeve is 1/10 of equity; the rule's cap makes 2x total notional automatic. The first signal is 2008-03-31, the first month-end with a month-end 12 back, so the first fill is 2008-04-01.)* `app/etf_portfolio.py`, read-only on forge.db, reads snapshot `etf-2026-10-30` only. Tests with known answers.
- [x] **P10.2 The test.** Stop and report. Rule, fixed here:
  - Universe: the ten ETFs. Window: 2007-03-01 (all ten trading) to 2026-10-30, trading from 12 months later.
  - Signal, on each month's last trading day: an ETF is held if its 12-month adjusted return is > 0, otherwise that sleeve is in cash. Long or flat, no shorts (F16: shorts lost).
  - Size: each held ETF targets 10% annualised volatility, from 60 trading days of returns, capped at 1/10 of equity x 2 per sleeve; total notional at most 2x equity.
  - Fills: next trading day's open, costs from `instruments`. Cash earns nothing, which is against the rule.
  - Benchmark: equal-weight buy-and-hold of the same ten, rebalanced monthly, same costs.
  - Pass: PSR vs 0 >= 0.95 on monthly returns of book minus benchmark, N = 1 (about annualised excess Sharpe 0.38 over 18.6 years). Report also: CAGR, max drawdown, 2x cost, fills one day later, each sleeve's share of return, and a benchmark volatility-matched to the book.
  - A pass earns a forward test on data after 2026-10-30, not a promotion.

---

## 8e. Noteworthy: the pattern ledger  (added 2026-11-01, done, FINDINGS F18)

- [x] **Ledger.** `app/macro_data.py` (Treasury, CBOE, BLS, NY Fed, Tiingo; each value carries its release date) and `app/patterns.py`, list committed before the first run. The `patterns` job re-tests weekly; `tsp_report.py` prints live patterns with their evidence and label under "Noteworthy". All of this takes effect only after a rebuild. A new pattern needs a commit before it runs, and the failures stay in the ledger.

---

## 8f. Edgar insider feed  (added 2026-11-01)

- [x] **Stage 1, feed and digest.** `app/insider_feed.py` pulls Edgar's read-only `/api/export` into `insider_txns` and `insider_clusters` and sends one daily ntfy digest of clusters and open-market buys of at least $100K, or a warning if Edgar is down or stale. Lines say UNTESTED and predict nothing. The S&P 100 and four crypto-linked tickers are muted in Edgar, so they store data without pushing. Crypto tokens have no insiders; only listed companies link to them.
- [ ] **Stage 2, the event study.** Pre-register a rule on SEC's Form 4 history before testing. Score each stock against the market. The universe must include companies that later shrank or vanished. Only a pass may change the message. *(Folded into P13 below, as its Form 4 family.)*

---

## 8g. Modules and roadmap  (added 2026-11-02)

Roadmap only: nothing here is built until it gets its own section with checklist items. Phase numbers 9 and 10 are used, so the deferred phases start at 11.

### Modules

| module | state | evidence |
|---|---|---|
| **crypto** | Live research. Forward tests #3214 and #4027 running (`forward_test`), no forward trade yet. No further 1h family searches. | F9-F11, F13 |
| **equities** | Phase 9 done: Tiingo adjusted daily bars for 10 ETFs, frozen in snapshot `etf-2026-10-30`; core 1d budget spent, all 8 testable candidates REJECTED. Phase 10's pre-registered ETF trend book FAILED against buy-and-hold. | F16, F17 |
| **tsp** | Analysis and alerts only. `live_strategy` = `trend_filter`. F12/F14: nothing beats 100% C. | F12, F14, F18 |

**TSP: drawdown control stated as a risk preference.** *(done 2026-11-02: `tsp_report.cost_line` prints the cost from the stored backtests; the framing note is `report.framing` in `tsp_config.json`.)* `trend_filter` already is a monthly drawdown-control rule (at most 1 move a month, inside the 2-a-month limit). What is missing is the report saying what it costs. On official prices (F14): CAGR 9.8% vs 11.2% for 100% C, max drawdown 34% vs 55%. The cost is not even over time: it won 2004-09 (10.9 vs 2.0) and trailed by 4.2 and 5.5 points a year in 2010-19 and 2020-. The daily report should present it as "you give up return to avoid deep drawdowns", with those numbers, never as a way to beat C. A different drawdown rule would be a new hypothesis, registered before it runs, max 2 transfers a month.

### Deferred phases

| phase | scope | deferred because |
|---|---|---|
| **P11** Portfolio / regime mode across asset classes | One book over crypto, ETFs and TSP funds, sized by volatility or regime. | Needs a fresh rationale. F16 (no timing skill per ETF) and F17 (the ETF trend book failed against buy-and-hold) remove the case it was meant to build on. Needs its own pre-registration and a benchmark that drift cannot beat by itself. |
| **P12** S&P 100 stocks | Daily bars for the S&P 100 with a **point-in-time** constituent list (members as of each date, including names later dropped, merged or bankrupt). | Survivorship bias: today's list only contains the winners. Needs a constituent-history source first; a vendor will be proposed when the phase starts. Follows P11's verdict, not its success. |
| **P13** EDGAR event study | Measure what each SEC filing type does to the filer's price before any notification claims it means anything. Universe P12's. | Needs P12's point-in-time universe and daily bars. |
| Derivatives (options, futures) | Out of scope. | No engine (no greeks, expiries, margin or roll model) and no evidence of an edge in the underlyings to express. |

### P13 in detail: filing types to study

Data: Edgar Alert's data where it carries the type; otherwise SEC EDGAR's own free submissions and full-index files (declared User-Agent, within SEC's rate limit). Every event is timestamped by EDGAR's **acceptance time**, not the period it covers: a filing accepted after 16:00 ET or on a non-trading day enters at the next open. No pushing of any of these until their study passes.

| family | forms / items | why it might move the price |
|---|---|---|
| Insider trades | Form 4 (open-market buy P, sale S, option exercise, 10b5-1 flag), Form 3, Form 5, Form 144 (proposed sale) | Insiders know the business; clustered buys are the classic claim. Absorbs §8f Stage 2. |
| Ownership and activists | SC 13D and 13D/A (≥5% with intent to influence), SC 13G/13G/A (passive ≥5%), 13F-HR (quarterly holdings, 45 days late) | An activist stake often moves the stock; 13G and 13F mostly do not, which is itself worth measuring. |
| 8-K: results and guidance | Item 2.02 results of operations (earnings), 7.01 Reg FD, 8.01 other events | Earnings surprise and post-announcement drift. |
| 8-K: deals and financing | 1.01 material agreement, 1.02 termination of one, 2.01 acquisition or disposal completed, 2.03 new debt, 2.04 debt accelerated, 3.02 unregistered share sales, 3.03 change to holders' rights | Financing and dilution, deal risk, default triggers. |
| 8-K: distress and accounting | 1.03 bankruptcy, 2.05 exit or restructuring costs, 2.06 impairments, 3.01 delisting notice or listing-rule failure, 4.01 auditor change, 4.02 prior financials can no longer be relied on (restatement) | The largest negative moves; 4.02 and 3.01 are the classic red flags. |
| 8-K: people and control | 5.01 change in control, 5.02 officer or director departure or appointment (CEO and CFO exits separately), 5.03 bylaw or charter change, 5.07 shareholder vote results | Surprise CFO exits are a known negative signal. |
| 8-K: cyber | 1.05 material cybersecurity incident | New item since Dec 2023, few events; may stay too thin to test. |
| Periodic reports | 10-K, 10-Q (going-concern language, material weaknesses), NT 10-K and NT 10-Q (late filing) | A late-filing notice is a known negative signal. |
| Offerings | S-1, S-3, 424B prospectuses, S-8 | New shares mean dilution; shelf takedowns often drop the price on the day. |
| M&A and tender offers | S-4, 425, SC TO-T (third-party tender), SC TO-I (issuer buyback tender), SC 14D9 (target's response), DEFM14A | Target premium, acquirer discount, arbitrage spread. |
| Proxy | DEF 14A, PRE 14A, DEFA14A, DFAN14A (proxy fight material from dissidents) | Proxy fights and contested votes. |
| Exits and structure | Form 25 (delisting), Form 15 (deregistration), Form 10 (spin-off registration) | Delisting and going dark are negative; spin-offs have a documented post-spin drift claim. |
| SEC correspondence | CORRESP and UPLOAD (SEC comment letters, released with a delay) | Comment letters on revenue recognition are a claimed warning sign. |
| Foreign filers | 6-K, 20-F, 40-F | Same events for non-US issuers; kept separate because timing and content differ. |

Rules for P13, written now so they cannot be bent later:

- **Each (filing type × direction × horizon) is one trial.** The list above is roughly 50 types; at three horizons (1, 5 and 20 trading days) that is ~150 trials, and the significance bar is deflated by that count, the same way A2 is. Adding a type later adds to the count; dropping a type that failed does not remove it.
- **Score against the market**, as abnormal return over a beta-adjusted benchmark, with costs. Overlapping events for the same stock in the same window are flagged, not double-counted.
- **Registered before the first run**: the type list, the horizons, the benchmark and the pass rule are committed first; the results, including every type that shows nothing, go into FINDINGS.
- **Notifications only quote measured numbers.** A type that passes may say "historically, N events, mean abnormal return X% over D days, CI ...". A type that has not been tested says UNTESTED. Nothing says "buy" or "sell".

---

## 9. Not in scope (and why)

| APEX section | status | reason |
|---|---|---|
| §7 Sentiment (Reddit, X, StockTwits, Google Trends) | **deferred indefinitely** | X API is paid and restricted; Reddit API is hostile; literature shows weak standalone edge; Fear & Greed is already collected and is the one sentiment input worth having. Revisit only if Phase 3 fingerprinting shows the existing context feeds predict moves — then, and only then, does a richer attention signal deserve the engineering. |
| §8 Insider Form 4 | **not here** | Equities-only; this pipeline trades crypto. Dave's `edgar-alert` project already ingests Form 4. If a crypto-relevant use emerges (e.g. COIN, MSTR as sentiment proxies) consume its table rather than re-scrape. |
| §9 13F / 13D / COT | **not here** | 45-day-stale disclosures are irrelevant at 1h–4h horizons. COT has no crypto coverage that matters. |
| §6 Forex markets | **deferred** | Different data vendor, different session logic, different cost model. ETFs are Phase 9. |
| Cross-sectional mode for equities | **deferred** (P11, §8g) | F16 and F17 are in; needs a fresh rationale and its own registration. The time-series book was Phase 10 and failed. |
| Individual stocks | **deferred** (P12, §8g) | Needs a point-in-time universe to avoid survivorship bias. |
| SEC filing event study (insider trades and all other filing types) | **deferred** (P13, §8g) | Needs P12's stocks. |
| Order routing from davesbridge to Alpaca | **deferred** | Nothing has passed validation. |
| Derivatives (options, futures) | **out of scope** | No engine for greeks, expiries, margin or rolls, and no edge in the underlyings to express. |
| §13 Local LLM research agent | **replaced** | See §3. Phase 8, if it happens, is Claude API + read-only DB tools. |
| §14 React dashboard with 10 sections | **replaced** by §8b | One page, read-only. |
| §2 Postgres / Redis / auth stack | **replaced** | SQLite + bearer token + Tailscale. |
| Live order execution | **never in this repo** | davesbridge `DRY_RUN` is the boundary. |

---

## 10. Working rules for the agent

1. Read `app/engine.py` and `app/validation.py` in full before anything else.
2. Run `python -m unittest discover -s tests` before and after every change. Add a test for every new statistic.
3. Every new table gets created in an `ensure_tables(conn)` in its module, `CREATE TABLE IF NOT EXISTS`. No migrations framework.
4. Every CLI prints what it did and what the number means, in the style of `analyze.py`'s "Read:" paragraphs.
5. When a result is bad (zero survivors, DSR 0.0), the output says so plainly and says what would change it. That is a successful run.
6. Never touch `davesbridge`'s `DRY_RUN` / `KILL_SWITCH` handling.
7. Commit after each checklist item with the item id in the message (`P2.1 window pinning`).
8. If a step needs a credential or a service that isn't there, stop and say exactly what is missing. Do not stub it and move on.
9. **Bulk writes: commit in batches and hold the write lock briefly.** *(Revised 2026-10-29.)* The agent now runs in the `claude-dev` container on Matrix itself (`docker exec -it claude-dev claude`, see `docs/V2_INSTALL.md`). The repo is at `/work`, the live DB is `/work/data/forge.db` on local disk (`FORGE_DB` is set), and davesbridge's `signals.db` is read-only at `/bridge/signals.db`. So bulk jobs (optimizer runs, backfills, re-annotation) can be run directly from the agent's shell; they no longer have to be handed over as `sf.sh` commands. What has not changed: the collector container shares this file and writes to it every hour (candles, context, and the P4.5 jobs), and SQLite allows one writer at a time. Any job that writes thousands of rows must:
   - **compute first, then write**: never hold an open write transaction across a backtest or a network call;
   - **commit in batches** (on the order of 100 rows or a few seconds of work per transaction, whichever is smaller), so the lock is held for well under the collector's 60 s busy timeout (`main.DB_TIMEOUT_S`);
   - be **restartable**: skip rows already written, so a run killed halfway can be resumed rather than redone.

   Test a new bulk job against a scratch copy first (`sqlite3` backup API into `SF_SCRATCH_DIR` or the temp dir), as before. The history behind the old rule: the P2.3 backfill died mid-run with `disk I/O error` on 2026-10-25, because SQLite locking over the SMB share is unreliable. That applies only to the laptop + SMB setup; if you ever work from the share again, the old rule applies there (no bulk writes over the network, hand over `sf.sh` commands, never enable WAL from the share). The DB is in rollback-journal (`delete`) mode; do not switch it to WAL without also checking the collector and `sf.sh` containers.

## 11. Your first response

1. Confirm you have read §1 and list the three findings from §1 that most change how you'd approach Phase 2.
2. Run the test suite and `python adversary.py --candidate 1033`; paste the verdict.
3. Start on **P2.1**.
