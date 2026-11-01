# TSP backtest: tsp data, 2004-10-11 to 2026-10-28

Fill lag 1 business day(s): a signal on day T's close fills at the close 1 business day(s) later. Max 2 flexible IFTs per month, further moves into G only.


## Full period

| strategy | CAGR % | max DD % | vol % | Sharpe | Sortino | worst month % | avg G % | days 100% G | IFTs | moves/month | max in a month | IR vs C | P(beats C) | DSR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| buy_hold_c | 11.24 | 55.2 | 18.9 | 0.50 | 0.71 | -16.8 (2008-10) | 0 | 0% | 0 (0 G-only) | 0.00 | 0 | +0.00 | 0.50 | 0.96 |
| buy_hold_6040 | 9.29 | 34.1 | 12.5 | 0.54 | 0.75 | -10.9 (2008-10) | 0 | 0% | 0 (0 G-only) | 0.00 | 0 | -0.39 | 0.04 | 0.97 |
| dual_momentum | 9.18 | 34.7 | 16.0 | 0.45 | 0.62 | -12.4 (2020-03) | 8 | 8% | 54 (0 G-only) | 0.20 | 1 | -0.17 | 0.22 | 0.93 |
| trend_filter | 9.85 | 33.8 | 13.9 | 0.53 | 0.73 | -12.4 (2020-03) | 17 | 17% | 24 (0 G-only) | 0.09 | 1 | -0.16 | 0.22 | 0.97 |
| regime_switch | 8.82 | 19.6 | 11.6 | 0.53 | 0.73 | -10.5 (2015-08) | 15 | 11% | 167 (7 G-only) | 0.63 | 3 | -0.23 | 0.14 | 0.97 |
| shift_capture | 9.03 | 35.0 | 14.4 | 0.47 | 0.64 | -14.5 (2020-03) | 8 | 5% | 87 (1 G-only) | 0.33 | 3 | -0.26 | 0.11 | 0.94 |

Sharpe is over the G Fund (its daily return is the risk-free rate). DSR deflates each strategy's Sharpe by all 31 configurations run here (noise ceiling 0.13 annualised); >= 0.95 would be evidence of skill, not luck. P(beats C) is the probabilistic Sharpe of the daily return over 100% C being > 0.

## Sub-periods (CAGR % / max DD %)

| period | buy_hold_c | buy_hold_6040 | dual_momentum | trend_filter | regime_switch | shift_capture |
|---|---|---|---|---|---|---|
| 2004-10-11..2009-12-31 | 2.0 / 55 | 3.2 / 34 | 9.4 / 18 | 10.9 / 10 | 8.6 / 15 | 5.2 / 33 |
| 2010-01-01..2019-12-31 | 13.4 / 19 | 10.2 / 14 | 7.1 / 28 | 9.2 / 19 | 7.8 / 15 | 9.7 / 18 |
| 2020-01-01..2026-10-28 | 15.2 / 34 | 12.6 / 26 | 11.7 / 34 | 9.7 / 34 | 10.1 / 20 | 10.7 / 35 |

## Walk-forward (params chosen on the train window by Sharpe, scored on the next test window)

**dual_momentum**: out-of-sample Sharpe 0.45, growth x2.59 (100% C over the same windows: Sharpe 0.86, x8.08); chosen params changed 3 time(s).

| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |
|---|---|---|---|---|---|
| 2005-2010 | 2011-2013 | `{"lookback_days": 252}` | 0.78 | 0.39 | 5.7 |
| 2008-2013 | 2014-2016 | `{"lookback_days": 252}` | 0.56 | 0.45 | 4.8 |
| 2011-2016 | 2017-2019 | `{"lookback_days": 126}` | 0.66 | 0.47 | 5.3 |
| 2014-2019 | 2020-2022 | `{"lookback_days": 126}` | 0.58 | 0.31 | 4.4 |
| 2017-2022 | 2023-2025 | `{"lookback_days": 189}` | 0.41 | 0.57 | 8.2 |
| 2020-2025 | 2026-2026 | `{"lookback_days": 252}` | 0.63 | 1.04 | 17.1 |

**trend_filter**: out-of-sample Sharpe 0.74, growth x4.35 (100% C over the same windows: Sharpe 0.86, x8.08); chosen params changed 3 time(s).

| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |
|---|---|---|---|---|---|
| 2005-2010 | 2011-2013 | `{"band_pct": 1.0, "ma_days": 150}` | 0.94 | 1.11 | 12.2 |
| 2008-2013 | 2014-2016 | `{"band_pct": 1.0, "ma_days": 150}` | 1.13 | 0.52 | 5.5 |
| 2011-2016 | 2017-2019 | `{"band_pct": 0.0, "ma_days": 150}` | 0.82 | 0.89 | 10.1 |
| 2014-2019 | 2020-2022 | `{"band_pct": 0.0, "ma_days": 300}` | 0.73 | 0.28 | 3.6 |
| 2017-2022 | 2023-2025 | `{"band_pct": 2.0, "ma_days": 100}` | 0.88 | 1.49 | 19.1 |
| 2020-2025 | 2026-2026 | `{"band_pct": 2.0, "ma_days": 100}` | 1.08 | 0.50 | 5.5 |

**regime_switch**: out-of-sample Sharpe 0.51, growth x2.37 (100% C over the same windows: Sharpe 0.86, x8.08); chosen params changed 5 time(s).

| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |
|---|---|---|---|---|---|
| 2005-2010 | 2011-2013 | `{"confirm_days": 3}` | 0.70 | 1.02 | 12.2 |
| 2008-2013 | 2014-2016 | `{"confirm_days": 1}` | 0.89 | 0.09 | 0.4 |
| 2011-2016 | 2017-2019 | `{"confirm_days": 5}` | 0.67 | 0.95 | 9.6 |
| 2014-2019 | 2020-2022 | `{"confirm_days": 10}` | 0.71 | -0.26 | -5.5 |
| 2017-2022 | 2023-2025 | `{"confirm_days": 1}` | 0.65 | 0.92 | 10.2 |
| 2020-2025 | 2026-2026 | `{"confirm_days": 3}` | 0.78 | 1.05 | 14.2 |

**shift_capture**: out-of-sample Sharpe 0.71, growth x5.05 (100% C over the same windows: Sharpe 0.86, x8.08); chosen params changed 3 time(s).

| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |
|---|---|---|---|---|---|
| 2005-2010 | 2011-2013 | `{"min_confidence": 0.6}` | 0.58 | 0.73 | 10.8 |
| 2008-2013 | 2014-2016 | `{"min_confidence": 0.65}` | 0.64 | 0.47 | 5.3 |
| 2011-2016 | 2017-2019 | `{"min_confidence": 0.55}` | 0.76 | 0.93 | 9.6 |
| 2014-2019 | 2020-2022 | `{"min_confidence": 0.7}` | 0.85 | 0.35 | 5.8 |
| 2017-2022 | 2023-2025 | `{"min_confidence": 0.7}` | 0.57 | 1.39 | 21.7 |
| 2020-2025 | 2026-2026 | `{"min_confidence": 0.7}` | 0.71 | 1.22 | 16.7 |

## Parameter sensitivity (full period)

**dual_momentum**: Sharpe 0.28 .. 0.45, median 0.38 across 5 settings.

| params | Sharpe | CAGR % | max DD % | IFTs |
|---|---|---|---|---|
| `{"lookback_days": 126}` | 0.42 | 8.60 | 35.3 | 72 |
| `{"lookback_days": 189}` | 0.38 | 8.10 | 34.1 | 69 |
| `{"lookback_days": 252}` | 0.45 | 9.18 | 34.7 | 54 |
| `{"lookback_days": 315}` | 0.28 | 6.50 | 36.1 | 64 |
| `{"lookback_days": 378}` | 0.36 | 7.84 | 34.6 | 44 |

**trend_filter**: Sharpe 0.48 .. 0.58, median 0.52 across 15 settings.

| params | Sharpe | CAGR % | max DD % | IFTs |
|---|---|---|---|---|
| `{"band_pct": 0.0, "ma_days": 100}` | 0.51 | 8.56 | 24.7 | 54 |
| `{"band_pct": 0.0, "ma_days": 150}` | 0.50 | 9.22 | 33.8 | 38 |
| `{"band_pct": 0.0, "ma_days": 200}` | 0.50 | 9.23 | 34.4 | 30 |
| `{"band_pct": 0.0, "ma_days": 250}` | 0.48 | 9.17 | 33.8 | 26 |
| `{"band_pct": 0.0, "ma_days": 300}` | 0.55 | 10.25 | 33.8 | 18 |
| `{"band_pct": 1.0, "ma_days": 100}` | 0.56 | 9.22 | 18.4 | 40 |
| `{"band_pct": 1.0, "ma_days": 150}` | 0.54 | 9.74 | 33.8 | 28 |
| `{"band_pct": 1.0, "ma_days": 200}` | 0.53 | 9.85 | 33.8 | 24 |
| `{"band_pct": 1.0, "ma_days": 250}` | 0.53 | 9.92 | 33.8 | 18 |
| `{"band_pct": 1.0, "ma_days": 300}` | 0.50 | 9.49 | 33.8 | 16 |
| `{"band_pct": 2.0, "ma_days": 100}` | 0.58 | 9.89 | 18.4 | 29 |
| `{"band_pct": 2.0, "ma_days": 150}` | 0.52 | 9.62 | 33.8 | 23 |
| `{"band_pct": 2.0, "ma_days": 200}` | 0.56 | 10.41 | 33.8 | 17 |
| `{"band_pct": 2.0, "ma_days": 250}` | 0.49 | 9.41 | 33.8 | 16 |
| `{"band_pct": 2.0, "ma_days": 300}` | 0.52 | 9.94 | 33.8 | 14 |

**regime_switch**: Sharpe 0.45 .. 0.53, median 0.51 across 4 settings.

| params | Sharpe | CAGR % | max DD % | IFTs |
|---|---|---|---|---|
| `{"confirm_days": 1}` | 0.47 | 7.57 | 19.5 | 360 |
| `{"confirm_days": 3}` | 0.53 | 8.82 | 19.6 | 167 |
| `{"confirm_days": 5}` | 0.51 | 8.91 | 20.8 | 105 |
| `{"confirm_days": 10}` | 0.45 | 8.36 | 28.2 | 36 |

**shift_capture**: Sharpe 0.46 .. 0.53, median 0.50 across 5 settings.

| params | Sharpe | CAGR % | max DD % | IFTs |
|---|---|---|---|---|
| `{"min_confidence": 0.5}` | 0.46 | 8.32 | 33.2 | 104 |
| `{"min_confidence": 0.55}` | 0.47 | 9.03 | 35.0 | 87 |
| `{"min_confidence": 0.6}` | 0.50 | 9.69 | 35.0 | 81 |
| `{"min_confidence": 0.65}` | 0.53 | 10.26 | 35.0 | 68 |
| `{"min_confidence": 0.7}` | 0.53 | 10.60 | 35.1 | 19 |

## Fill-lag robustness (lag 2)

| strategy | CAGR % | max DD % | Sharpe |
|---|---|---|---|
| buy_hold_c | 11.24 | 55.2 | 0.50 |
| buy_hold_6040 | 9.29 | 34.1 | 0.54 |
| dual_momentum | 8.90 | 33.8 | 0.43 |
| trend_filter | 9.75 | 33.8 | 0.53 |
| regime_switch | 8.61 | 21.6 | 0.51 |
| shift_capture | 10.23 | 35.0 | 0.53 |

_Hypothetical, backtested results on one historical path. Past performance does not guarantee future results. Analysis only: the module never places an IFT._
