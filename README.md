# StrategyForge

A research factory for systematic strategies, running on Matrix (Unraid).
The collector (`app/main.py`) archives hourly crypto candles and market
context into `forge.db` and runs the research jobs on a schedule; the other
`app/*.py` modules are one-shot CLIs (optimizer, adversary, validation,
paper trading, health). Nothing in this repo places live orders.

- `docs/V2_PROMPT.md`: scope, phases and working rules
- `docs/V2_INSTALL.md`: deployment on Matrix (collector image, `sf.sh`, `claude-dev`)
- `docs/FINDINGS.md`: every result, newest first
- Dashboard: `app/web.py` (read-only, port 8091, bearer token, LAN/Tailscale only); see `docs/V2_INSTALL.md`
- Tests: `python -m unittest discover -s tests` (stdlib only, no pip packages)

## TSP Tactical Allocation module

Analysis and alerts for the federal Thrift Savings Plan (funds G, F, C, S, I;
L funds for comparison only). **It never logs in to TSP or places an IFT.**
You make every interfund transfer yourself; the module tells you what its
rules would do, why, and what that did in backtests. Past performance does
not guarantee future results.

### The rules it enforces (`app/tsp_rules.py`)

- An IFT sets whole-percent targets adding to 100 across the whole balance.
- Per calendar month (of the fill date): 2 IFTs in any direction, then
  unlimited IFTs that only move money **into G**: no other fund may end with
  a larger share than it had at the fill close.
- A request before 12:00 ET on a business day fills at that day's close,
  otherwise at the next business day's close (NYSE calendar, US Eastern
  time computed without a tz database).
- Backtests: a signal on day T's close fills at T+`fill_lag_days`' close
  (default 1, i.e. submitted the next morning). 0 is refused: it would trade
  at the close the signal was computed from.
- Long only, no leverage, no costs. Every backtest's IFT log is re-checked by
  an independent verifier (`verify_audit`); a violation stops the run.

### Files

| file | what |
|---|---|
| `app/tsp_config.json` | every threshold, proxy, the base allocation, strategy parameters and sensitivity grids (`TSP_CONFIG` overrides the path) |
| `app/tsp_data.py` | fetch + cache. Official share prices → `tsp_prices` (source `tsp`); ETF proxies (adjusted closes) → `tsp_prices` (source `yahoo`); VIX, VIX3M, 10y, 3m, HYG, LQD, FRED 2s10s and HY OAS → the collector's `context` table as `tsp:<name>`, stamped at 16:00 ET of their date. Sources are swappable in the config. |
| `app/tsp_signals.py` | daily regime (risk-on / neutral / risk-off / crisis) with the inputs that drove it; major-shift flags with confidence and historical hit rate; the move-budget score |
| `app/tsp_strategies.py` | buy-and-hold benchmarks (100% C, 60/40 C/F, L 2050 on TSP data), dual momentum, 200-day trend filter, regime switching, shift capture |
| `app/tsp_backtest.py` | daily simulator under the rules, IFT audit log, metrics, walk-forward, parameter sensitivity, deflated Sharpe over every configuration run, sub-periods, fill-lag robustness |
| `app/tsp_report.py` | the daily report and ntfy alert; records the IFTs you actually make |

### Data

```
cd app
python tsp_data.py --update          # proxies + macro from Yahoo/FRED; also tries tsp.gov
python tsp_data.py --import ~/Downloads/fund-price-history.csv
python tsp_data.py --status
```

tsp.gov answers scripted downloads with HTTP 403 from Matrix. The fetcher still
tries daily with an honest User-Agent; the reliable path is to download the
CSV from tsp.gov/share-price-history in a browser (all funds, full range) and
`--import` it. Until you do, everything runs on proxies and the report says so:
C≈SPY, S≈VXF, I≈VEU (EFA before 2007), F≈AGG, G≈13-week T-bill accrual. The
proxy G understates the real G Fund, and no ETF matches the I Fund's index
since its 2024 change; once TSP prices are imported, `tsp_data.tracking_report`
measures each proxy's tracking error.

### Backtests

```
python tsp_backtest.py --data proxy --out ../docs/TSP_BACKTEST.md
python tsp_backtest.py --data tsp                    # after --import
python tsp_backtest.py --data proxy --audit trend_filter   # every simulated IFT, with reason and fill prices
```

The report gives CAGR, max drawdown, volatility, Sharpe/Sortino over G, worst
month, time in G, IFTs and moves per month, information ratio and the
probability of beating 100% C, deflated Sharpe across all configurations
run, sub-periods (2004-09, 2010-19, 2020-), walk-forward (parameters picked
on 6 years, scored on the next 3), the full sensitivity grid, and the same
library at fill lag 2. Runs and audit logs are stored in `tsp_runs` /
`tsp_ifts`; the daily report quotes them as evidence.

Read them with suspicion. Tactical TSP strategies tend to look great over
2008-09 and then trail 100% C for years; the sub-period and walk-forward
tables exist to show exactly that. See FINDINGS F12.

### Daily report and alert

```
python tsp_report.py                                  # print
python tsp_report.py --update --notify                # what the collector runs every 6 h
python tsp_report.py --record-ift "C=60,S=20,G=20" --date 2026-11-02
```

The report: data date and source, regime and its drivers, shift flags of the
last days (confidence, hit rate, mean edge), the live strategy's suggestion
(`live_strategy` in the config) with reasoning and backtest evidence, flexible
moves left this month and business days left, and the 12:00 ET deadline for
the next close. It is pushed through ntfy (`NTFY_TOPIC`, same as `health.py`)
at most once per data date. Record each IFT you make so the move count and
current allocation are yours, not assumed; a recorded move TSP would have
refused (a third non-G move in a month) is rejected.

Scheduling: the collector's `JOB_SPECS` has a `tsp` job every 6 hours. The
collector runs from its image, so rebuild and recreate the `strategyforge`
container once (`docs/V2_INSTALL.md`, "When a rebuild IS needed") for the job
to start.
