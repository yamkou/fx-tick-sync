# Implementation / Validation TODO

## Phase 0 — Preserve and inventory
- Put the repository under Git if not already.
- Create a clean pre-refactor commit/tag.
- Inventory both supplied code trees and determine which is authoritative.
- Trace current Dukascopy acquisition -> Parquet -> Drive -> Streamlit/export/share paths.
- Inventory current Google Drive folder IDs/configuration.
- Inventory historical Dukascopy files before moving or rewriting anything.
- Record current test commands and baseline behaviour.

## Phase 1 — Provenance and policy
- Add policy model.
- Add provenance model.
- Define fail-closed defaults.
- Define LOCAL_TEST vs DISTRIBUTION.
- Define mixed/derived strictest-policy inheritance.
- Add unit tests before wiring publication changes.
- Decide Parquet metadata + sidecar manifest implementation.

## Phase 2 — Isolate existing Dukascopy workflow
- Mark known Dukascopy history as private reference.
- Preserve historical backtest access.
- Separate private-reference Drive root/folder from commercial/export roots.
- Prevent private-reference share-link/publication generation.
- Move Dukascopy-specific fetch logic behind a Dukascopy source adapter where appropriate.
- Ensure cloud/commercial UI cannot select private-reference datasets.
- Add a second policy check immediately before publication/export.

## Phase 3 — Migration tooling
- Build non-destructive migration.
- Verify row counts.
- Verify min/max timestamps.
- Verify representative checksums/prices.
- Generate provenance manifests.
- Build `audit_dataset` command.
- Run audit over all existing data.

## Phase 4 — cTrader P0 validation
Before building a large production collector, test:
- IC Markets demo
- AXIORY demo
- XAUUSD
- one-week bid history
- one-week ask history
- paging
- 1-year lookback
- 3-year lookback
- actual historical depth
- ticks/day and storage estimate
- gaps
- spread distribution
- OAuth/token refresh
- volume availability
- symbol mapping
- demo/live equivalence where feasible

The historical lookback depth is a major product gate.

## Phase 5 — Collection foundation
- cTrader adapter
- bid/ask merge
- paging
- reconnect
- token refresh
- idempotent storage keys
- success-range table distinguishing "no ticks existed" from "fetch failed"
- daily monitoring
- second-source comparison
- storage/backup design

## Phase 6 — MT5
- Verify exact MT5 tick FLAGS behaviour on a real installation.
- Create custom symbols.
- Copy required symbol specifications.
- Implement/import ticks in safe chunks.
- Test one-week sample visually and through Strategy Tester.
- Confirm UTC -> broker/server-time handling.

## Phase 7 — MT4
- Inspect existing `mt_export`.
- Determine whether it creates HST only or appropriate FXT/tester inputs.
- Decide whether full MT4 99% tick-testing support is in scope.

## Phase 8 — Analytics / Python backtesting
- Build source comparison metrics.
- Build multi-strategy batch comparison.
- Generate reports without embedding raw reference tick data.
- Candidate metrics:
  - tick count/minute
  - missing minutes
  - spread median/p90/p95/p99
  - M1 OHLC differences
  - PF
  - DD
  - trade count
  - net profit
  - equity curve
  - period-by-period performance

## Phase 9 — Commercial release gate
- Audit every source licence/terms.
- Obtain specialist legal review where needed.
- Do not treat multi-source blending as eliminating upstream rights/restrictions.
- Do not use prop-firm feeds unless explicitly permitted.
- Confirm every dataset marked distributable has documented approval/evidence.
- Run dataset audit.
- Run tests.
- Review generated artefacts for accidental raw/reference data.
