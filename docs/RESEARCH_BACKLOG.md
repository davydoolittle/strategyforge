# Research backlog

What could be tested next, and the screens' declared thresholds. Seeded
2026-11-27 from V2_PROMPT's roadmap (sections 8g-8k) and FINDINGS. **An item here
is not approved.** Before anything runs it gets a registration in V2_PROMPT,
approved by the owner as written, with its hypothesis list, trial budget and
pass rule fixed. Every result, pass or fail, becomes a FINDINGS entry, and a
closed family's trials keep counting.

## Open candidates

| id | idea | status | prior evidence / constraint |
|---|---|---|---|
| B1 | P11 portfolio / regime mode across asset classes | deferred | F16, F17, F22: no timing skill per ETF; trend switching cuts drawdowns, not return. Needs a fresh rationale and a benchmark drift cannot beat. |
| B2 | P13 filing-type event study (the list in V2_PROMPT "P13 in detail") | deferred | Each type x direction x horizon is a trial; deflated across ~150. Needs P12's point-in-time universe (built, stocks.db) and prices. |
| B3 | P13 route A on 2016-on only (1.01 drift) | not registered | F21: 2016-on is 2.6% unpriced (under the 5% rule); choosing that window after seeing coverage makes it a new registration. No return has been seen. |
| B4 | P13 route B (forward recording) | running since 2026-11-09 | Verdict only at the registered size, years away. |
| B5 | Sector-only cross-sectional momentum | not registered | F20: the sector sleeve was slightly ahead (PSR 0.60), which is not evidence; would count F20 as trial 1. |
| B6 | Another trend rule (50-day, momentum score, per-pair trend) | not registered | F22 is trial 1 of that family. |
| B7 | 15m timeframe for squeeze (more trades per hypothesis) | not registered | F11; budget to be declared before the run. |
| B8 | Calendar effects on the ETF universe (below) | done 2026-11-27 (F24): Treasury month end passes, all else fails | F18 already tested turn-of-month on SPY (SUGGESTIVE, did not survive the correction): it counts. |
| B10 | Treasury month-end rule, cost-inclusive (hold IEF or TLT in the last five trading days of the month, cash otherwise) vs buy-and-hold | earned by F24, not registered | Counts F24 as trial 1 of the family; the effect is published (index extension) and may fade. |
| B9 | Profitability factor with complete data | not testable now | F23: 58.6% data coverage on free XBRL. |

## B8. Calendar-effects study: hypothesis list (declared 2026-11-27, before any run)

Universe: the research ten (`equities.UNIVERSE`), snapshot `etf-2026-10-30`.
Each hypothesis is tested once per ETF; the significance bar is Bonferroni
across all 4 x 10 = 40 tests (one-sided p < 0.05 / 40). F18's turn-of-month
reading on SPY is already on the record and is not re-scored as new.

| # | hypothesis | on days | against |
|---|---|---|---|
| C1 | turn of month | last trading day and first three of each month | all other days |
| C2 | day of week | each weekday separately (counted as one hypothesis, the most extreme weekday, with its own 5-way correction inside) | the other weekdays |
| C3 | options-expiration week | the week containing the third Friday | all other weeks |
| C4 | month end | the last five trading days of the month | all other days |

Written up as a FINDINGS entry whatever the result. Registration, data checks and
the test statistic go into V2_PROMPT before the run.

## Tracker screens (P9.4): declared thresholds

Display only: these choose names for the Tracker panel, never trades. Thresholds
are fixed here and shown on screen; a change is a dated line in DECISION_LOG.

| screen | rule | cap |
|---|---|---|
| momentum | top decile of the S&P 100 by 12-month return / 12-month realised volatility, excluding names in the top 10% by 1-month return | 10 names |
| value_quality | Piotroski F-score >= 7, trailing P/E below its SEC sector median, FCF yield > 0 | 10 names |
| insider_cluster | >= 3 open-market purchases by >= 2 distinct insiders within 30 days, by transaction date | 10 names |
