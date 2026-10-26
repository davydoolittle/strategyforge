# StrategyForge findings

Written results, newest first. Each one says what was run, what the numbers
are, and what they do and do not license. Numbers are reproducible from the
pinned windows on each candidate (P2.1).

---

## F4. A4 and A7 tightened: the soft attacks now discriminate

*2026-10-26. Commit "adversary: tighten A4/A7". Re-run on a scratch copy of
forge.db (reports #24 and #25 there).*

**What changed.**

- **A4 plateau.** Each numeric parameter is now nudged by ±10% **and** ±25%,
  giving 48 OOS neighbours instead of 24. A nudge that lands on a value
  already tried is skipped. To pass, ≥ 70% of neighbours must be profitable
  (unchanged), **and** the worst neighbour's OOS PF must be ≥ 1.0 (new).
- **A7 transfer.** A pair counts as holding only with PF ≥ 1.1 on ≥ 15 OOS
  trades; before, net > 0 on ≥ 5 trades was enough. Each pair is now run at
  its own tick size from the new `main.TICK_SIZE` table, which was checked
  against Coinbase `GET /products/{pair}` `quote_increment` on 2026-10-26:
  BTC/ETH/SOL 0.01, XRP 0.0001. Pairs not in the table are excluded and
  listed in the report. Before this, every transfer ran at the tuned pair's
  0.01 tick: the F1 XRP artifact.

**What changed in the verdicts.** Both candidates are still REJECTED on A2.
Only the soft attacks moved.

| | #2716 BTC before | #2716 after | #3214 ETH before | #3214 after |
|---|---|---|---|---|
| A4 | PASS, 100% of 24, worst PF 1.09 | **fail**, 94% of 48, worst PF **0.81** | PASS, 100% of 24, worst 1.29 | PASS, 100% of 48, worst 1.19 |
| A7 ETH / BTC | ETH 1.09/32t → held (net > 0) | ETH 1.09/32t → **does not hold** | BTC 0.97/42t | BTC 0.97/42t |
| A7 SOL | 0.42/41t | 0.42/41t | 0.70/59t | 0.70/59t |
| A7 XRP | 0.04/29t (tick 0.01) | **0.88/17t** (tick 0.0001) | 0.03/59t (tick 0.01) | **1.06/29t** (tick 0.0001) |
| A7 | PASS | **fail** | fail | fail |
| soft fails | none | **A4, A7** | A7, A8 | A7, A8 |
| verdict without A2 | SURVIVED | **FRAGILE** | FRAGILE | FRAGILE |

**What this shows.**

- **A4 now separates the two candidates.** #2716's worst neighbours are all
  at ±25%: `adx_threshold` 26.6 → 33.25 gives PF 0.81 on 24 trades,
  `rsi_buy` +25% gives 0.98, and `stop_atr` −25% gives 0.99. At ±10% it
  looked like a plateau; one step further it is an edge. #3214 holds PF ≥ 1.19
  on all 48 neighbours. That is the first A4 result in the project that is
  not 100%/PASS, and the first candidate to clear the stricter rule.
- **#2716's A7 pass hung on one marginal pair.** It passed only
  through ETH at PF 1.09. Under PF ≥ 1.1 it fails. Neither candidate
  transfers anywhere at a PF that would pay for an extra trial.
- **The XRP transfer numbers were a cost artifact, as F1 said.** At the real
  tick, XRP goes from PF 0.03–0.04 to 0.88–1.06: break-even, not collapse.
  XRP is now usable evidence in A7. It is still not evidence *for* either
  candidate.
- **Neither check should get looser.** If A2 were ignored, both candidates
  would be FRAGILE, not SURVIVED.
- **Not done.** The optimizer still writes `tick_size: 0.01` into every new
  XRP candidate's cfg, so XRP's own optimizer results remain mis-costed until
  `optimizer.sample_cfg` takes the tick from `main.TICK_SIZE`. That is one
  line, left for when XRP is re-run, because it changes stored results.

---

## F3. Effective number of trials: under N_eff, no verdict changes

*2026-10-26. `neff.py` + `validation.effective_trials`, run against a scratch
copy of forge.db. Information only: A2 still decides on N.*

**Method.**

- **Source.** This is from López de Prado, *Machine Learning for Asset
  Managers* (2020), ch. 8, and López de Prado & Lewis (2019). The request
  cited *Advances in Financial ML* ch. 8, but that chapter is about feature
  importance. The effective-trials clustering is in MLAM.
- **Common window.** Every trial of a pair/tf was re-run on one reference
  window: the longest pinned IS window, which is 2021-05 → 2025-03 for
  BTC/ETH/SOL. Correlations need a shared calendar, and the 600 older trials
  were pinned to a different, shorter window.
- **Series.** Each trial's trade returns became a daily series, with each
  trade spread across the days it was open.
- **Clustering.** Trials were clustered on the correlation distance
  √((1−ρ)/2). The book uses repeated k-means, which is too slow in stdlib
  Python. I used average-linkage hierarchical clustering instead. The number
  of clusters is chosen by the same ONC criterion: the t-stat of the
  silhouette scores.
- **Output.** N_eff is the number of clusters. A2 now reports it, with
  E[max SR] and DSR at N_eff, plus a **break-even N**: the largest number
  of independent trials at which the candidate would still pass.

**Result: N_eff = 2 on every 1h pair.** Daily and weekly buckets give the
same answer.

| pair 1h | N | N_eff | cluster sizes | mean \|ρ\| | participation ratio | clusters at ρ≥0.5 / 0.7 / 0.9 |
|---|---|---|---|---|---|---|
| BTC | 901 | 2 | 899 + 2 | 0.29 | 9 | 81 / 418 / 883 |
| ETH | 901 | 2 | 899 + 2 (weekly 896 + 5) | 0.29 | 9 | 86 / 482 / 893 |
| SOL | 900 | 2 | 478 + 422 | 0.25 | 12 | 99 / 500 / 890 |
| XRP | 300 | 2 | 283 + 17 | 0.49 | 4 | 9 / 31 / 159 |

At 4h, N_eff is 3 on BTC, ETH and SOL (300 trials each).

**Verdicts under N_eff.**

| # | SR/trade | trades | PSR vs 0 | DSR at N | DSR at N_eff = 2 | E[max SR] at N → N_eff | break-even N | verdict at N_eff |
|---|---|---|---|---|---|---|---|---|
| 3047 ETH | 0.080 | 252 | 0.904 | 0.000 | 0.067 | 1.07 → 0.17 | 0 | REJECTED |
| 3533 SOL | 0.085 | 231 | 0.911 | 0.000 | 0.070 | 1.11 → 0.18 | 0 | REJECTED |
| 3214 ETH | 0.113 | 162 | 0.934 | 0.000 | 0.216 | 1.07 → 0.17 | 0 | REJECTED |
| 2716 BTC | 0.141 | 139 | 0.957 | 0.000 | 0.337 | 1.09 → 0.18 | 1 | REJECTED |
| 2764 BTC | 0.188 | 74 | 0.953 | 0.000 | 0.541 | 1.09 → 0.18 | 1 | REJECTED (A1 too) |
| 3901 (2716 pruned) | 0.137 | 150 | 0.958 | 0.000 | 0.313 | 1.09 → 0.18 | 1 | REJECTED |

**None of the four passes under N_eff, and neither does any other
candidate attacked.** The break-even column is the clearest number.
#3047, #3533 and #3214 would fail **even if they were the only trial ever
run**: PSR against zero is below 0.95. #2716, #2764 and #3901 pass only at
N = 1, which means no deflation at all.

**Why N_eff = 2 should be treated with suspicion even though it changes no
verdict:**

1. **The trials have no real cluster structure.**
   - The silhouette t-stat is highest at k=2 and then falls almost steadily
     (BTC: 3.34 at k=2, 1.81 at k=4, about 1.0 at k=13–370). The trials form
     a continuum.
   - The criterion finds the cleanest single cut. On BTC and ETH that cut
     isolates 2 outlier trials. On SOL it is a near-even split, matching the
     50/50 `use_shorts` coin flip.
   - The book's ONC recursion would not fix this. It re-clusters only when
     at least two clusters fall below the mean quality, and with k=2 that
     cannot happen.
2. **Other reasonable estimators disagree by two orders of magnitude.**
   - The participation ratio of the correlation matrix gives 9–12.
   - Cutting the same dendrogram where clusters average ρ ≥ 0.5 gives 81–99.
   - At ρ ≥ 0.9, near-duplicates only, it gives 883–893, which is close to N.
   - Mean |ρ| is only 0.25–0.29. Most pairs of trials are *not* the same bet.
   - An estimate this sensitive to the method is a modelling choice, not a
     measurement.
3. **A small N_eff lowers the bar after the search has already looked.**
   - The 900 trials were each a chance to find a lucky parameter set,
     whether or not their return series correlate. Correlated trials still
     search different corners of the parameter space.
   - Selecting the best OOS PF out of 900 is still selection, and N_eff does
     not see that.
4. **The variance was kept as is.**
   - The book also replaces the across-trial SR variance with the variance
     of the cluster-level SRs. That has not been done here.
   - With two clusters, that variance would rest on two numbers, so it
     means nothing.

Even in the most favourable case, N_eff = 2 and E[max SR] ≈ 0.18, the best
per-trade Sharpe in the family (0.14 on 139 trades) is still below the
noise ceiling. The F1 conclusion does not depend on how N is counted.

**What would make N_eff useful.** It would help on a search whose trials
really do fall into distinct families, such as several strategy families
or a hypothesis-driven grid. Before it decides anything, require
agreement across estimators, for example ONC within 2× of the ρ ≥ 0.5 cut.

---

## F2. Ablation of #2716 and #3214: what earns its place

*2026-10-26. `ablation.py` (P2.5), run against a scratch copy of forge.db.*

For each candidate, ablation tried 14 single changes on its pinned IS and OOS
windows: 8 toggles flipped and 6 trap thresholds removed. A feature
**earns** its place if the strategy is worse without it on *both* windows,
with a full-window PF gap of at least 0.05. It **hurts** if the strategy is
better without it on both. **Decoration** means it changes which trades are
taken, but not in one direction. **Inactive** means it changes no trade at all.

| feature | #2716 BTC (longs only) | #3214 ETH (long+short) |
|---|---|---|
| use_shorts | off; adding it **hurts** (OOS PF 1.45 → 1.03) | **earns** (without it: OOS PF 1.62 → 0.94) |
| use_htf | **earns** (OOS −0.06) | inactive |
| use_vwap | decoration | decoration |
| use_volume / min_rel_vol | **earns** (OOS −0.26) | **earns** (OOS −0.43) |
| avoid_chop / max_compression | inactive | inactive |
| avoid_late via max_dist_ema | **earns** (OOS −0.13 / −0.08) | **earns** (OOS −0.15 / −0.20) |
| max_big_bars (late trap) | decoration | decoration |
| avoid_fake | **earns** (OOS −0.11) | off; adding it **hurts** (IS −0.15) |
| use_trailing | off; adding it **hurts** (PF 0.23) | off; adding it **hurts** (PF 0.32) |
| min_ema_sep | inactive | **earns** (OOS −0.45) |
| adx_threshold | decoration (IS +0.01, OOS −0.11) | **earns** (OOS −0.18) |

**Findings that hold on both candidates:**

- **The volume trap and the distance arm of the late trap are the only
  filters that earn their place on both.** Removing either costs
  0.08–0.43 of OOS PF.
- **`use_vwap` and the big-bar arm of the late trap are decoration on both.**
- **`avoid_chop` is inactive on both, and it is redundant by
  construction.** Its ADX arm (`adx <= adx_threshold`) is already excluded
  by `strong_long`/`strong_short`. Its compression arm
  (`sep < max_compression`) is already excluded by the hardcoded `sep > 1.0`
  in `trending` and by `strong_sep`, whenever
  `max_compression <= max(1.0, min_ema_sep)`. That holds for **1,235 of the
  2,124** 1h trials that had `avoid_chop` on. So the random search spends
  part of its trial budget, and raises the DSR bar, on a toggle that does
  nothing.
- **Trailing exits are disastrous on both** (PF 0.2–0.3). This fits
  `use_trailing` having died out among survivors.

**Pruned variants.** For each parent, every active feature that did not
earn its place was switched off, the result was registered in the scratch
DB, and the adversary attacked it:

| | toggles on | full trades / pf | OOS trades / pf | SR | adversary |
|---|---|---|---|---|---|
| #2716 parent | 6 | 139 / 1.34 | 40 / 1.45 | 0.141 | REJECTED (A2) |
| #3901 = #2716 pruned (−vwap, −chop, −big_bars, −min_ema_sep, −adx_threshold) | 4 | 150 / 1.33 | 42 / 1.34 | 0.137 | REJECTED (A2); A4 96% |
| #3214 parent | 5 | 162 / 1.31 | 50 / 1.62 | 0.113 | REJECTED (A2; soft A7 A8) |
| #3902 = #3214 pruned (−htf, −vwap, −chop, −big_bars) | 2 | 165 / 1.31 | 50 / 1.62 | 0.114 | REJECTED (A2; soft A7 A8) |

- **#3902 is #3214 with three fewer toggles and the same OOS trades.** By
  the simplicity rule it replaces #3214. Neither one survives.
- **#2716 lost 0.11 of OOS PF when pruned.** Almost all of that comes from
  removing `adx_threshold`, which the rule calls decoration because IS moved
  the other way (+0.01). Removing several decoration features at once is not
  free.
- **#3901 scored 96% on A4.** That is the first A4 result below 100% in any
  report (see F1).

**What this does not show.** These are PF differences on 40–50 OOS trades.
None of them is individually significant, and the IS half of each
comparison was selected on. This is evidence about which *parts* of the
family carry the result, to guide the next hypothesis, such as a
volume-plus-late-entry core. It is not evidence that the result is real.
Pruning cannot fix A2: it changes SR by less than 0.01, and the benchmark
is about 1.08.

---

## F1. V4.1 family, 1h, 2021-2026, 4 pairs: no validatable edge under random search

*2026-10-26. Phase 2 random search after P2.1–P2.4.2 (pinned windows, per-pair
seeds, per-trial Sharpe, history backfilled to 2021-05, XRP-USD added).*

### What was run

`optimizer.py --tf 1h --trials 300 --seed 2026`: 300 parameter sets per pair, a
different stream per pair (seeds 2026000–2026003), 1,200 trials, all new DNA.

| pair | IS window | OOS window | trials | passed OOS gate | mean IS trades |
|---|---|---|---|---|---|
| BTC-USD | 2021-05-17 → 2025-03-05 | → 2026-10-26 | 300 | 14 | 184 |
| ETH-USD | 2021-05-17 → 2025-03-05 | → 2026-10-26 | 300 | 13 | 195 |
| SOL-USD | 2021-06-30 → 2025-03-18 | → 2026-10-26 | 300 | 3 | 188 |
| XRP-USD | 2023-07-26 → 2025-10-30 | → 2026-10-26 | 300 | 0 | 188 |

The longer history did what P2.4 predicted for sample size: mean IS trades per
trial went from 55–74 (the 2024-09 → 2026-09 runs) to 184–195. On BTC, ETH and
SOL, A1 (sample size) now passes for every candidate except #2764. **The
binding constraint has moved from A1 to A2.**

### Leaderboard (OOS profit factor, `base_seed=2026`, passed=1)

| # | pair | IS pf / trades | OOS pf / trades | OOS net | IS SR | OOS SR |
|---|---|---|---|---|---|---|
| 2764 | BTC | 1.40 / 64 | 2.26 / 10 | +717 | 0.155 | 0.377 |
| 2804 | BTC | 1.13 / 88 | 2.06 / 17 | +1,233 | 0.059 | 0.318 |
| 2825 | BTC | 1.27 / 75 | 1.94 / 16 | +859 | 0.113 | 0.316 |
| 3214 | ETH | 1.15 / 112 | 1.62 / 50 | +2,308 | 0.065 | 0.213 |
| 2902 | BTC | 1.10 / 47 | 1.60 / 14 | +570 | 0.049 | 0.202 |
| 2710 | BTC | 1.13 / 86 | 1.58 / 20 | +822 | 0.063 | 0.201 |
| 2795 | BTC | 1.26 / 89 | 1.56 / 20 | +866 | 0.104 | 0.202 |
| 3533 | SOL | 1.11 / 164 | 1.50 / 67 | +2,394 | 0.047 | 0.174 |
| 3047 | ETH | 1.06 / 180 | 1.50 / 72 | +2,505 | 0.033 | 0.192 |
| 2716 | BTC | 1.29 / 99 | 1.45 / 40 | +1,266 | 0.123 | 0.183 |

Nearly every row has OOS > IS. That is what you would expect if you pick
the best OOS draw out of 300. It does not mean the edge strengthens over time.

### Adversary verdicts

| # | pair | full trades / pf | OOS trades / pf | SR/trade | DSR | E[max SR] | hard | soft | verdict |
|---|---|---|---|---|---|---|---|---|---|
| 3047 | ETH | 252 / 1.18 | 72 / 1.50 | 0.080 | 0.000 | 1.07 | A2 | A8 | REJECTED |
| 3533 | SOL | 231 / 1.23 | 67 / 1.50 | 0.085 | 0.000 | 1.11 | A2 | A8 | REJECTED |
| 3214 | ETH | 162 / 1.31 | 50 / 1.62 | 0.113 | 0.000 | 1.07 | A2 | A7 A8 | REJECTED |
| 2716 | BTC | 139 / 1.34 | 40 / 1.45 | 0.141 | 0.000 | 1.09 | A2 | — | REJECTED |
| 2764 | BTC | 74 / 1.51 | 10 / 2.26 | 0.188 | 0.000 | 1.09 | A1 A2 | — | REJECTED |

E[max SR] is computed over the 900 1h trials of that pair, with SR variance
0.110–0.119 from the ~890 trials that have ≥ 10 IS trades. Other attack
evidence:

- **A3 walk-forward:**
  - #2716: 5/5 folds profitable.
  - #2764: 5/5.
  - #3214: 4/5.
  - #3047 and #3533: 3/5.
- **A5 cost stress:**
  - At 2× costs every candidate stays above 1.10 OOS PF: #3214 is 1.45, #3047 is 1.37, #2716 is 1.22, #3533 is 1.22.
  - #2716 falls to 1.03 at 3× costs.
- **A8 drawdown:**
  - #3047, #3533 and #3214 have Monte-Carlo p95 drawdowns of 32%, 30% and 27% of capital, above the 25% budget.
  - #2716 has 22%.
- **A6 delayed fill:**
  - **#3047 improves when every fill is delayed one bar**, from +3,470 to +3,630 (+4.6%, 252 → 244 trades).
  - That is under the 10% margin, so A6 passes. But it is the same signature as #590 in §1 of V2_PROMPT.
  - The other four decay as they should: #2716 falls from +3,610 to +989, #3214 from +3,882 to +1,749.

**Read.** The best candidates in the family have a per-trade Sharpe of
0.08–0.19. The best of 900 pure-noise draws is expected to show about 1.08. The
gap is a factor of 6–13, so DSR rounds to 0.000. It is not borderline. More
trades won't fix that alone: DSR depends on
`(SR − E[max SR])·√(n−1)`, and SR is far below the benchmark. #2716 is
the most structurally clean candidate: it passes A1, A3 5/5, A4, A5, A6, A7
and A8, and fails only A2. It is also the one with the highest SR and the
fewest trades, which is what fitting noise looks like.

### XRP-USD transfer PFs 0.02–0.07 are a cost artifact, not evidence

In A7 every candidate collapses on XRP:

| # | A7 on XRP (as reported) | rerun with `tick_size=0.0001` |
|---|---|---|
| 3047 | pf 0.073 / 119 trades, −9,401 | pf 1.07 / 40 trades, +195 |
| 3533 | pf 0.023 / 78 trades, −8,878 | pf 0.87 / 37 trades, −389 |
| 3214 | pf 0.028 / 59 trades, −8,455 | pf 1.06 / 29 trades, +144 |
| 2716 | pf 0.038 / 29 trades, −5,561 | pf 0.88 / 17 trades, −165 |
| 2764 | pf 0.031 / 52 trades, −6,929 | pf 1.01 / 23 trades, +23 |

`engine.default_cfg()` fixes `tick_size=0.01` and `slippage_ticks=5` for
every pair. That is $0.05 of slippage per side. On BTC/ETH/SOL it is
negligible. On XRP (about $1–3.70 in the OOS window) it is 1.5–5% per side.
That is often more than the ATR stop distance, so a trade can be stopped
out on the bar it opens. The inflated trade counts in the reported column
come from this.

**Consequences:**

1. The XRP column of A7 is meaningless as it stands.
2. **XRP's 300 optimizer trials ran under the same costs, so "0 of 300 passed" on XRP says nothing about the strategy.** They also count toward `n_trials_all`.
3. With realistic costs, XRP transfer is roughly break-even (PF 0.87–1.07). That is consistent with no edge, but it is not a collapse.

**Fix (not made here):** make tick size per pair, from Coinbase
`quote_increment`, either in `main.PAIRS` metadata or on the candidate row.
Then re-run XRP before counting it as a fourth pair of evidence. Until
then, read this finding as **3 pairs of evidence plus one unusable run**.

### A4 (plateau) does not discriminate

Every adversarial report ever written, 20 reports on 13 candidates, scores 100%
on A4. Its worst neighbours are all still at PF ≥ 1.06. A test that every
candidate passes adds no information. It is too weak for three reasons:

1. **It runs on the OOS window, which is the window the candidates were
   selected on.** Neighbours ±10% away share most of the same trades, so
   they share the same OOS luck.
2. **±10% is small.** For integer EMAs it is often a single step, and many
   trap thresholds sit on coarse rules where a 10% nudge changes no trade at
   all.
3. **The bar is "net > 0".** A neighbour at PF 1.01 counts the same as one
   at the base PF.

**Proposed A4 v2 (not implemented; it would change verdicts):**

- Evaluate on the **IS window**, or the full window, not OOS.
- Nudge by **±25%**, plus **50 random joint perturbations**. Each draws every numeric parameter uniformly within ±25%, with a fixed seed. A joint draw is what catches a spike in several dimensions at once.
- Also **flip each toggle**, reusing `ablation.py`'s variants.
- Pass if the **median neighbour PF ≥ max(1.05, 0.75 × base PF)** and **≥ 70% of neighbours keep PF ≥ 1.0**.
- Report the lowest neighbour as well, so a cliff edge is visible.

Calibrate it before turning it on. Run it on 20 random *failed* trials. If
it does not reject most of them, it is still too weak.

### What this licenses

- Random search over V4.1 at 1h on BTC/ETH/SOL finds no parameter set
  whose Sharpe can be told apart from the best of the draws. This is the
  "written finding" branch of Phase 2's definition of done for **1h**. 4h
  has not been re-run on the long history.
- It does **not** show the family is useless. The ablation (P2.5) asks a
  narrower question with far fewer trials: which filters carry the result?
- More random trials would make this worse: E[max SR] grows with N.

### SR variance pools two different windows (caveat)

The 900 trials per pair behind E[max SR] mix 600 trials from the 2026-09
runs (IS 2024-09 → 2026-01, short windows) with 300 trials from this run
(IS 2021-05 → 2025-03). Per-trade SR does not depend on window length, but
its estimation noise does: short windows have fewer trades and noisier SR.
So the variance, about 0.11, is probably slightly too high and the benchmark
slightly too strict. It is not strict enough to matter here. Even halving the
variance cuts E[max SR] by only √2, to about 0.76, which is still 4–9× the
candidates' SR.
