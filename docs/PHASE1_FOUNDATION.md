# Phase 1: independent provenance and policy foundation

## Scope

Only `fxtick/provenance.py`, `fxtick/policy.py` and their independent tests are
introduced. This document describes their contract. Existing entry points,
exporters, data files, requirements and workflows are unchanged. No real feed is
approved for distribution. Passing the tests does not mean the existing live
distribution paths are protected: wiring those paths is a later phase.

The code uses only the Python standard library, with no network, filesystem,
conversion, Google Drive or credential access. Importing it has no operational
side effects. Existing `fxtick/__init__.py` is unchanged.

## Data contract

`Provenance` is immutable. It carries `dataset_id`, `source`, `provider`,
`license_class`, `redistributable`, `acquired_at`, `derived_from`, `account_type`,
`acquisition_mechanism` and `schema_version=1`.

- Acquisition times must be timezone-aware and are normalized to UTC. `None`
  represents missing information, which blocks both purposes during evaluation.
- `derived_from` is an immutable tuple of parent dataset IDs. JSON uses an array.
  Callers must supply the complete reachable parent graph, including raw sources.
- Source/provider/account/mechanism identifiers are stripped, case-folded and
  hyphens normalized to underscores. Dataset IDs remain opaque and case-sensitive.
- Dukascopy and the `dukascopy_python` acquisition identifier, in source or
  provider, force `PRIVATE_REFERENCE` and `redistributable=False`. A conflicting
  positive metadata claim cannot upgrade the record.
- Default provenance is UNKNOWN. A path such as `COMMERCIAL/approved.parquet`
  never grants rights. There is no filename- or folder-based discovery API.
- `from_dict`/`from_json` reject missing required fields, unknown fields/versions,
  invalid types, duplicate JSON keys, duplicate parent IDs and naive timestamps.
  Missing optional scope fields default to `unspecified` (not a wildcard).
- `to_dict`/`to_json` serialize metadata in memory. No Parquet footer, sidecar or
  historical file is read or written in this phase.

## Trusted approvals and purposes

`SourcePolicy` is trusted project configuration passed separately from dataset
metadata. Never deserialize an approval from a customer dataset or accept a UI
checkbox as licence review. No production approvals are bundled.

A distribution approval requires a matching source/provider/account/mechanism
tuple, `DISTRIBUTABLE`, `redistributable=True` and a non-empty approval reference.
The caller remains responsible for the referenced review, applicability and
current validity. Phase 1 checks presence and exact scope, not the legal document.
A new evaluation must use the current configuration; a saved decision is not a
permanent publication credential. Duplicate config keys block evaluation.

| Dataset / configuration | LOCAL_TEST | DISTRIBUTION |
|---|---|---|
| Valid Dukascopy provenance | ALLOW by application policy | BLOCK, no override |
| UNKNOWN or missing provenance | BLOCK | BLOCK |
| Explicit source approval | Requires separate local_test_allowed | Requires positive distribution approval and compatible metadata |
| Unreviewed IC Markets / AXIORY / FxPro / cTrader / Binance | BLOCK by default | BLOCK by default |
| Mixed / derived | Every reachable record must permit local use | Every reachable record must permit distribution |

UNKNOWN LOCAL_TEST is deliberately fail-closed in the new foundation. This does
not change legacy historical validation: existing exporters do not invoke it yet.
Local permission establishes neither legal permission nor cloud-storage/sharing
permission. No cloud-storage permissions are inferred or implemented in Phase 1.

## Lineage evaluation

`evaluate_policy(root, purpose, parents=..., source_policies=...)` returns an
immutable `PolicyDecision` with `allowed`, `purpose`, `effective_license_class`
and per-dataset issue codes. The result ANDs permissions across the root and all
ancestors. A child cannot override a restrictive parent or trusted source policy.

The class ordering for this conservative summary is DISTRIBUTABLE < INTERNAL_ONLY
< PRIVATE_REFERENCE < UNKNOWN. Always use `allowed`, not the summary class alone:
permissions also depend on flags, scope, missing information and purpose.

The explicit source IDs `derived` and `mixed` denote transformations. They require
at least one parent, impose their own restrictions, and need no separate positive
source approval: all underlying source approvals must pass. Setting a child to
DISTRIBUTABLE is only a claim; it never grants rights over a denied parent. Use
the decision's effective class to report inherited restrictions rather than the
child's declared class. OHLC/MT conversions are not exceptions to lineage rules.

Missing parents, conflicting root records, parent ID mismatches and cyclic
lineage block evaluation. Shared-ancestor DAGs are valid. Evaluation uses an
iterative traversal so a deep chain cannot skip ancestry through recursion limits.
Only reachable records are evaluated; callers selecting several inputs must
include ALL selected inputs in the root's ancestry. Unselected data in a catalog
is not part of that dataset.

`assert_distribution_allowed` returns the positive decision or raises
`PolicyDeniedError`, retaining the decision and its reasons. Bad API argument
types/invalid serialized records raise exceptions; callers must never recover by
skipping validation and exporting anyway.

## Tests

From the repository root, using Python 3.12:

```text
python -S -B -m unittest discover -s tests -p "test_*.py" -v
```

`-S` excludes site-packages and proves the foundation needs no third-party
dependencies; `-B` avoids writing bytecode. No requirements or CI edits are
needed. Tests use fictional approvals and synthetic metadata only.

## Phase 2 connection points (not implemented)

1. Acquisition in `sync_and_upload.sync_symbol` / `fetcher`: attach verified source
   provenance to newly acquired CSV and preserve it through normalization.
2. `duck.normalized_select`, `union_sources`, `merge_month`, `write_parquet`: preserve
   complete ancestry independently of the existing five numerical columns.
3. Validate content identity and agreement between Parquet metadata and sidecars;
   this phase cannot detect metadata removed/replaced or a dishonest missing parent.
4. A separate export service should evaluate LOCAL_TEST or DISTRIBUTION before
   calling the unchanged `mt_export` format functions.
5. `sync_and_upload.build_distribution`: inspect the direct CSV path as well as
   any future Parquet path; reject before distribution artifacts are generated.
6. `app_cloud.list_symbol_files` and its generation/download path: filter listings
   and recheck the actual selected content after download and before delivery.
7. `gdrive.upload_file` / `share_anyone_reader`: isolate private/commercial storage
   and enforce publication checks again using current configuration and the exact
   artifact. Do not expose an unchecked publication fallback.

No connection, migration, folder switch, historical classification, legal approval,
broker collection, endpoint modification or existing-data deletion is part of this
phase.
