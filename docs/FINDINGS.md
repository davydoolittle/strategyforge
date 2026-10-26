# StrategyForge findings

Written results, newest first. Each one says what was run, what the numbers
are, and what they do and do not license. Numbers are reproducible from the
pinned windows on each candidate (P2.1).

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
