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

---

## 9. Not in scope (and why)

| APEX section | status | reason |
|---|---|---|
| §7 Sentiment (Reddit, X, StockTwits, Google Trends) | **deferred indefinitely** | X API is paid and restricted; Reddit API is hostile; literature shows weak standalone edge; Fear & Greed is already collected and is the one sentiment input worth having. Revisit only if Phase 3 fingerprinting shows the existing context feeds predict moves — then, and only then, does a richer attention signal deserve the engineering. |
| §8 Insider Form 4 | **not here** | Equities-only; this pipeline trades crypto. Dave's `edgar-alert` project already ingests Form 4. If a crypto-relevant use emerges (e.g. COIN, MSTR as sentiment proxies) consume its table rather than re-scrape. |
| §9 13F / 13D / COT | **not here** | 45-day-stale disclosures are irrelevant at 1h–4h horizons. COT has no crypto coverage that matters. |
| §6 Equities / ETFs / forex markets | **deferred** | Different data vendor, different session logic, different cost model. Get crypto validated first. |
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
