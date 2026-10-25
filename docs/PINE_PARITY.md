# Pine export parity log

| date | candidate | pair / tf | engine window | engine trades | TradingView trades (same window) | matched | notes |
|---|---|---|---|---|---|---|---|
| 2026-10-25 | #1033 | BTC-USD 1h (COINBASE:BTCUSD) | 2024-10-01 → 2026-10-25 | 30 | 30 | **30 / 30** | Every entry and exit price identical to the dollar. Net +1,440 engine vs +1,405 TV; ~$8/trade gap is commission rounding. TV export timestamps carry a constant 1 h chart-timezone offset. Compiled on first paste, no edits. Source CSV: `data/exports/SF_c1033_BTC-USD_1h_COINBASE_BTCUSD_2026-10-25.csv`. |

Method: export List of Trades as CSV from Strategy Tester, run
`engine.backtest(conn, pair, cfg, start, end, 3600)` with the candidate's params_json,
match on direction + entry time (±2 h tolerance for timezone), compare prices.
Checklist item P1.1 in docs/V2_PROMPT.md: **done**.
