# StrategyForge

A research factory for systematic strategies. The collector (`app/main.py`)
archives hourly crypto candles and market context into `forge.db` and runs the
research jobs on a schedule. Everything else in `app/` is a one-shot CLI:
optimizer, adversary, validation, lineage, paper reconciliation, health, Pine
export, and the TSP allocation module. Nothing in this repo places live orders.

**Read `docs/V2_PROMPT.md` and `docs/FINDINGS.md` before any research task.**

## Where you run

- `claude-dev` container on Matrix (Unraid). The repo is at `/work`.
- Live DB: `/work/data/forge.db` (`FORGE_DB` is set). The collector writes to it
  every hour, so see the bulk-write rule below.
- davesbridge data, read-only: `/bridge` (`/bridge/signals.db`).
- CLIs run from `/work/app`. Tests: `python -m unittest discover -s tests`.
- The collector's jobs run the copy of `app/` baked into its image, not this
  share. A change to `main.py` or to a module a job runs takes effect only after
  a rebuild and recreate on Matrix (`docs/V2_INSTALL.md`). This container can't
  do that.

## Rules (`docs/V2_PROMPT.md` §10)

- Never loosen adversary thresholds.
- `approved` is human-only.
- Never touch davesbridge's `DRY_RUN` / `KILL_SWITCH` handling.
- Bulk writes follow §10.9: compute first, then write; commit in batches; be
  restartable; test on a scratch copy first.
- Never commit secrets or `WEB_TOKEN`.

## Findings so far

- **F9:** Core family at 1h: #4027 is the best candidate, and it still fails A2. Per-trade SR ~0.1 can't be validated at 1h.
- **F10:** Null. As a four-pair book, #4027 and #3214 get worse, and no verdict changes.
- **F11:** Null. Squeeze family at 1h: all 14 testable candidates rejected on A2.
- **F12:** TSP, proxy data: no tactical strategy beats 100% C after 2009. They buy smaller drawdowns, not return.
- **F13:** #4027 registered as the N = 1 forward test: PSR vs 0 ≥ 0.95 on forward trades only, ~76 trades at SR 0.184, no re-tuning.
- **F14:** TSP on official prices: F12 stands, nothing beats 100% C. DSRs above 0.95 come from one config moving the noise ceiling, not from an edge.
- **F16:** Core at 1d on ten ETFs: all 8 testable candidates rejected on A2. The best SR so far (#6377, 0.274) is market drift, not timing. No F15 exists.
- **F17:** Pre-registered ETF trend book, N = 1: FAIL against buy-and-hold (PSR 0.032). It cut risk more than return, but no forward test is earned.

## State of play

- F10, F11 null.
- No further 1h family searches.
- The core 1d ETF budget is spent (`optimizer.DECLARED_BUDGETS`). ETF bars are frozen in snapshot `etf-2026-10-30`.
- No strategy in this repo has beaten buy-and-hold. Changing Phase 10's rule after F17 is a new hypothesis and needs its own registration.
- Forward tests #3214 and #4027 running. Both are marked `forward_test`; neither has a forward trade yet.
