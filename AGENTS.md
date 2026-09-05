# fx-tick-sync — Codex Project Instructions

## Mission
This repository is being developed into a commercial MT4/MT5 historical tick-data validation and conversion platform.

Before changing code, read:
- `docs/PRODUCT_SPEC.md`
- `docs/DATA_POLICY.md`
- `docs/ARCHITECTURE.md`
- `docs/TODO.md`

## Non-negotiable rules
1. Dukascopy-derived market data MUST NOT enter a commercial/public distribution path.
2. Dukascopy data MAY remain usable as `PRIVATE_REFERENCE` for the owner's historical research, QA, comparisons, Python backtests, and local MT4/MT5 validation, subject to final legal review.
3. Do not remove the owner's existing historical-validation capability merely to isolate Dukascopy.
4. Google Drive private-reference storage and commercial/export storage must be logically separated. Whether private cloud retention is legally permissible remains subject to legal review; do not describe it as legally guaranteed.
5. `PRIVATE_REFERENCE -> EXPORT_SHARED` is prohibited.
6. Unknown provenance is denied by default (`fail closed`).
7. If a dataset is mixed or derived from multiple sources, inherit the strictest applicable policy. One non-redistributable parent/tick is sufficient to block distribution.
8. Never infer redistribution rights merely because data is obtainable through an API, demo account, public archive, cTrader, or a broker.
9. Do not mark IC Markets, AXIORY, FxPro, cTrader, or another broker feed as redistributable until its applicable terms/licence have been reviewed and explicitly approved in project configuration.
10. Keep conversion logic separate from distribution-policy enforcement.
11. Do not delete or rewrite working functionality without first tracing call sites and tests.
12. Preserve UTC as the internal/master time basis unless the specification explicitly requires otherwise.
13. Prefer idempotent operations and auditable provenance.
14. Before destructive or large refactors, explain the proposed change and affected files.
15. After implementation, run tests and report exact commands/results.

## First Codex task
Do NOT immediately rewrite the repository.

First:
1. Inspect the complete repository.
2. Identify the actual current architecture and call graph.
3. Compare it with the documents under `docs/`.
4. Identify every path through which Dukascopy-derived data can reach Google Drive, Streamlit/cloud output, ZIP/CSV/Parquet export, MT4/MT5 distribution, or a share URL.
5. Produce an implementation plan grouped into small phases.
6. Identify assumptions in these handoff documents that do not match the actual code.
7. Only modify code after the user approves the plan.

## Coding direction
Target separation:

- source acquisition
- normalized tick storage
- provenance
- policy
- transformation
- analytics/backtesting
- publication/distribution

The policy layer should be fail-closed and independently testable.

Suggested concepts:
- `SourcePolicy`
- `LicenseClass`
- `ExportPurpose.LOCAL_TEST`
- `ExportPurpose.DISTRIBUTION`
- `assert_distribution_allowed()`
- `audit_dataset`

Names may change if the existing architecture suggests a better implementation. Preserve intent, not necessarily these exact class/file names.

## Security / secrets
Do not commit:
- OAuth tokens
- Google credentials
- broker credentials
- API secrets
- account IDs or other secrets

Use environment variables / existing secret-management mechanisms.
