# Data Policy and Dukascopy Contamination Prevention

## Status
This document is an engineering risk-control specification, not legal advice.

The final commercial release must be reviewed against the then-current terms/licences of each data source.

## 1. Core rule
Dukascopy-derived market data must never reach a customer/public/commercial data-distribution path.

Dukascopy can remain useful as a private reference source for:
- historical strategy research
- QA
- source comparison
- Python backtesting
- local MT4/MT5 validation
- aggregate analytics/report generation

Do not interpret the above as a legal conclusion that every private cloud-storage use is permitted. That remains subject to legal review.

## 2. Storage zones

Recommended logical zones:

```text
FX/
├── PRIVATE_REFERENCE/
│   └── DUKASCOPY/
│       ├── XAUUSD/
│       ├── EURUSD/
│       └── ...
├── COMMERCIAL/
│   ├── CTRADER/
│   │   ├── IC_MARKETS/
│   │   └── AXIORY/
│   └── BINANCE/
├── REPORTS/
└── EXPORT_SHARED/
```

The important boundary is not the exact folder names. The important invariant is that reference-only data cannot flow into distribution.

## 3. Provenance requirements
Every normalized dataset must have machine-readable provenance.

Minimum concepts:
- source
- provider
- account/source type
- licence/policy class
- redistribution eligibility
- acquisition time
- acquisition mechanism
- parent/derived-from datasets where applicable

Possible schema fields:

```text
source
provider
account_type
license_class
redistributable
acquired_at
```

For large Parquet files, provenance may also be encoded in Parquet metadata and a sidecar manifest. Do not rely solely on filenames/folders.

Suggested sidecar:
`XAUUSD_2024_ticks.meta.json`

## 4. Fail-closed policy
Unknown provenance is NOT distributable.

Bad:
```text
unknown -> probably okay -> export
```

Required:
```text
unknown -> BLOCK
```

Distribution requires an explicit positive policy decision.

## 5. Mixed/derived datasets
A mixed dataset inherits the strictest policy of its parents.

Example:
```text
IC Markets + Dukascopy -> redistribution blocked
```

Even one Dukascopy-derived component must block raw/market-data distribution.

Do not "clean" provenance by aggregation, renaming, copying, resampling, converting to M1, converting CSV->Parquet, or converting to MT4/MT5 format.

## 6. Separate purposes
The software must distinguish at least:

### LOCAL_TEST
May allow a private/reference dataset to be converted temporarily for the owner's local validation.

### DISTRIBUTION
Requires explicit redistribution permission.

Example intended policy:
```text
Dukascopy:
  LOCAL_TEST    -> permitted by application policy
  DISTRIBUTION  -> blocked
```

This application policy does not itself establish legal permission.

## 7. Cloud-storage policy
Do not use a single `cloud_storage_allowed` flag.

Model at least:
- private_reference_storage_allowed (project policy; subject to legal review)
- commercial_storage_allowed
- public_distribution_allowed

The owner wants historical Dukascopy data to remain available for long-term backtests, including via a private Google Drive workflow if legally supportable.

The engineering architecture should therefore support private-reference isolation rather than deleting historical capability.

## 8. Export enforcement
Do not rely only on directory names.

Before any public/commercial export:
1. inspect dataset provenance
2. reject unknown provenance
3. reject any row/dataset/parent whose policy is not explicitly distributable
4. reject Dukascopy-derived data
5. log the decision

Suggested invariant:
```text
count(non-explicitly-redistributable rows or parents) > 0
=> DISTRIBUTION BLOCKED
```

## 9. Multiple barriers
Use defence in depth:
1. separate storage roots
2. provenance embedded with data
3. policy enforcement before export
4. cloud UI only lists eligible datasets
5. publication endpoint checks again
6. CI/audit check before release/publish

## 10. Analytics output
Keep market data separate from analytics results.

Suggested classes:
- RAW_DATA
- DERIVED_MARKET_DATA
- ANALYTICS_RESULT

Do not assume that resampling tick data into OHLC automatically makes it redistributable.

For externally shown QA results, prefer non-reconstructable aggregate outputs such as:
- PF/DD/trade count
- aggregate spread statistics
- tick-density ratios
- missing-period counts
- source comparison statistics
- equity/performance curves where appropriate

Avoid embedding downloadable/reconstructable raw tick arrays into public interactive charts.

## 11. Current code risks to investigate
Codex must confirm these against the actual repository before changing them:
- Dukascopy acquisition appears coupled to generic fetching.
- Existing sync/upload workflow may acquire Dukascopy data and upload Parquet to Google Drive.
- Existing distribution workflow may transform stored Parquet into MT4/MT5 files/ZIPs and expose share/download paths.
- Existing data schema may lack durable provenance.

Do not assume these observations are exact until the current repository has been inspected.
