# Decision log

One dated line per decision, oldest first. The evidence behind each is the
FINDINGS entry named; this file only records what was decided. Seeded
2026-11-27 from FINDINGS F1-F16 (there is no F15); later decisions are added as
they are made.

| date | decision | evidence |
|---|---|---|
| 2026-10-26 | V4.1 random search on four 1h pairs has no validatable edge: no candidate promoted; more trials raise the bar, so the lever is observations per hypothesis, not optimisation. | F1 |
| 2026-10-26 | Ablation keeps only components that earn their place; children of #2716 and #3214 are tracked as lineage, not promoted. | F2 |
| 2026-10-26 | Effective-trial counting (N_eff) is reported beside A2 but does not replace N; it changed no verdict. | F3 |
| 2026-10-26 | A4 and A7 tightened so the soft attacks discriminate; thresholds are never loosened afterwards. | F4 |
| 2026-10-26 | vol_of_vol is not adopted as a filter: it fails the P3.2 gate once duplicate trades are removed. | F5 |
| 2026-10-26 | Event fingerprints are context only; the young context feeds are not used as signals. | F6 |
| 2026-10-26 | The regime tag is a descriptive feature, not a selector: at 1h the entry rule decides it, and no candidate is single-regime on daily bars. | F7 |
| 2026-10-28 | No chop-only V4.1 filter: SR per trade doubles but trades fall 3-5x and the evidence does not improve. | F8 |
| 2026-10-29 | Core family at 1h: #4027 is the best candidate yet still fails A2; per-trade SR around 0.1 cannot be validated at 1h. | F9 |
| 2026-10-29 | No four-pair book of #4027 or #3214: pooling dilutes SR and verdicts do not change. | F10 |
| 2026-10-29 | No further 1h random searches on the four crypto pairs; squeeze on SOL is the one hypothesis worth a forward test. | F11 |
| 2026-10-29 | TSP tactical rules are drawdown control, not an edge; they are presented as a risk preference. | F12 |
| 2026-10-30 | #4027 registered as the N = 1 forward test (PSR vs 0 >= 0.95 on forward trades, no re-tuning). | F13 |
| 2026-11-01 | On official TSP prices F12 stands: nothing beats 100% C; DSRs above 0.95 there come from one config moving the noise ceiling. | F14 |
| 2026-11-01 | Core 1d on ten ETFs rejected; the declared 1d ETF budget is spent and the bars stay frozen in snapshot etf-2026-10-30. | F16 |
| 2026-11-27 | P10.3 calendar-effects study registered and approved as written (4 hypotheses x 10 ETFs, p < 0.05/40, same sign in both halves). | V2_PROMPT P10.3 |
