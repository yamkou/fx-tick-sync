# Phase 2: operational provenance and distribution boundaries

## Scope and validation status

Phase 1's provenance/policy modules and original 61 tests are unchanged.
Phase 2 connects them to acquisition, normalization, local conversion and delivery.
No actual history was registered, modified, moved, regenerated or deleted during
implementation. No Google Drive, Dukascopy, mail service or package repository was
contacted. Git push to the explicitly authorized development branch is separate.

Implementation is verified with standard-library tests, temporary synthetic files
and explicit engine/API fakes. Real-library integration tests are provided but
remain unexecuted in the implementation environment; see commands below. This is
not a claim that MT terminal import or a live cloud deployment has been validated.

## Flow before and after

Before:

```text
Dukascopy -> CSV -> DuckDB/month merge -> Parquet -> shared Drive root/update
             -> MT4/MT5 -> ZIP -> Drive anyone-reader -> summary/email
Drive Parquet -> Streamlit -> MT4/MT5 -> ZIP -> download_button
```

After:

```text
New Dukascopy -> marked frame -> content-bound CSV manifest
    -> bound Query with ALL input artifacts -> new Parquet + embedded lineage + manifest
    -> LOCAL_TEST -> new MT4/MT5/HST + inherited manifest -> optional LOCAL_TEST ZIP
    -> explicit PRIVATE_REFERENCE Drive root, owner-only ancestry, immutable snapshot

Owner explicitly selects ONE old file -> separate SHA-256/size ledger
    -> unchanged old file + matching ledger -> LOCAL_TEST -> new derived artifacts

Any distribution operation -> re-read current content/metadata
    -> Phase 1 checks complete graph and current trusted source policies
    -> exact content/lineage attestation, separate from metadata/legacy ledger
    -> MT / ZIP / distribution upload / public share / URL / Streamlit boundary

Dukascopy, one Dukascopy ancestor, UNKNOWN, missing or mismatched metadata -> BLOCK
```

## Files and responsibilities

| File | Responsibility |
|---|---|
| `fxtick/artifacts.py` | SHA-256/size checks, strict manifests, Parquet agreement, ancestry, explicit legacy registration |
| `fxtick/trusted_config.py` | Empty production source approvals and content attestations; never loaded from dataset metadata |
| `fxtick/query.py` | Carries SQL with all selected verified artifacts; plain SQL cannot authorize exports |
| `fxtick/fetcher.py` | Marks newly fetched frames; checks frame content before CSV; propagates chunk parents |
| `fxtick/duck.py` | Bound reads/normalization/unions/month filters; new Parquet output with embedded graph and sidecar |
| `fxtick/export_service.py` | Rechecks MT inputs, snapshots archive members, guards final ZIP and UI delivery |
| `fxtick/mt_export.py` | Guards all three public format writers; existing format/time conversion bodies retained |
| `fxtick/gdrive.py` | Zone/ancestor/ACL checks, immutable uploads, verified downloads, share/link rechecks |
| `register_legacy.py` | Explicit offline single-file owner registration CLI |
| `local_export.py` | Offline LOCAL_TEST CLI for registered old and newly managed tick inputs |
| `sync_and_upload.py` | Private acquisition/snapshot job; direct distribution helper and mail functions remain guarded |
| `app_cloud.py` | Distribution-only listing, download validation, explicit distribution conversions and guarded ZIP delivery |
| `.github/workflows/tick_sync.yml` | Private-reference sync only, separate root secrets, no distribution/mail inputs |
| `tests/phase2_fakes.py` | Explicit fake engines/Drive; not a substitute for real-format verification |
| `tests/test_phase2.py` | Offline boundary integration and denial side-effect assertions |
| `tests/test_phase2_real.py` | Deferred real DuckDB/PyArrow/NumPy, acquisition, Streamlit and encrypted ZIP tests |

`instruments.py` was inspected and is unchanged: all current instrument mappings
use Dukascopy identifiers. A symbol or broker name never grants redistribution.
The existing requirements files are unchanged.

## Owner registration: no automatic historical classification

The following are example commands, NOT commands executed on actual history:

```text
python -B register_legacy.py "D:\history\owner-selected.parquet" --ledger "D:\private-config\reference-ledger.json" --confirm-dukascopy-owner-reference
python -B local_export.py "D:\history\owner-selected.parquet" --ledger "D:\private-config\reference-ledger.json" --format mt5 --tz utc --output "D:\local-test\new-mt5.txt"
```

Registration requires one explicitly specified file and owner confirmation. No
directory scan, glob, filename inference, source-data rewrite or Drive access is
performed. The ledger records source DUKASCOPY, PRIVATE_REFERENCE, false
redistribution, LOCAL_TEST, SHA-256, size, registration time, schema version and a
Phase 1 provenance record. Its acquisition mechanism is `owner_attestation`; the
time records the attestation, not a newly asserted historical feed download time.

Re-registering the same unchanged file in the same ledger is idempotent. A changed
file is denied in both purposes; an explicit fresh registration into a separately
selected ledger is required. Old registration records are not silently upgraded.
Renamed legacy files require explicit registration of the selected path. Managed
files may be copied with their sidecars, but rights remain unchanged.

The ledger format is deliberately incapable of expressing commercial rights.
Changing its class, source, allowed use or redistribution flag invalidates it.
Registration does not write `trusted_config.py`. Unregistered old files are
UNKNOWN and denied in both LOCAL_TEST and DISTRIBUTION. A managed new input can be
selected alongside registered legacy inputs using the same CLI ledger argument.

The LOCAL_TEST CLI accepts normalized tick CSV/Parquet as conversion inputs.
Existing MT output files remain usable in their terminals unchanged and can be
registered for identity checks; the CLI does not invent an MT-to-ticks importer.

## Durable identity and trust

Managed artifacts have `<filename>.provenance.json`: schema version, SHA-256 of
the exact artifact bytes, file size, root provenance and complete parent records.
New Parquet also contains the same graph in `fxtick.lineage.v1`. Missing metadata,
duplicate JSON fields, inconsistent graphs, changed bytes and missing ancestors
fail closed. Parquet magic is checked in addition to extension so a rename does
not skip its footer validation. CSV/HST/MT text use the sidecar rather than adding
columns that would break importer formats. ZIPs include member manifests and have
their own manifest with inherited ancestry.

Distribution requires BOTH existing Phase 1 source-policy approval and a trusted
mapping from exact content hash to canonical lineage hash. No production feed or
historical artifact is approved. Tests use fictional sources only. Changing the
content registry alone cannot override Dukascopy/UNKNOWN/ancestor policy denial;
changing dataset metadata alone cannot obtain a matching trusted attestation.

After this process validates every actual approved input, its guarded MT/ZIP
builders may record an ephemeral attestation for the resulting artifact. Those
receipts live only in process memory. They do not persist source approvals or
survive restart; later publication needs reviewed trusted attestations again.
Arbitrary SQL and arbitrary Python supplied by untrusted users are not supported.

Local manifests/legacy ledgers are owner-operated records, not signed third-party
evidence of historical source truth. Someone controlling Python code, operator
configuration and all input bytes is outside this application boundary. Content
hashes alone cannot independently establish a source's identity or legal rights.

## Exact delivery gates

- **MT4/MT5/HST:** `guarded_conversion` on all public writers calls the existing
  policy through `require_query`, before formatting and again before publishing
  the new output. The default purpose is LOCAL_TEST. Both cloud callers specify
  DISTRIBUTION explicitly. No output may replace an existing path.
- **ZIP:** `build_zip` checks every actual member, archives verified snapshots in
  a private temporary directory, and rechecks before creating the final ZIP. A
  denied or changed input leaves no final ZIP at the requested path.
- **Drive upload:** `upload_file` checks content/policy before API writes, verifies
  zone ancestry, then uploads a verified snapshot and bound manifest. `replace=True`
  is refused. A same-name remote file causes refusal, not an update.
- **Drive download:** `download_file` verifies temporary bytes and metadata before
  copying to a new caller destination. It does not return an unverified artifact.
- **Public share:** `share_anyone_reader` re-downloads and validates actual content,
  graph, current source policies, attestation, zone and remote version before the
  anyone-reader permission write. A PRIVATE_REFERENCE zone claim is rejected.
- **URLs/email:** `distribution_url` repeats remote validation; `send_email` obtains
  its URL through this gate before SMTP. The summary's URL also uses this gate.
- **Streamlit:** listing filters actual verified files, not just names. Selected
  remote files are verified again; MT and ZIP use DISTRIBUTION; `streamlit_download`
  rechecks current artifact bytes/policy immediately before `download_button`.

## Google Drive and weekly sync

Explicit configuration is required:

- `GDRIVE_PRIVATE_REFERENCE_FOLDER_ID`
- `GDRIVE_DISTRIBUTION_FOLDER_ID`
- Existing `GDRIVE_TOKEN_JSON` (never put its value in source or logs)
- Streamlit additionally requires the existing `APP_PASSWORD`.

There is no fallback to `GDRIVE_FOLDER_ID`. Roots must be distinct. Traversal
rejects a destination outside its configured root or inside the opposite root,
cycles and ambiguous ancestry. Private folders and their ancestors must expose
only owner user permissions. Shared drives, other collaborators, unknown ACLs and
inherited public/domain access fail closed. Configure a separate private area;
the code does not remove anyone's existing Drive permissions.

The weekly job creates new monthly snapshots with timestamps/unique suffixes and
selects the latest managed snapshot to resume/merge. Filename parsing locates
candidates only; downloaded content still requires its managed manifest and
policy checks. Old monthly/annual files are neither migrated nor overwritten.
With a fresh private root and no managed snapshot, the existing seven-day initial
lookback applies. Historical backfill/migration is separate and never inferred.

Cron timing, symbol inputs and concurrency remain. TARGET_FORMAT is fixed to NONE
in the workflow; non-NONE requests to `main()` are rejected before credentials are
used. The private job cannot generate, clean up, share or email distribution ZIPs.
The old cleanup helper is not called. Existing exports are not deleted or revoked,
and generated URLs are not described as automatically expiring.

Private cloud retention is an engineering capability, **not a legal guarantee**.
Its legal applicability remains subject to review.

## Tests and exact commands

Declared dependencies: DuckDB `>=1.1,<2`, PyArrow `>=15`, NumPy `>=1.26`, Streamlit
`>=1.35`; Actions also uses dukascopy-python `>=4.0,<5`, pandas `>=2.1,<3`,
python-dateutil `>=2.8` and pyzipper `>=0.3.6`. No pyproject/setup.cfg was present.

Implementation environment interpreter:

```powershell
$phase2Python = 'C:/Users/kou/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
& $phase2Python -S -B -m unittest discover -s tests -p 'test_policy.py' -q
& $phase2Python -S -B -m unittest discover -s tests -p 'test_provenance.py' -q
& $phase2Python -S -B -m unittest discover -s tests -p 'test_phase2.py' -q
& $phase2Python -B -m unittest discover -s tests -p 'test_phase2_real.py' -v
& $phase2Python -S -B -m unittest discover -s tests -p 'test_*.py' -q
git diff --check
```

The original 61 Phase 1 tests remain unchanged. Phase 2 offline tests cover owner
confirmation/unregistered files, hashes, tampering, mixed ancestry, fake
Parquet/SQL propagation, direct MT writers, acquisition CSV chunk propagation,
ZIP rechecks, Drive upload/download/share, email URL and Streamlit delivery.
Denied operations assert no final output or delivery API write as applicable.

Verified results for this implementation: Phase 1 policy **43 PASS**, provenance
**18 PASS**; Phase 2 offline **63 PASS**. Total discovery **131 tests**, of which
**124 executed and passed**, **7 skipped/unexecuted**, **0 failures/errors**.
The expected `Dukascopy sync ... distribution is prohibited` error log comes from
the test asserting that the private job refuses a distribution request.

Seven real-library tests are explicitly skipped/unexecuted here: four real
conversion/Parquet tests, one mocked-feed test using real pandas/acquisition
libraries, one real Streamlit test, and one real pyzipper test. Standard-library
fakes do not validate the actual DuckDB SQL/PyArrow footer implementation.

In an existing environment with the declared dependencies, run (without `-S`):

```text
python -B -m unittest discover -s tests -p "test_phase2_real.py" -v
python -B -m unittest discover -s tests -p "test_*.py" -v
```

These tests still use synthetic data and mock external transport. No live
credentials or market data are needed. Do not count skips as passes.

## Self-review and Phase 3 carryover

Preserved: original Phase 1 logic/tests, five tick columns, UTC master basis,
timezone formatting bodies, MT4 tick CSV, MT5 millisecond text and HST formatting.
No native MT4 FXT or MT5 terminal/import behavior was newly implemented or tested.

Remaining work and risks:

1. Run deferred real-library tests, a full Streamlit application smoke test with
   fake Drive, and real terminal import checks before deployment.
2. Owner-directed historical inventory/registration and non-destructive migration,
   including rows, min/max UTC, price checks and checksums; no actual registration
   was performed in Phase 2.
3. Legal/source review, private-cloud applicability and protected management of
   production source approvals/attestations. Defaults permit no distribution.
4. Validate real Drive ACL/ancestor/version/API behavior with separate authorized
   synthetic test storage before enabling cron. Existing secrets were not changed.
5. A concurrent external Drive administrator can change permissions/content after
   a check; Drive writes and checks are not one atomic transaction. No code can
   revoke bytes already delivered through a browser download. Access control of
   the host/configuration/Drive account remains part of the trust boundary.
6. Interrupted uploads can leave an orphan manifest/new snapshot; no destructive
   rollback or cleanup of existing files is attempted. Failed local I/O may leave
   an incomplete new artifact, which has no valid manifest and fails closed.
7. Immutable snapshots retain old versions and increase storage. Full hash checks,
   footer reads, temporary snapshots and listing-time validation add I/O. Benchmark
   long histories and design audited retention/indexing in a later phase.
8. The legacy ledger is an owner-managed file, not a concurrent multi-writer
   database. Serialize registration operations and keep private backups.
