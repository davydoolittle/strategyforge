# TSP backtest: proxy data, 2004-10-11 to 2026-10-28

Fill lag 1 business day(s): a signal on day T's close fills at the close 1 business day(s) later. Max 2 flexible IFTs per month, further moves into G only.

- proxy data: C=SPY S=VXF I=VEU/EFA F=AGG (adjusted closes); G = 13-week T-bill accrual, below the real G Fund rate

## Full period

| strategy | CAGR % | max DD % | vol % | Sharpe | Sortino | worst month % | avg G % | days 100% G | IFTs | moves/month | max in a month | IR vs C | P(beats C) | DSR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| buy_hold_c | 11.13 | 55.2 | 18.8 | 0.56 | 0.79 | -16.5 (2008-10) | 0 | 0% | 0 (0 G-only) | 0.00 | 0 | +0.00 | 0.50 | 0.90 |
| buy_hold_6040 | 9.16 | 34.2 | 12.7 | 0.61 | 0.87 | -10.7 (2008-10) | 0 | 0% | 0 (0 G-only) | 0.00 | 0 | -0.39 | 0.03 | 0.94 |
| dual_momentum | 10.07 | 35.6 | 17.0 | 0.55 | 0.76 | -12.5 (2020-03) | 6 | 6% | 62 (0 G-only) | 0.23 | 1 | -0.10 | 0.33 | 0.88 |
| trend_filter | 9.45 | 33.7 | 13.8 | 0.60 | 0.82 | -12.5 (2020-03) | 17 | 17% | 24 (0 G-only) | 0.09 | 1 | -0.18 | 0.19 | 0.92 |
| regime_switch | 8.10 | 22.4 | 11.9 | 0.57 | 0.77 | -10.9 (2015-08) | 15 | 11% | 168 (6 G-only) | 0.63 | 3 | -0.27 | 0.10 | 0.90 |
| shift_capture | 7.46 | 35.1 | 14.7 | 0.44 | 0.61 | -14.8 (2020-03) | 9 | 4% | 126 (3 G-only) | 0.48 | 3 | -0.39 | 0.03 | 0.76 |

Sharpe is over the G Fund (its daily return is the risk-free rate). DSR deflates each strategy's Sharpe by all 31 configurations run here (noise ceiling 0.29 annualised); >= 0.95 would be evidence of skill, not luck. P(beats C) is the probabilistic Sharpe of the daily return over 100% C being > 0.

## Sub-periods (CAGR % / max DD %)

| period | buy_hold_c | buy_hold_6040 | dual_momentum | trend_filter | regime_switch | shift_capture |
|---|---|---|---|---|---|---|
| 2004-10-11..2009-12-31 | 1.8 / 55 | 3.0 / 34 | 12.9 / 19 | 10.1 / 12 | 8.0 / 17 | 5.0 / 34 |
| 2010-01-01..2019-12-31 | 13.3 / 19 | 10.0 / 14 | 7.3 / 29 | 8.8 / 19 | 7.0 / 17 | 8.2 / 16 |
| 2020-01-01..2026-10-28 | 15.1 / 34 | 12.6 / 26 | 11.4 / 34 | 9.5 / 34 | 9.3 / 22 | 7.9 / 35 |

## Walk-forward (params chosen on the train window by Sharpe, scored on the next test window)

**dual_momentum**: out-of-sample Sharpe 0.47, growth x2.87 (100% C over the same windows: Sharpe 0.86, x7.97); chosen params changed 3 time(s).

| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |
|---|---|---|---|---|---|
| 2005-2010 | 2011-2013 | `{"lookback_days": 252}` | 0.76 | 0.48 | 7.5 |
| 2008-2013 | 2014-2016 | `{"lookback_days": 252}` | 0.54 | 0.57 | 6.7 |
| 2011-2016 | 2017-2019 | `{"lookback_days": 378}` | 0.59 | 0.71 | 9.0 |
| 2014-2019 | 2020-2022 | `{"lookback_days": 378}` | 0.62 | 0.06 | -1.8 |
| 2017-2022 | 2023-2025 | `{"lookback_days": 189}` | 0.42 | 0.88 | 12.4 |
| 2020-2025 | 2026-2026 | `{"lookback_days": 252}` | 0.64 | 0.71 | 11.6 |

**trend_filter**: out-of-sample Sharpe 0.72, growth x4.11 (100% C over the same windows: Sharpe 0.86, x7.97); chosen params changed 3 time(s).

| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |
|---|---|---|---|---|---|
| 2005-2010 | 2011-2013 | `{"band_pct": 1.0, "ma_days": 150}` | 0.88 | 1.07 | 11.8 |
| 2008-2013 | 2014-2016 | `{"band_pct": 1.0, "ma_days": 150}` | 1.05 | 0.49 | 5.2 |
| 2011-2016 | 2017-2019 | `{"band_pct": 0.0, "ma_days": 150}` | 0.79 | 0.87 | 9.9 |
| 2014-2019 | 2020-2022 | `{"band_pct": 0.0, "ma_days": 300}` | 0.71 | 0.27 | 3.4 |
| 2017-2022 | 2023-2025 | `{"band_pct": 2.0, "ma_days": 100}` | 0.85 | 1.47 | 18.3 |
| 2020-2025 | 2026-2026 | `{"band_pct": 2.0, "ma_days": 100}` | 1.05 | 0.49 | 5.4 |

**regime_switch**: out-of-sample Sharpe 0.55, growth x2.82 (100% C over the same windows: Sharpe 0.86, x7.97); chosen params changed 4 time(s).

| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |
|---|---|---|---|---|---|
| 2005-2010 | 2011-2013 | `{"confirm_days": 10}` | 0.64 | 0.87 | 11.9 |
| 2008-2013 | 2014-2016 | `{"confirm_days": 3}` | 0.70 | 0.21 | 1.8 |
| 2011-2016 | 2017-2019 | `{"confirm_days": 10}` | 0.61 | 0.98 | 10.8 |
| 2014-2019 | 2020-2022 | `{"confirm_days": 10}` | 0.65 | -0.27 | -5.8 |
| 2017-2022 | 2023-2025 | `{"confirm_days": 5}` | 0.56 | 1.08 | 14.9 |
| 2020-2025 | 2026-2026 | `{"confirm_days": 3}` | 0.71 | 1.00 | 13.8 |

**shift_capture**: out-of-sample Sharpe 0.58, growth x2.17 (100% C over the same windows: Sharpe 0.86, x7.97); chosen params changed 1 time(s).

| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |
|---|---|---|---|---|---|
| 2005-2010 | 2011-2013 | `{"min_confidence": 0.65}` | 0.50 | 0.87 | 13.9 |
| 2008-2013 | 2014-2016 | `{"min_confidence": 0.65}` | 0.57 | 0.43 | 4.9 |
| 2011-2016 | 2017-2019 | `{"min_confidence": 0.7}` | 10.43 | 21.92 | 1.6 |
| 2014-2019 | 2020-2022 | `{"min_confidence": 0.7}` | 12.26 | 8.24 | 0.8 |
| 2017-2022 | 2023-2025 | `{"min_confidence": 0.7}` | 13.52 | 25.76 | 4.8 |
| 2020-2025 | 2026-2026 | `{"min_confidence": 0.7}` | 14.33 | 25.75 | 3.8 |

## Parameter sensitivity (full period)

**dual_momentum**: Sharpe 0.33 .. 0.55, median 0.45 across 5 settings.

| params | Sharpe | CAGR % | max DD % | IFTs |
|---|---|---|---|---|
| `{"lookback_days": 126}` | 0.49 | 8.84 | 35.6 | 72 |
| `{"lookback_days": 189}` | 0.45 | 8.10 | 34.1 | 67 |
| `{"lookback_days": 252}` | 0.55 | 10.07 | 35.6 | 62 |
| `{"lookback_days": 315}` | 0.33 | 6.12 | 34.9 | 58 |
| `{"lookback_days": 378}` | 0.42 | 7.86 | 34.6 | 40 |

**trend_filter**: Sharpe 0.54 .. 0.66, median 0.58 across 15 settings.

| params | Sharpe | CAGR % | max DD % | IFTs |
|---|---|---|---|---|
| `{"band_pct": 0.0, "ma_days": 100}` | 0.56 | 7.94 | 25.3 | 56 |
| `{"band_pct": 0.0, "ma_days": 150}` | 0.56 | 8.68 | 33.7 | 40 |
| `{"band_pct": 0.0, "ma_days": 200}` | 0.56 | 8.77 | 34.4 | 30 |
| `{"band_pct": 0.0, "ma_days": 250}` | 0.54 | 8.77 | 33.7 | 26 |
| `{"band_pct": 0.0, "ma_days": 300}` | 0.59 | 9.61 | 33.7 | 18 |
| `{"band_pct": 1.0, "ma_days": 100}` | 0.62 | 8.75 | 20.1 | 40 |
| `{"band_pct": 1.0, "ma_days": 150}` | 0.60 | 9.28 | 33.7 | 28 |
| `{"band_pct": 1.0, "ma_days": 200}` | 0.60 | 9.45 | 33.7 | 24 |
| `{"band_pct": 1.0, "ma_days": 250}` | 0.56 | 9.04 | 33.7 | 20 |
| `{"band_pct": 1.0, "ma_days": 300}` | 0.56 | 9.10 | 33.7 | 16 |
| `{"band_pct": 2.0, "ma_days": 100}` | 0.66 | 9.62 | 18.9 | 29 |
| `{"band_pct": 2.0, "ma_days": 150}` | 0.58 | 9.19 | 33.7 | 23 |
| `{"band_pct": 2.0, "ma_days": 200}` | 0.59 | 9.51 | 33.7 | 19 |
| `{"band_pct": 2.0, "ma_days": 250}` | 0.55 | 9.00 | 33.7 | 16 |
| `{"band_pct": 2.0, "ma_days": 300}` | 0.58 | 9.56 | 33.7 | 14 |

**regime_switch**: Sharpe 0.46 .. 0.57, median 0.54 across 4 settings.

| params | Sharpe | CAGR % | max DD % | IFTs |
|---|---|---|---|---|
| `{"confirm_days": 1}` | 0.46 | 6.29 | 22.5 | 366 |
| `{"confirm_days": 3}` | 0.57 | 8.10 | 22.4 | 168 |
| `{"confirm_days": 5}` | 0.54 | 8.10 | 21.8 | 104 |
| `{"confirm_days": 10}` | 0.49 | 7.84 | 28.4 | 35 |

**shift_capture**: Sharpe -0.13 .. 0.57, median 0.44 across 5 settings.

| params | Sharpe | CAGR % | max DD % | IFTs |
|---|---|---|---|---|
| `{"min_confidence": 0.5}` | 0.44 | 6.97 | 40.2 | 152 |
| `{"min_confidence": 0.55}` | 0.44 | 7.46 | 35.1 | 126 |
| `{"min_confidence": 0.6}` | 0.50 | 8.56 | 35.1 | 100 |
| `{"min_confidence": 0.65}` | 0.57 | 9.81 | 35.1 | 75 |
| `{"min_confidence": 0.7}` | -0.13 | 0.01 | 54.4 | 13 |

## Fill-lag robustness (lag 2)

| strategy | CAGR % | max DD % | Sharpe |
|---|---|---|---|
| buy_hold_c | 11.13 | 55.2 | 0.56 |
| buy_hold_6040 | 9.16 | 34.2 | 0.61 |
| dual_momentum | 9.75 | 33.7 | 0.53 |
| trend_filter | 9.36 | 33.7 | 0.59 |
| regime_switch | 7.93 | 22.3 | 0.54 |
| shift_capture | 10.31 | 35.1 | 0.60 |

_Hypothetical, backtested results on one historical path. Past performance does not guarantee future results. Analysis only: the module never places an IFT._
