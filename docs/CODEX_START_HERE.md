# Codex Start Here

Open the actual `fx-tick-sync` repository in Codex and copy this handoff pack into the repository root.

Then give Codex this first instruction:

> Read `AGENTS.md` and every file under `docs/`. Do not modify code yet. Inspect the complete repository and map the actual current data flow, especially every path from Dukascopy acquisition through Parquet/Google Drive/Streamlit/MT4/MT5/ZIP/share links. Compare the actual implementation with the handoff specification. Report (1) current architecture, (2) discrepancies/incorrect assumptions in the handoff, (3) concrete contamination/distribution risks, (4) proposed phased file-by-file implementation plan, and (5) tests required. Preserve the existing historical-backtest capability. Wait for approval before modifying code.

After reviewing that report, a suitable second instruction is:

> Implement Phase 1 only: provenance + policy + tests. Keep existing behaviour working unless a change is required for safety. Do not yet implement cTrader or migrate/delete historical data. Show the diff summary and exact test results when complete.

Then proceed phase by phase.

## Important
The documents describe the intended architecture based on prior discussion and the uploaded project snapshots. The live repository is authoritative for actual filenames, dependencies, and call paths. Codex must inspect it rather than blindly applying assumed names.
