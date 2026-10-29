# StrategyForge findings

Written results, newest first. Each one says what was run, what the numbers
are, and what they do and do not license. Numbers are reproducible from the
pinned windows on each candidate (P2.1).

---

## F11. Squeeze family, 1h: all 14 testable candidates REJECTED on A2; per-trade SR is the same ~0.1–0.2, on fewer trades

*2026-10-29. P6.3, run from the claude-dev container on the live DB:
`optimizer.py --family squeeze --tf 1h --trials 300 --seed 6101`
(candidates #5101–#6300), then `adversary.py` on every squeeze survivor with
≥ 30 OOS trades (adversarial_reports #49–#62). The optimizer committed one
row per trial and is now restartable (§10.9). The run took about 6 minutes.*

**The hypothesis came from F6, not from a search.** Big 24h moves follow
low volatility and start near the 30-day extreme they move toward. Squeeze
enters long when all three hold:

- the 90-day percentile of Bollinger bandwidth (as `features.py` computes
  it) is ≤ `compression_pctile`;
- close breaks the previous `breakout_len` bars' high;
- close sits at ≥ `extreme_bias` of the 30-day range.

Shorts are mirrored. The stop is a fixed ATR stop with an R:R target. Six
parameters are searched.

### The search

| pair | trials | mean trades per trial (IS+OOS) | trials with IS net > 0 | median IS SR (≥ 20 trades) | passed OOS gate | ≥ 30 OOS trades | E[max SR] as A2 computes it |
|---|---|---|---|---|---|---|---|
| BTC | 300 | 60 | 44 | −0.164 | 0 | 0 | 0.530 |
| ETH | 300 | 57 | 107 | −0.030 | 19 | 5 | **3.486** (see below) |
| SOL | 300 | 69 | **185** | **+0.055** | 25 | 9 | 0.380 |
| XRP | 300 | 21 | 83 | −0.074 | 0 | 0 | **7.814** (see below) |

- **The family trades rarely.** A squeeze trial takes about 60 trades in
  5½ years. A core trial took 230–260 on BTC and ETH. The extra filtering
  costs evidence per hypothesis, which F9 named as the binding constraint.
- **On XRP the family barely trades.** The mean is 21 trades, and 158 of
  300 trials have fewer than 10 IS trades, because XRP's history starts in
  2023-07 and 90 days of it go to the percentile's warmup.
- **SOL is the first pair where the typical trial makes money.** 185 of 300
  squeeze trials are profitable IS, and the median IS SR is +0.055. Across
  every family and pair since per-pair seeds, no median was above +0.005
  (core BTC). The other medians run from −0.264 (V4.1 BTC) to −0.03, and
  V4.1's XRP median of −1.41 comes from the mis-costed pre-F4 run.
- **BTC is the opposite.** Only 44 of 300 squeeze trials are profitable,
  with a median of −0.164, and none passed the OOS gate. So the squeeze
  pattern is not a family-level edge either. See "What this licenses".

### A2's benchmark on ETH and XRP is broken by one degenerate trial each

`MIN_TRADES_FOR_SR_VARIANCE = 10` was set when V4.1's degenerate trials
were the few-trade ones (V2_PROMPT §1 finding 5). Squeeze has a new kind:

- **#5518 (ETH)** took exactly 10 IS trades, all stopped out. Its returns
  are almost identical, so SR = mean/std = **−19.4**.
- **#6166 (XRP)** did the same at **−32.2**.

Either trial alone inflates its pair's SR variance about 50×. ETH goes from
0.018 to 1.45 and XRP from 0.044 to 7.28, so the benchmark goes from about
0.4 to 3.5 and 7.8.

This only ever makes A2 stricter, so it cannot produce a false pass. But it
could hide a real one, so every ETH candidate was re-checked at a sane
variance: trials with ≥ 20 trades, E[max SR] 0.389. **No result changes.**
The best ETH DSR at the sane benchmark is 0.002. Nothing was changed in
the code. A fix would be a written change to the variance rule, for
example a ≥ 20-trade floor or excluding trials whose returns have
near-zero dispersion. Because the fix makes A2 easier to pass, it should
be decided on its own, not inside a run that would benefit from it.

**Fixed after this run (2026-10-29, the commit after P6.3).** The variance
now also excludes trials whose per-trade return std is below 0.1 risk
units (`validation.MIN_RETURN_STD_RISK_UNITS`), tested only where
|SR| ≥ 1. The std is backed out of the stored SR and IS net, so no backfill
was needed. On the live table it removes exactly #5518 and #6166:

- ETH squeeze: E[max SR] 3.486 → 0.467.
- XRP squeeze: 7.814 → 0.725.
- The other 13 family/pair/tf groups are unchanged to the third decimal.

The ≥ 20-trade floor was rejected. It lowers the bar in 14 of 15 groups
(e.g. core SOL 0.301 → 0.262, squeeze SOL 0.380 → 0.346), so it loosens A2
on trials that were never degenerate. Both trials still count in N. No
verdict above changes: every ETH squeeze candidate stays at DSR ≈ 0
against 0.467.

### Adversary verdicts

All 14 are REJECTED, and A2 alone decides it. Columns: "need" is the SR
that would give DSR 0.95 at that trade count; "DSR (sane)" is DSR at the
≥ 20-trade variance, information only.

| # | pair | full trades / pf | OOS trades / pf | SR/trade | PSR vs 0 | E[max] | DSR | DSR (sane) | need | break-even N | soft fails |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 5868 | SOL | 100 / 1.50 | 36 / 1.34 | **0.196** | 0.978 | 0.380 | 0.029 | 0.061 | 0.53 | 1 | A4 A7 A9 |
| 5750 | SOL | 97 / 1.45 | 39 / 1.27 | 0.179 | 0.968 | 0.380 | 0.019 | 0.042 | 0.52 | 1 | A4 |
| 5967 | SOL | 86 / 1.46 | 33 / 1.40 | 0.177 | 0.959 | 0.380 | 0.023 | 0.049 | | 1 | A3 |
| 5841 | SOL | 106 / 1.37 | 41 / 1.24 | 0.155 | 0.954 | 0.380 | 0.008 | 0.020 | | 1 | A4 A9 |
| **5833** | SOL | 110 / 1.35 | 42 / 1.44 | 0.145 | 0.941 | 0.380 | 0.005 | 0.015 | 0.52 | 0 | **none** |
| 5716 | SOL | 119 / 1.31 | 40 / 1.46 | 0.127 | 0.926 | 0.380 | 0.002 | 0.006 | | 0 | A4 A5 A7 A8 |
| 5843 | SOL | 164 / 1.29 | 60 / 1.28 | 0.122 | 0.950 | 0.380 | 0.000 | 0.001 | | 0 | A8 |
| 5498 | ETH | 123 / 1.22 | 43 / 1.28 | 0.098 | 0.872 | 3.486 | 0.000 | 0.000 | 0.50 (sane) | 0 | A4 A8 |
| 5610 | ETH | 92 / 1.21 | 31 / 1.31 | 0.092 | 0.821 | 3.486 | 0.000 | 0.002 | | 0 | A3 A4 A7 |
| 5982 | SOL | 145 / 1.21 | 47 / 1.37 | 0.088 | 0.863 | 0.380 | 0.000 | 0.001 | | 0 | A4 A8 |
| 5565 | ETH | 153 / 1.19 | 55 / 1.46 | 0.087 | 0.865 | 3.486 | 0.000 | 0.000 | | 0 | A3 A7 A8 A9 |
| 5883 | SOL | 218 / 1.19 | 66 / 1.36 | 0.083 | 0.898 | 0.380 | 0.000 | 0.000 | | 0 | A7 A8 |
| 5629 | ETH | 138 / 1.16 | 48 / 1.20 | 0.077 | 0.825 | 3.486 | 0.000 | 0.000 | | 0 | A4 A5 A8 |
| 5630 | ETH | 98 / 1.14 | 35 / 1.23 | 0.070 | 0.762 | 3.486 | 0.000 | 0.001 | | 0 | A4 A5 A8 A9 |

**What the table shows.**

- **The per-trade SR is the same as before.** It runs 0.07–0.20 here.
  Core showed 0.06–0.18 and V4.1 0.08–0.19. A family designed from a
  measured pattern, with 6 parameters instead of 10 or 21, lands in the
  same place.
- **The bar is higher than core's, because the trials vary more.** SOL's
  E[max] is 0.38, against core's 0.30. Fewer trades per trial mean noisier
  trial SRs. At about 100 trades, passing needs SR ≈ 0.52–0.53, which is
  2.7× the best candidate.
- **#5833 (SOL) is the second report ever with no soft fails**, after
  #4027 in F9. It has 5/5 folds, a 100% plateau (worst PF 1.02), PF 1.27
  at 2× costs, and decays when delayed. It still has SR 0.145 against a
  ceiling of 0.38: DSR 0.005, break-even N 0. PSR vs 0 is 0.941, so it
  would fail even as the only trial ever run.
- **Shorts are on in every testable candidate.** All 14 with ≥ 30 OOS
  trades have `use_shorts` on, against 38 of 44 among all survivors. That
  is a trade-count effect: long-only squeeze trials rarely reach 30 OOS
  trades. It is not evidence that the short side carries the edge.
- **A6 is clean apart from one mild flag.** #5716 nets slightly more when
  delayed (+3,266 → +3,402, +4%). That is inside the 10% margin, so it
  passes. The other 13 decay, several sharply (#5565 +2,589 → +509), as a
  breakout entry should.
- **A7 rarely holds.** No SOL candidate transfers to BTC (PF 0.61–0.90).
  ETH holds for some (1.10–1.31), and XRP holds for none of the SOL
  candidates. This repeats the family-level split above: SOL and ETH
  partly agree, BTC disagrees.

### Conclusion

Three families and 3,300 trials per the A2 count (900 V4.1 + 1,200 core +
1,200 squeeze; 4,200 distinct 1h parameter sets in all) produce per-trade
SR of about 0.1–0.2. None comes within a factor of 2 of what A2 needs
at 1h on these pairs. The squeeze family was the best-motivated of the
three, built from F6's only Bonferroni-significant result, and it shifts
nothing.

### What this licenses

- **No more 1h random searches on these four pairs.** Three families
  designed in three different ways all land at SR 0.1–0.2. F9's
  recommendation now rests on three families, not two.
- **One hypothesis is worth pre-registering: squeeze on SOL.** It is the
  only family/pair where most random trials made money IS (185/300). It
  is a property of the family on that pair, not of one parameter set,
  which is the kind of signal F10 said a book would need. To test it
  honestly:
  1. Fix one parameter set **now**, before any forward data exists: the
     median of the 25 SOL survivors, or #5833 as the one with no soft
     fails. Write it down.
  2. Evaluate it at N = 1, on SOL trades after 2026-10-29 only.
  3. At SR 0.15 that needs about 110 trades. At about 20 trades a year,
     that is **5–6 years**.

  That is too slow to be the plan, but it is the honest price of this
  hypothesis at 1h.
- **Fix the SR-variance rule before the next search.** The ≥ 10-trade
  floor no longer screens out degenerate trials. A ≥ 20-trade floor, or a
  near-zero-dispersion exclusion, needs a written reason in its commit
  (§2: never loosen without one). It would not have changed any verdict
  here.
- **More observations per hypothesis remain the only lever.** A 15m
  timeframe (P2.4.3) would multiply squeeze's trade count by up to 4×, if
  the compression pattern survives the change of scale. F6 measured it on
  24h moves. A 15m test would be a new hypothesis, with its trial budget
  declared before it is run.

---

## F10. As a four-pair book, #4027 and #3214 get worse, not better; no verdict changes

*2026-10-29. P6.1: `portfolio.py --candidate 4027` and `--candidate 3214`
on a scratch copy of forge.db. Read-only; nothing was written to either DB.*

**What was run.** Each candidate's cfg trades BTC, ETH, SOL and XRP out of
one $10,000 pool at 1% risk per trade:

- the home pair on its pinned windows;
- every other pair at its own verified tick, on its windows as they stood
  when the candidate was pinned (the A7 cut).

Execution is the engine's. The book re-sizes each trade from the pool's
realised equity at entry. The home leg reproduces the adversary report
exactly: #4027 151 trades / +5,768 / DSR 0.264; #3214 162 / +3,882.

**#4027 (core, home BTC, long only)**

| full window | trades | net | PF | max DD % | SR/trade | daily SR, annualised |
|---|---|---|---|---|---|---|
| **BTC (home)** | 151 | +5,768 | 1.49 | 10.3 | **0.184** | **1.32** |
| ETH | 155 | +719 | 1.07 | 15.3 | 0.035 | 0.27 |
| SOL | 110 | −10 | 1.00 | 18.5 | 0.007 | 0.05 |
| XRP | 72 | −512 | 0.90 | 14.6 | −0.038 | −0.22 |
| **book** | 488 | +5,228 | 1.12 | **33.5** | 0.066 | 0.69 |

On the OOS windows: BTC 43t / PF 1.64 / SR 0.243 / daily 1.90. The book
has 152t / PF 1.11 / SR 0.066 / daily 0.71, and SOL loses 1,013.

**#3214 (V4.1, home ETH)**

| full window | trades | net | PF | max DD % | SR/trade | daily SR, annualised |
|---|---|---|---|---|---|---|
| BTC | 149 | −538 | 0.95 | 20.7 | −0.011 | −0.08 |
| **ETH (home)** | 162 | +3,882 | 1.31 | 12.6 | **0.113** | **0.89** |
| SOL | 157 | −2,041 | 0.83 | 32.2 | −0.072 | −0.53 |
| XRP | 79 | +2,428 | 1.37 | 11.3 | 0.147 | 1.01 |
| **book** | 547 | +2,185 | 1.05 | **46.3** | 0.035 | 0.38 |

On the OOS windows: ETH 50t / PF 1.62 / daily 1.75. The book has 180t /
PF 1.03 / daily 0.32.

**A2 for the book.** The book is deflated against the home pair's
own-family trials, since that is where the one selection happened.

| | SR/trade | trades | PSR vs 0 | E[max SR] | DSR | break-even N | A2 |
|---|---|---|---|---|---|---|---|
| #4027 BTC alone | 0.184 | 151 | 0.990 | 0.234 (300 core) | 0.264 | 2 | FAIL |
| #4027 book | 0.066 | 488 | 0.933 | 0.234 | 0.000 | 0 | FAIL |
| #3214 ETH alone | 0.113 | 162 | 0.934 | 1.069 (900 V4.1) | 0.000 | 0 | FAIL |
| #3214 book | 0.035 | 547 | 0.795 | 1.069 | 0.000 | 0 | FAIL |

**No verdict changes.** Both stay REJECTED on A2, and the book's A2 is
further from passing than the home pair's.

### What the book does that the single pair doesn't

- **It has the room to diversify, and the other pairs don't use it.** The
  pairs' daily returns correlate weakly: mean ρ 0.18 for #4027 and 0.21
  for #3214. The pairwise range is 0.06–0.32, with BTC–ETH and ETH–XRP the
  highest. Suppose four legs each matched the home leg's daily Sharpe. At
  ρ ≈ 0.2 the book's daily Sharpe would be about √(4 / (1 + 3·0.2)) ≈ 1.6×
  the home leg's. That would be about 2.1 for #4027 instead of 1.32.
- **It adds trades, not SR per trade.** A trade's return as a fraction of
  the equity it was sized from is the same in the book as alone. So the
  book's per-trade SR is just the pooled per-trade SR of the four pairs.
  The other legs' SR is 0.01–0.04 for #4027, so pooling drags 0.184 down
  to 0.066. That is below the ceiling of 0.234, so 3× the trades push DSR
  from 0.264 to 0.000. This is F9's point in another form: more
  observations help only an SR that is already above the ceiling.
- **The daily Sharpe halves.** #4027 goes 1.32 → 0.69 and #3214 goes
  0.89 → 0.38. The legs with no edge add variance and no return.
- **The drawdown triples.** #4027 goes 10.3% → 33.5% and #3214 goes
  12.6% → 46.3%. All four positions are open at once at times, each
  risking 1% of the pool, and the zero-edge legs have long losing runs of
  their own. A real book would need `risk_pct` near 0.3% per leg to match
  one pair's drawdown. That scales net down by the same factor and leaves
  every Sharpe unchanged.
- **The XRP leg of #3214 beats its home pair.** It shows SR 0.147 and PF
  1.37 on 79 trades, against ETH's 0.113. On the OOS window alone XRP is
  only PF 1.06, so most of that comes from the 2023-07 → 2025-11 IS span.
  And BTC and SOL lose. This is the A7 picture again: a pattern that holds
  on one pair out of three is what noise looks like at these trade counts.
- **The one number that looks better is not deflated.** #4027's book has
  PSR(daily SR > 0) of 0.955 over 1,992 days. That says the book is
  probably not a loser. It does not count the 300 draws that chose its
  parameters, and there is no trial distribution of daily Sharpes to
  count them against. It licenses nothing.

### What this licenses

- **A book is not a way around A2 for these candidates.** It would help
  a parameter set that carries roughly the same per-trade SR on every
  pair. That would be a family-level edge, not a pair-level fit. Neither
  candidate has one. Their transfer legs are at or near zero, and #3214's
  best leg is not its home pair.
- **Use `portfolio.py` as a check, not a rescue.** For a future candidate,
  a book SR per trade close to the home SR, with low pairwise correlation,
  would be real evidence of a family-level edge. A book that dilutes it,
  as here, is evidence of a pair-level fit.

---

## F9. Core family, 1h: the best candidate in the project still fails A2. Per-trade SR ~0.1 cannot be validated at 1h

*2026-10-29. P5.3's run on the live DB: `optimizer.py --family core --tf 1h
--trials 300 --seed 5301`, then `adversary.py` on eight core candidates
(adversarial_reports #41–#48). Numbers below are read from those rows.*

**Note on ids.** On the live DB, candidates #3901–#5100 are this run.
F2's and F8's #3901/#3902 were children registered on scratch copies only.
On the live DB those two ids are core BTC trials.

### What was run

The search used per-pair seeds 5301000–5301003: 1,200 trials, 1,200
distinct DNAs, all `family='core'`. A2 deflates each candidate against its
own pair's 300 core trials. The V4.1 trials are not counted (P5.2).

| pair | IS window | OOS window | trials | passed OOS gate | mean IS trades | SR variance (≥ 10 trades) | E[max SR], N = 300 |
|---|---|---|---|---|---|---|---|
| BTC-USD | 2021-05-17 → 2025-03-07 | → 2026-10-29 | 300 | 15 | 258 | 0.0065 | 0.234 |
| ETH-USD | 2021-05-17 → 2025-03-07 | → 2026-10-29 | 300 | 21 | 229 | 0.0055 | 0.216 |
| SOL-USD | 2021-06-30 → 2025-03-20 | → 2026-10-29 | 300 | 1 | 147 | 0.0108 | 0.301 |
| XRP-USD | 2023-07-26 → 2025-11-02 | → 2026-10-29 | 300 | 1 | 120 | 0.0163 | 0.369 |

This is the first family run since F4. The optimizer took each pair's tick
from `main.TICK_SIZE`, so XRP is costed correctly here, unlike in F1.

**The noise ceiling is 4–5× lower than V4.1's.** V4.1's E[max SR] was
1.07–1.11 (F1), from an across-trial SR variance of about 0.11. Core's
trials vary 7–20× less (0.0055–0.016). That is what fewer, narrower
parameters buy: the best of 300 draws is not far above the typical draw.
This is the bar P2.5 and P5.3 were trying to lower, and it did come down.

### Adversary verdicts

| # | pair | full trades / pf | OOS trades / pf | SR/trade | PSR vs 0 | E[max SR] | DSR | SR needed to pass | break-even N | hard | soft | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4027 | BTC (long) | 151 / 1.49 | 43 / 1.64 | **0.184** | 0.990 | 0.234 | **0.264** | 0.359 | 2 | A2 | — | REJECTED |
| 4171 | BTC (long) | 196 / 1.33 | 65 / 1.25 | 0.141 | 0.979 | 0.234 | 0.089 | 0.342 | 1 | A2 | A4 A5 A8 | REJECTED |
| 3976 | BTC (long) | 183 / 1.28 | 65 / 1.23 | 0.130 | 0.962 | 0.234 | 0.077 | 0.351 | 1 | A2 | A4 A5 | REJECTED |
| 5027 | XRP (long) | 114 / 1.32 | 34 / 1.71 | 0.129 | 0.923 | 0.369 | 0.004 | 0.500 | 0 | **A6** A2 | A7 | REJECTED |
| 4471 | ETH | 207 / 1.21 | 66 / 1.22 | 0.097 | 0.924 | 0.216 | 0.040 | 0.320 | 0 | A2 | A5 A8 A9 | REJECTED |
| 4053 | BTC (long) | 224 / 1.22 | 69 / 1.34 | 0.095 | 0.931 | 0.234 | 0.015 | 0.329 | 0 | A2 | A5 A8 A9 | REJECTED |
| 4281 | ETH | 308 / 1.21 | 84 / 1.47 | 0.086 | 0.939 | 0.216 | 0.010 | 0.301 | 0 | A2 | A4 A8 | REJECTED |
| 4683 | SOL | 185 / 1.14 | 67 / 1.24 | 0.060 | 0.799 | 0.301 | 0.000 | 0.396 | 0 | A2 | A4 A8 | REJECTED |

"SR needed to pass" is the per-trade SR that would give DSR ≥ 0.95 at that
candidate's own trade count, skew, kurtosis and ceiling. "(long)" means
`use_shorts` is off.

**All eight are REJECTED, and A2 alone decides it.** A1 passes for every
one: each has ≥ 114 full and ≥ 34 OOS trades. Take A2 away and seven of the
eight are FRAGILE or better. The SR range is 0.06–0.18, against ceilings of
0.22–0.30 on BTC, ETH and SOL and 0.37 on XRP.

### #4027 is the strongest candidate in the project, and it still fails

#4027 is BTC, long only: EMA 8/28/52, ADX > 31.9, rel vol ≥ 1.39 on a
25-bar mean, distance ≤ 3.19%, stop 3.2 ATR, R:R 2.5.

- **It has no soft fails, and A2 is its only fail.** Only one other report
  ever had that: #2716 in F1, before F4 tightened A4 and A7. Under those
  rules #2716 fails both. #4027 passes them.
  - A3: 5/5 folds profitable.
  - A4: 100% of neighbours profitable; worst neighbour PF 1.11.
  - A5: OOS PF 1.41 at 2× costs.
  - A6: decays properly when delayed, +5,768 → +4,225.
  - A7: ETH 1.15/44t and XRP 1.18/23t hold; SOL 0.70 does not.
  - A8: p95 drawdown 22%.
  - A9: +2,055 outside its best regime (chop).
- **Its DSR is 0.264.** That is the highest in any report so far. Every
  V4.1 candidate showed 0.000 at N.
- **It still sits below the ceiling.** SR 0.184 against E[max] 0.234.
  Because its SR is *below* the ceiling and not just close to it, more
  history at the same SR would push DSR toward 0, not toward 1. To pass at
  151 trades it would need SR 0.36, about twice what it shows.
- **Break-even N is 2.** It would pass only if it had been one of at most
  two trials. PSR against zero is 0.990, so as a single pre-registered
  hypothesis it would clear the bar. It was not one: it is trial 126 of
  the 300 BTC draws.

### #5027 also fails A6

#5027 (XRP) **improves when its fills are delayed one bar**: +3,102 →
+3,794 (+22%, 114 → 110 trades). That is beyond A6's 10% margin, so it is a
second hard fail. It is the signature of #590 and #3047. It is also the
second-best core candidate by OOS PF (1.71). Like the V4.1 leaderboard in
F1, the top of the OOS ranking holds the candidates most likely to be
noise. #4683 (SOL) also improves when delayed (+1,990 → +2,060, +3.5%),
inside the margin, and it is the weakest candidate here by every measure.

### What SR a candidate would need

A2 requires DSR ≥ 0.95. That means the SR must exceed the ceiling by about
1.645 standard errors, and the standard error is roughly
√((1 − skew·SR + (kurt−1)/4·SR²)/(n−1)). With these candidates'
skew/kurtosis (about 0.5 / 1.5):

| ceiling (pair, N = 300) | 100 trades | 150 | 200 | 300 | 600 | 1,000 |
|---|---|---|---|---|---|---|
| 0.216 (ETH) | 0.37 | 0.34 | 0.32 | 0.30 | 0.28 | 0.27 |
| 0.234 (BTC) | 0.38 | 0.36 | 0.34 | 0.32 | 0.30 | 0.28 |
| 0.301 (SOL) | 0.45 | 0.42 | 0.41 | 0.39 | 0.36 | 0.35 |

At the 150–300 trades a 1h strategy gets from 5½ years, a candidate needs a
per-trade SR of **about 0.30–0.36 on BTC/ETH and 0.39–0.42 on SOL**. That
is 2× the best core candidate and 3× the typical survivor. No amount of
history brings the bar below the ceiling itself (0.22–0.30), and every
candidate in both families is below it. A bigger search raises the bar:
at BTC's variance, E[max] is 0.233 at N = 300, 0.267 at 1,200 and 0.279 at
2,100.

### Conclusion

**Two families and 2,100 trials per the A2 count (900 V4.1 trials per pair
plus 1,200 core; 3,000 distinct 1h DNAs in all) produce per-trade Sharpe of
about 0.1, at best 0.18–0.19.** Neither family has a parameter set that can
be told apart from the best of its own random draws at 1h on these pairs.

- V4.1 failed because its ceiling was very high (about 1.08).
- Core lowered the ceiling to 0.22–0.30, which is what a smaller, ablated
  family should do, and still failed: the candidates' SR did not rise with
  it.
- The edge, if it exists, is about SR 0.1–0.2 per trade. The data offers
  about 150–300 trades per candidate. At that size, any search over more
  than a handful of parameter sets makes validation impossible.

This is the "written finding" branch of Phase 2's definition of done,
for **1h**, now for two families.

**What this licenses.**

- **No more random search at 1h on these pairs**, for either family. More
  trials raise the ceiling. A third family searched the same way will hit
  the same wall unless its per-trade SR is about 2× anything seen so far.
- **A pre-registered forward test of #4027 is the one honest test left.**
  At N = 1 the bar is PSR vs 0 ≥ 0.95, and at SR 0.184 that takes about
  76 trades. #4027 trades about 28 times a year, so that is roughly
  **2.7 years** of trades after 2026-10-29. Those trades must come from
  the paper loop (P4) or `health.py`'s forward window, not from re-running
  history. If SR holds, it passes; if it halves to 0.1, it needs about 260
  trades (~9 years). This is slow, and it is still the only path that does
  not pay for the 300 draws that found it.
- **More observations per hypothesis.** Finer timeframes (P2.4.3, 15m)
  or more liquid pairs raise n per candidate. That only helps if the
  per-trade SR survives the change of timeframe. It is a new hypothesis,
  to be searched with a small, fixed trial budget declared in advance.

---

## F8. Trading V4.1 only on chop days doubles SR per trade, but the evidence stays the same

*2026-10-28. P5.4: `filters.py --filter "regime==chop" --register` on #3214
and #2716, then `adversary.py` on both children and both parents. Run on a
scratch copy of forge.db; nothing was written to the live DB.*

**What was tested.** The tag is P3.4's: chop means the last completed UTC
day had daily ADX(14) < 20. The filter is checked at the signal bar, and
only signals on chop days are let through. The threshold was not tuned:
one value, one trial per parent. The engine gets the tag from
`FeatureComputer.regime()`, with the same bounds A9 uses. `regime` is a
new categorical filter feature (`==` / `!=`).

**Caveat on independence.** F7 picked chop as the best regime per trade,
across annotated trades that include these two candidates' own IS and OOS
trades. So this is not an out-of-sample test of the hypothesis. It shows
what the filter costs and earns on the trades that suggested it.

**Parent vs child on the pinned windows**

| | IS trades / pf / net | OOS trades / pf / net | blocked IS (net) | blocked OOS (net) |
|---|---|---|---|---|
| #3214 ETH parent | 112 / 1.15 / +1,279 | 50 / 1.62 / +2,308 | | |
| #3901 = #3214 chop only | 19 / 2.06 / +1,361 | 11 / 1.58 / +474 | 93t (+74) | 39t (**+1,864**) |
| #2716 BTC parent | 99 / 1.29 / +2,081 | 40 / 1.45 / +1,266 | | |
| #3902 = #2716 chop only | 27 / 1.41 / +712 | 24 / 1.96 / +1,400 | 73t (**+1,046**) | 16t (−148) |

`filters.py`'s own test of a helpful filter is that the blocked trades net
negative in both windows. Neither child passes it:

- On #3214 the blocked trades made money in both windows. Most of the
  OOS profit (+1,864 of +2,308) was in trades the filter removed.
- On #2716 the filter helps OOS (PF 1.45 → 1.96, blocked −148). But it
  throws away +1,046 of IS profit, so the OOS gain is one-window evidence
  on 16 blocked trades.

**Adversary**

| | full trades / pf | SR/trade | PSR vs 0 | DSR (N = 901) | break-even N | hard | soft | verdict |
|---|---|---|---|---|---|---|---|---|
| #3214 parent | 162 / 1.31 | 0.113 | 0.934 | 0.000 | 0 | A2 | A7 A8 | REJECTED |
| #3901 child | 30 / 1.86 | 0.286 | 0.949 | 0.000 | 0 | **A1** A2 | A4 A7 A9 | REJECTED |
| #2716 parent | 139 / 1.34 | 0.141 | 0.957 | 0.000 | 1 | A2 | A4 A7 | REJECTED |
| #3902 child | 51 / 1.67 | 0.237 | 0.957 | 0.000 | 1 | **A1** A2 | A4 A7 A9 | REJECTED |

E[max SR] is 1.07 (ETH) and 1.09 (BTC) over 901 V4.1 trials per pair:
900 from the search plus the child. Since P5.2 that count covers V4.1
trials only.

**What this shows.**

- **The filter changes the per-trade numbers, not the evidence.** SR per
  trade roughly doubles (0.11 → 0.29, 0.14 → 0.24), and PF and drawdown
  improve (A8 p95 DD 27% → 11% on #3214). But trades drop 3–5×.
  - PSR against zero, which asks how sure we are that SR > 0 at all, does
    not move: 0.957 → 0.957 on #2716 and 0.934 → 0.949 on #3214.
  - Break-even N stays at 0–1. The filter turns many weak trades into a few
    better ones and buys no extra certainty.
- **A1 now fails on both children.** OOS has 11 and 24 trades, against a
  floor of 30. The full windows have 30 and 51 trades, so #3902 clears the
  full-window floor of 50 by one trade. That turns a single hard failure
  (A2) into two.
- **A9 fails by construction.** A chop-only strategy has no trades outside
  its one regime. A9 working as intended here is not new information.
- **A4 and A7 get worse.** A4's worst neighbours go below PF 1.0 (0.93,
  0.98), though 98% of neighbours stay profitable. A7 cannot pass: the
  other pairs' chop-day OOS windows hold only 7–17 trades.
- **A3 holds up only partly.** #3902 has 4/5 profitable folds. #3901 has
  3/5, on 4–7 trades per fold.

**What this licenses.** Nothing. `regime == chop` is not a filter for
V4.1. F7's "chop earns most per trade" is real on these trades, but trend
days are profitable too, just less so per trade. Removing them costs more
in sample size than it gains in per-trade quality. If regime is used again,
it should be as a sizing input (more risk on chop days), so no trades are
lost. That would be a new hypothesis, which needs trades the F7 sample
did not contain.

---

## F7. Regime tag: at 1h it is decided by the entry rule; on daily bars no candidate is single-regime

*2026-10-26. `features.RegimeTagger` and adversary A9 (P3.4), run on a
scratch copy of forge.db.*

- **The spec's literal tag is decided by the entry rule.** The spec says:
  slow EMA slope plus ADX at signal time, on the trade's own 1h bars. But
  V4.1 only enters when 1h ADX is above its threshold and the slow EMA
  slopes the trade's way. So across #2716, #3214, #3047 and #2764:
  - **No trade was ever tagged chop** (0 of 627).
  - The tag mostly repeats the trade's direction. Long-only #2764 is 100%
    trend_up with a 24-bar slope.

  An A9 built on that tag would fail every long-only candidate by
  construction.
- **As built,** the regime is the market state on the **last completed UTC
  day** before the signal:
  - trend_up or trend_down from the 5-day slope of the daily 50-EMA, when
    daily ADX(14) ≥ 20;
  - chop when daily ADX is below 20.

  It uses no data after the signal. The tag is stored in a new
  `trade_features.regime` column and is filled at annotation time. Existing
  rows are filled by `python features.py --backfill-regime`. On scratch,
  that tagged 3,166 of 3,206 rows; 40 fall in the first 55 days, before the
  daily EMA has warmed up.
- **A9 (soft).** It fails if the full-window trades outside the most
  profitable regime net ≤ 0. Two cases fail automatically: a strategy that
  trades only one regime, and one whose other regimes only give back money.

| | trend_up | trend_down | chop | best | net outside best | A9 |
|---|---|---|---|---|---|---|
| #2716 BTC (long-only) | +1168 / 50t | **−197 / 38t** | +2743 / 50t | chop | +972 | PASS |
| #3214 ETH | +1687 / 61t | +700 / 69t | +1706 / 30t | chop | +2388 | PASS |
| #3047, #3533, #2764 (from the prototype) | | | | | +733 to +1634 | PASS |

**What this shows.**

- **No verdict changed.** Both re-run candidates keep the soft fails from F4
  and stay REJECTED on A2.
- **A9 passes all five candidates tested.** Unlike A4 before F4, that is not
  because the bar is loose. Each of them really did make money in at least
  two daily regimes. A9 will bite on a strategy that only worked in, say,
  the 2024 bull run. None of the current leaders is one.
- **The surprise is chop.** Across all annotated trades (overlapping
  candidates, so treat this as a direction, not a size), chop days earn
  +51.7k on 936 trades, about +55 per trade. trend_up earns +14.3k on 1,126
  (+13 per trade) and trend_down +12.7k on 1,104 (+11 per trade). This is a
  1h trend-entry system, and it earns most per trade when the daily chart
  has no trend, meaning hourly bursts inside a daily range. It fits F6:
  big 24h moves follow low volatility. It is worth one P3.2-style
  evaluation of `regime == chop` once there are new trades to check it on.
  It is not a filter yet.

---

## F6. Event fingerprints: before a big day, volatility is LOW; context feeds are too young to say anything

*2026-10-26. `fingerprint.py` (P3.3), run against a scratch copy of
forge.db: 7,104 windows stored (BTC, ETH, SOL from 2021; XRP from 2023). A
rerun replaced rows instead of adding them.*

**Definition as built.**

- Windows of 24 bars are laid on one grid, so they never overlap.
- A window is a **move** if |close[i+24] − close[i]| ≥ K × ATR14[i].
- Features are read at bar i, the window's start, so nothing after i is
  used.
- My first version scanned bar by bar for moves but used the grid for
  non-moves. That made 83% of windows "moves" and skewed `hour` and
  `wick_ratio`, so it was discarded.

**K = 3, as specified, is not an event.**

- 31–34% of 24h windows on every pair clear 3 hourly ATRs.
- In hourly-ATR units, the median 24h move is 1.9 and the 95th percentile
  is about 8.
- At K = 3 the comparison is bigger days against smaller days. The report
  warns about this, and `--k` is a CLI option.
- K = 8 is about the top 5% of windows (349 moves out of 7,104) and is the
  reading that answers the question.

**Full history, OHLCV features, K = 8.** "Bonferroni" means the result still
passes p < 0.05 after correcting for the 150 AUCs tested.

| feature | AUC all | up | down | Bonferroni |
|---|---|---|---|---|
| vol_regime_pctile | 0.32 | 0.35 | 0.28 | yes |
| atr_pct | 0.33 | 0.35 | 0.32 | yes |
| bb_bw / bb_bw_pctile | 0.35 / 0.36 | 0.36 / 0.38 | 0.34 / 0.34 | yes |
| rv7_ann_pct | 0.40 | 0.40 | 0.40 | yes |
| vol_of_vol | 0.41 | 0.43 | 0.37 | yes |
| dist_30d_low_pct | 0.44 | 0.50 | **0.38** | all, down |
| dist_30d_high_pct | 0.47 | **0.42** | 0.53 | up |
| stretch_slow_ema_pct | 0.51 | **0.59** | 0.43 | up |
| dow | 0.56 | 0.52 | 0.60 | down |
| ret_24h, hour, consec_closes, rel_vol | 0.50–0.53 | | | no |

**What the table shows.**

- **Low volatility comes before big moves, and that is partly built in.**
  The threshold is K × ATR at bar i. A quiet bar i, or a weekend, which
  explains `dow` and `is_weekend`, makes a move in ATR units easier to
  reach. Volatility compression before expansion is also a real, known
  pattern. This table cannot separate the two explanations. A threshold in
  units of daily or 30-day volatility could, and would be the next step if
  this matters.
- **The directional features point to continuation, not reversal.**
  - Up-moves start closer to the 30-day high (AUC 0.42 on distance from the
    high) and further above the slow EMA (0.59).
  - Down-moves start closer to the 30-day low (0.38).
  - The pooled AUCs are about 0.5 because the up and down effects cancel.
- **These p-values are too small.** The four pairs move together on the same
  days, so 349 moves are perhaps 150 distinct market days. Even so, the
  volatility features are many standard errors from 0.5. The directional ones
  are close enough to the bar that they should be treated as hypotheses.
- **Context-feed era.** The collector has 54 days of data (2026-09-02 →
  2026-10-26), not the ~1 month the spec assumed. That gives 212 windows:
  70 moves at K = 3 and **9** at K = 8. The best context features at K = 3
  are fear_greed_chg_30d 0.61 and funding_rate_chg_30d 0.61, on 22–31 moves,
  and none pass Bonferroni. 30-day changes have existed only since
  2026-10-02. Nothing about funding, open interest, DVOL or the order book
  can be concluded yet. Re-running in about 3 months gives roughly 4× the
  data.

**What this licenses.** Nothing for trading. Two hypotheses are worth one
`feature_evals` / `filters.py` trial each, later:

1. V4.1 entries should avoid high-volatility regimes. This is already the
   opposite of what vol_of_vol suggested in P3.2, where its AUC was 0.58,
   higher in winners. That disagreement is itself a reason for caution.
2. Breakout entries near the 30-day extreme in the trade's direction.

The trap filters already approximate both.

---

## F5. vol_of_vol does not clear the P3.2 gate once duplicate trades are removed

*2026-10-26. `feature_evals.py`, run on a scratch copy of forge.db.*

- **The 0.611 was inflated by duplicates.** `trade_features` has 3,111 rows,
  but only 1,871 distinct signals (pair, tf, signal bar, direction). The 35
  annotated candidates often take the same trade. On the raw rows,
  vol_of_vol has AUC 0.598. Counting each signal once, it is **0.576**. The
  0.611 in V2_PROMPT was measured on fewer rows. Either way it is below the
  0.60 gate, so no `feature_evals` row was stored and vol_of_vol is not a
  candidate filter.
- **No other feature clears the gate.** The strongest OHLCV features are
  vol_of_vol 0.576, rv7_ann_pct 0.425 and bb_bw_pctile 0.570, all on about
  1,700–1,900 trades. The context features show larger AUCs (iv_rv_spread
  1.000, btc_dominance 0.285, fear_greed 0.288), but on only 11–26 trades,
  because the context collector starts 2026-09-01.
- **The P3.1 plumbing was exercised anyway.** `filters.py --candidate 2716
  --filter "vol_of_vol>=12"`, registered on scratch only, blocked 61 of 99
  IS trades and 30 of 40 OOS trades. The blocked trades were net
  **profitable** in both windows (+414 IS, +289 OOS). The child keeps 11 OOS
  trades, so A1 would reject it. That is consistent with the gate: an AUC of
  0.58 is not enough separation to pay for losing two-thirds of the trades.
- **Holdout trades arrive slowly.** `--annotate-new` extended the 35
  candidates to the newest candle and added 95 trades, but only 6 distinct
  signals came after the current sample's last signal (2026-10-15). At that
  rate, the 50 holdout trades a decision needs will take roughly three
  months of collector time.

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
