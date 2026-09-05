# Target Architecture

## 1. Desired dependency direction

```text
Sources
  ↓
Normalized ticks + provenance
  ↓
Storage
  ↓
QA / analytics / backtest
  ↓
Policy gate
  ↓
Local export OR commercial publication
```

Policy must be checked at the boundary where data changes purpose.

## 2. Proposed module shape

This is a target, not an instruction to blindly rename files.

```text
fxtick/
├── sources/
│   ├── base.py
│   ├── dukascopy.py
│   ├── ctrader.py
│   ├── binance.py
│   └── local.py
├── policy.py
├── provenance.py
├── export_service.py
├── audit.py
├── duck.py
├── mt_export.py
├── instruments.py
└── gdrive.py
```

Applications/scripts may include:

```text
download_dukascopy_reference.py
collect_commercial.py
compare_sources.py
audit_dataset.py
app_local.py
cloud/app_cloud.py
cloud/publish_dataset.py
```

Adapt to the actual repository after inspection.

## 3. Source abstraction
A source should expose identity/policy separately from generic tick transformation.

Conceptual interface:

```python
class TickSource:
    source_id: str

    @property
    def policy(self):
        ...

    def fetch_ticks(self, ...):
        ...
```

Dukascopy-specific acquisition should not masquerade as a generic commercial fetcher.

## 4. Policy abstraction
Possible concepts:

```python
class LicenseClass:
    LOCAL_REFERENCE
    INTERNAL_ONLY
    DISTRIBUTABLE
    UNKNOWN
```

and:

```python
class ExportPurpose:
    LOCAL_TEST
    DISTRIBUTION
```

Policy defaults must be restrictive.

Example conceptual source policy:

```python
dukascopy = SourcePolicy(
    source="dukascopy",
    license_class="local_reference",
    redistributable=False,
    public_distribution_allowed=False,
)
```

For an unreviewed broker:
```python
redistributable=False
license_class="unknown"
```

until explicitly approved.

## 5. Conversion layer
Keep `mt_export` or equivalent focused on format conversion.

Prefer:
```text
policy.py
   ↓
export_service.py
   ↓
mt_export.py
```

rather than embedding licensing decisions deep inside CSV formatting code.

## 6. Private Dukascopy flow

```text
Dukascopy
   ↓
PRIVATE_REFERENCE / DUKASCOPY
   ↓
QA / Python backtest / source comparison
   ↓
LOCAL_TEST
   ↓
temporary local MT4/MT5 representation
```

No path from this flow to commercial/public publication is permitted.

## 7. Commercial-candidate flow

```text
cTrader/Binance/other source
   ↓
source-specific provenance
   ↓
raw/normalized commercial-candidate storage
   ↓
QA
   ↓
policy check
   ↓
only if explicitly licensed/approved
   ↓
publication/export
```

"Commercial candidate" does not mean "redistributable".

## 8. Google Drive
The existing Drive workflow is useful for historical testing, so do not simply remove Drive integration.

Instead:
- isolate private-reference roots from commercial roots
- use different folder IDs/configuration where practical
- make publication code incapable of selecting private-reference folders
- validate provenance again at publication time
- avoid share-link creation for private-reference datasets

## 9. Audit command
Add a repository/tooling command capable of scanning datasets and reporting:

```text
Dataset Audit
-------------
Dukascopy/private reference
Commercial eligible
Unknown provenance
Mixed/blocked
```

A commercial publish operation must fail if blocked or unknown datasets are selected.

## 10. Migration
Do not overwrite the only copy of existing historical Parquet files.

Suggested process:
1. inventory existing files
2. identify known Dukascopy history
3. create migration output separately
4. attach provenance/manifest
5. validate row count
6. validate min/max timestamps
7. validate prices/checksum or equivalent
8. only then switch readers to the migrated dataset

## 11. cTrader insertion point
Add cTrader only after provenance/policy boundaries exist.

Required technical verification before relying on it:
- historical lookback depth
- paging limits
- bid/ask request/merge behaviour
- tick limits
- price decoding
- volume availability
- OAuth refresh
- symbol mapping
- demo/live feed equivalence
- gap/reconnect behaviour
- licensing/redistribution terms

## 12. Testing requirements
At minimum add tests for:
- Dukascopy distribution blocked
- Dukascopy local test allowed by app policy
- unknown source blocked
- mixed dataset blocked
- explicit approved source allowed
- renamed/copied Dukascopy file remains blocked by provenance
- sidecar/Parquet metadata disagreement handled safely
- missing metadata blocked
- cloud listing excludes blocked datasets
- publication endpoint rechecks policy
