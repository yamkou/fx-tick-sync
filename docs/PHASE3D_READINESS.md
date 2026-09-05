# Phase 3D — offline verification and production gates

2026-09-06: offline verification is complete; this is **not production sign-off**.
Existing Phase 1–3C runtime, tests, requirements and historical data are unchanged.
No packages, real credentials, services, VPS tasks or real notifications were installed
or provisioned. Network tests used numeric 127.0.0.1 only; git pushes were the only
authorized external operations.

## Test evidence

Python 3.12.14, Windows, normal site packages enabled (no `-S`):

```powershell
& 'C:/Users/kou/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -B -m unittest discover -s tests -p 'test_*.py' -q
```

| Scope | PASS | FAIL | SKIP |
|---|---:|---:|---:|
| Phase 1 | 61 | 0 | 0 |
| Phase 2 | 63 | 0 | 7 |
| Phase 3A | 70 | 0 | 0 |
| Phase 3B | 87 | 0 | 0 |
| Phase 3C | 63 | 0 | 0 |
| Phase 3D | 22 | 0 | 0 |
| Total (373 discovered) | 366 | 0 | 7 |

Baseline before Phase 3D: 351 discovered, 344 PASS, 0 FAIL, 7 SKIP.
The seven skipped cases in `tests/test_phase2_real.py` remain **unexecuted**:

| Test method | Missing dependency |
|---|---|
| test_csv_parquet_mt4_mt5_hst_local_roundtrip | DuckDB, PyArrow |
| test_legacy_parquet_registration_does_not_edit_footer | DuckDB, PyArrow |
| test_real_month_merge_deduplicates_and_retains_lineage | DuckDB, PyArrow |
| test_real_parquet_footer_sidecar_disagreement | DuckDB, PyArrow |
| test_mocked_feed_marks_frame_and_csv | dukascopy-python; installed pandas also outside requirement |
| test_real_streamlit_download_boundary_denies_duka | Streamlit, DuckDB, PyArrow |
| test_local_encrypted_zip_retains_denial | pyzipper |

NumPy is available even though some generic skip messages mention it. Waitress/TLS
acceptance has no optional unittest case here and is separately unverified, not an
eighth skipped test. Tests keep acquisition and Drive transports fake after packages
are installed; package availability is not permission to access those services.

## HTTP, crash and failure evidence

`test_phase3d_http.py` sends real TCP requests through a test-only stdlib WSGI server
to the production application/worker/authenticator/SQLite path and fake notification
sink. CollectorManager uses a fake probe and loopback transport. Valid signed requests
return 202 after persistence; invalid HMAC, expired timestamps, sequence replay,
nonce reuse with a newer sequence, and unknown collector return 403 without advancing
receipt state. Malformed JSON returns 400; oversized body returns 413.
This validates application integration, **not Waitress, HTTPS or reverse proxy parsing**.

| Injection | Observed behavior |
|---|---|
| Missing heartbeat | WARNING at 120 seconds, CRITICAL at 180, then one RECOVERY after fresh receipt |
| Collector reports stopped | CRITICAL, then RECOVERY after healthy receipt |
| Tick age 181 seconds / write age 301 seconds | WARNING, then RECOVERY after fresh values |
| Repeated evaluation | No repeated transition delivery within suppression window |
| Notification provider failure | Pending event retained, health 503/degraded, cooldown retry succeeds |
| SQLite exclusive lock beyond busy timeout | Worker fails closed, health 503; release and explicit restart restore receipt state |
| Monitor child killed after committed CRITICAL | Heartbeat and CRITICAL restored; no immediate duplicate transition |
| Child killed in uncommitted SQLite transaction | Partial sequence update rolled back |
| Backup and separate-path restore | Latest heartbeat and recovered state restored; no duplicate recovery |
| Corrupt scratch database | Open rejected; corrupt bytes preserved |

SQLite failure cannot safely persist new collector alerts; it does not fabricate a
collector RECOVERY. A separate uptime node must detect the failed/unreachable monitor.
Automatic worker restart after DB failure is not implemented.

The crash fixture kills only a test-created child process with a marked temporary
directory. It pauses after the fake downstream commits delivery but before monitor
acknowledgment. Restart resends the **same event ID**: an idempotent sink records two
attempts and one effect; a non-idempotent sink records two attempts and two effects.
Existing outbox semantics are at-least-once, not exactly-once. Production gateway
deduplication must scope event ID by route/recipient and retain it across restarts.
LINE/Email transport interfaces alone do not guarantee receiver idempotency.

## Short multi-collector measurement

Raw evidence: [PHASE3D_LOAD_RESULTS.json](PHASE3D_LOAD_RESULTS.json).
Each run uses signed receipts, real temporary SQLite, three healthy cycles, a
critical interval, recovery and a restart. This measures direct receive processing,
not concurrent HTTP capacity. Notification delivery is fake.

| Collectors | Receipts | Critical / recovery | Peak / final queue | Wall s | CPU s | Peak Python heap bytes | DB bytes |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10 | 40 | 10 / 10 | 10 / 0 | 0.754622 | 0.046875 | 130940 | 86016 |
| 20 | 80 | 20 / 20 | 20 / 0 | 1.394717 | 0.265625 | 145634 | 126976 |
| 50 | 200 | 50 / 50 | 50 / 0 | 3.512075 | 0.578125 | 268699 | 245760 |

Healthy-cycle heap samples (bytes): 10 nodes 51128/54327/54346; 20 nodes
36261/36475/33148; 50 nodes 44455/52199/54401. These short samples are not proof of
leak-free operation. `tracemalloc` measures Python allocations, not process RSS;
CPU is process CPU time, not a utilization percentage. History grows SQLite state.
Retention, capacity limits, real HTTP concurrency and 24-hour/7-day soak tests remain.
To repeat into a **new** report path:

```powershell
& $Phase3DVenvPython -B tests/phase3d_load.py --output <new-report-path.json>
```

## Dependency audit and later single-approval environment setup

No pyproject.toml/setup.cfg was found. Shared direct ranges in requirements.txt,
requirements-actions.txt, requirements-core.txt and requirements-monitor.txt agree.
Transitive resolution and platform wheel availability have not been verified.

| Package | Declared range | Current environment |
|---|---|---|
| duckdb | >=1.1,<2 | missing |
| pyarrow | >=15 | missing |
| numpy | >=1.26 | 2.3.5, meets range |
| streamlit | >=1.35 | missing |
| waitress | >=3,<4 | missing |
| pyzipper | >=0.3.6 | missing |
| dukascopy-python | >=4.0,<5 | missing |
| pandas | >=2.1,<3 | 3.0.1, outside range |
| python-dateutil | >=2.8 | 2.9.0.post0, meets range |
| google-api-python-client | >=2.120 | missing |
| google-auth | >=2.29 | missing |
| google-auth-httplib2 | >=0.2 | missing |

The following complete PowerShell block is for **later approval only**, not executed
in Phase 3D. It creates a new dedicated venv outside Git, uses its explicit interpreter,
and leaves the existing/global environment unchanged. Approval of package download
does not approve real credentials, service registration or notification delivery.

```powershell
$Phase3DRepo = 'C:\Users\kou\Documents\GitHub\fx-tick-sync'
$Phase3DBasePython = 'C:\Users\kou\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$Phase3DVenvRoot = Join-Path $env:LOCALAPPDATA 'fx-tick-sync\venvs\phase3d'
if (Test-Path -LiteralPath $Phase3DVenvRoot) { throw 'Choose a new dedicated venv path; do not overwrite.' }
& $Phase3DBasePython -m venv $Phase3DVenvRoot
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
$Phase3DVenvPython = Join-Path $Phase3DVenvRoot 'Scripts\python.exe'
& $Phase3DVenvPython -m pip --isolated install --index-url https://pypi.org/simple -r "$Phase3DRepo\requirements.txt" -r "$Phase3DRepo\requirements-actions.txt" -r "$Phase3DRepo\requirements-core.txt" -r "$Phase3DRepo\requirements-monitor.txt"
if ($LASTEXITCODE -ne 0) { throw 'dependency installation failed' }
& $Phase3DVenvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'dependency consistency failed' }
Set-Location -LiteralPath $Phase3DRepo
& $Phase3DVenvPython -B -m unittest discover -s tests -p 'test_*.py' -v
if ($LASTEXITCODE -ne 0) { throw 'tests failed; retain environment for diagnosis' }
& $Phase3DVenvPython -m pip freeze --all | Set-Content -LiteralPath (Join-Path $Phase3DVenvRoot 'resolved-requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'dependency inventory failed' }
```

Inspect unittest SKIP lines even when exit code is zero. For an approved offline
wheelhouse, replace `--index-url https://pypi.org/simple` with
`--no-index --find-links '<approved-wheelhouse>'`; no wheelhouse is selected now.
Do not use both installation alternatives. Review the frozen inventory before any
later lockfile commit; it is an observation, not an automatically approved lock.

## Deployment and remaining gates

[Deployment handoff](../deployment/README.md) supplies Windows Collector and separate
Tokyo Monitor layouts, JSON and empty environment templates, config-only commands,
startup/shutdown candidates, log/state/backup paths and third-node uptime guidance.
Both supplied config-only commands passed without starting a runtime.

Production blockers: reviewed real Collector entry point/probes/writer integration;
durable sender sequence and authorized boot restart handling; compatible venv;
Waitress/TLS/proxy validation; actual service session/permissions and graceful shutdown;
approved secret provisioning; concrete LINE/Email transports or reviewed webhook
gateway with persistent dedup; independent uptime delivery; backup/restore operations,
log rotation and state retention. They are not solved by the plan-only collector CLI.
Cross-platform deployment and real MT5/cTrader behavior have not been tested here.

All following real-environment acceptance items remain unchecked:

- [ ] Windows VPS boot
- [ ] Collector automatic startup
- [ ] MT5 startup and session availability
- [ ] Collector heartbeat send/receiver acknowledgment
- [ ] Independent Monitor receipt persistence
- [ ] Smartphone notification delivery and readable format
- [ ] Selected Collector forced termination
- [ ] Selected MT5 forced termination
- [ ] Approved VPS forced stop
- [ ] Heartbeat timeout at configured thresholds
- [ ] CRITICAL notification, dedup and reminder timing
- [ ] VPS recovery with valid boot/sequence
- [ ] RECOVERY notification and correct downtime
- [ ] SQLite restart recovery and separate-path backup restore
- [ ] Monitor restart with outbox restoration
- [ ] Monitor death detected by independent uptime checker
- [ ] 24-hour continuous run with CPU/RSS/DB/queue measurements
- [ ] 7-day continuous run and retention/backup review
- [ ] HTTPS/proxy request limits, authentication/rotation/replay in staging
- [ ] Gateway duplicate handling after remote acceptance and local crash

Before Phase 4: first approve the dedicated dependency environment and close all
seven skipped tests; then review real collector/sender integration and stage on
separate hosts with independent uptime monitoring. Provision secrets and test real
notifications only after destinations are selected and approved. Do not declare
production readiness until the checklist and restore drill are complete.
Phase 1/2 PRIVATE_REFERENCE, LOCAL_TEST, provenance/legacy ledger and MT/ZIP/Drive/
Streamlit distribution boundaries remain unchanged. Monitoring provides no broker
redistribution rights and does not authorize Dukascopy distribution.
