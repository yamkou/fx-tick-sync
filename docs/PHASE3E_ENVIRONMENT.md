# Phase 3E — isolated real-library verification

## Environment and dependency decision

Verified on Windows AMD64 with CPython 3.12.14 and pip 25.0.1.
Repository environment: `C:\Users\kou\Documents\GitHub\fx-tick-sync\.venv`.
No previous .venv existed; the new environment has
`include-system-site-packages = false` and is Git-ignored. Existing/global Python
was not modified. Its pandas remains 3.0.1; its NumPy remains 2.3.5 and dateutil
2.9.0.post0. The other audited direct dependencies were absent before setup.

Existing runtime ranges remain unchanged:

- `requirements-core.txt`: portable DuckDB/PyArrow/NumPy processing.
- `requirements-monitor.txt`: Waitress monitor backend.
- `requirements.txt`: Streamlit/Google application dependencies.
- `requirements-actions.txt`: reference acquisition and archive dependencies,
  including `pandas>=2.1,<3` and `dukascopy-python>=4,<5`.
- New `requirements-dev.txt`: includes all four for full unittest integration.
- New `constraints-windows-py312.txt`: all 58 resolved package versions for this
  Windows/Python 3.12 verification environment.

No pyproject.toml/setup.cfg or alternative packaging definition was found.
The pip dry-run resolved the combined existing ranges without changing requirements.
Only binary distributions from the explicit public PyPI index were requested.
The initial sandbox network attempt failed; the approved escalated package-only
request succeeded. The subsequent install used the resolved constraints.

| Direct package | Installed |
|---|---|
| duckdb | 1.5.5 |
| pyarrow | 25.0.1 |
| streamlit | 1.63.0 |
| dukascopy-python | 4.0.1 |
| pyzipper | 0.4.0 |
| waitress | 3.0.2 |
| pandas | 2.3.3 |
| numpy | 2.5.2 |
| python-dateutil | 2.9.0.post0 |
| google-api-python-client | 2.200.0 |
| google-auth | 2.57.1 |
| google-auth-httplib2 | 0.4.2 |

The pandas conflict is resolved solely inside the isolated environment: 2.3.3 meets
the existing `<3` requirement. No global downgrade was performed. Actual
`python -m pip freeze` output is [PHASE3E_FREEZE.txt](PHASE3E_FREEZE.txt), containing
all 58 installed application/transitive packages. `pip check` reported no broken
requirements. Pip itself is environment tooling and excluded from normal freeze.

## Verification

Commands run from the repository:

```powershell
& ./.venv/Scripts/python.exe -m pip check
& ./.venv/Scripts/python.exe -B tests/run_offline.py test_phase2_real.py
& ./.venv/Scripts/python.exe -B tests/run_offline.py test_phase3e_waitress.py
& ./.venv/Scripts/python.exe -B tests/run_offline.py
```

`run_offline.py` disables Streamlit usage statistics and rejects Python socket
connect/bind/sendto/DNS operations outside loopback. This is an additional test
guard, not an OS firewall for native extensions or child processes. The existing
crash children only use synthetic local SQLite fixtures. Acquisition and Google
transports remain mocked. No real Dukascopy, Drive, LINE, Email, broker, VPS/RDP or
production notification endpoint was accessed. Network package download and the
authorized Git push are separate from application tests.

The original 373 Phase 1–3D cases are retained; Phase 3E repeats the ten HTTP
contract cases against real Waitress rather than replacing the stdlib server tests.
All 383 cases PASS, 0 FAIL, 0 SKIP. Phase 1's 61 cases remain successful.

| Formerly skipped method | Original direct blocker | Result |
|---|---|---|
| test_csv_parquet_mt4_mt5_hst_local_roundtrip | DuckDB/PyArrow | PASS |
| test_legacy_parquet_registration_does_not_edit_footer | DuckDB/PyArrow | PASS |
| test_real_month_merge_deduplicates_and_retains_lineage | DuckDB/PyArrow | PASS |
| test_real_parquet_footer_sidecar_disagreement | DuckDB/PyArrow | PASS |
| test_mocked_feed_marks_frame_and_csv | dukascopy-python; global pandas also outside range | PASS |
| test_real_streamlit_download_boundary_denies_duka | Streamlit/DuckDB/PyArrow | PASS |
| test_local_encrypted_zip_retains_denial | pyzipper | PASS |

Real Parquet footer/sidecar mismatch rejection, inherited LOCAL_TEST allowance and
DISTRIBUTION denial, legacy bytes preservation, MT4/MT5/HST generation, Streamlit
download denial and encrypted ZIP are exercised using temporary synthetic data.
Streamlit AppTest emitted its expected bare-mode ScriptRunContext warning; it is
not a test failure. The real Streamlit application was not publicly deployed.

Waitress binds **127.0.0.1 only, ephemeral port**, verified from its socket. The
production HeartbeatApplication and MonitorRuntime process CollectorManager receipts,
authenticate HMAC, persist SQLite and deliver fake notifications. Ten cases cover
valid receipt, invalid HMAC, expiry, sequence replay, nonce reuse, unknown identity,
malformed/oversized payload, failure/recovery transitions, provider failure and
SQLite lock/restart. No HTTPS/proxy, public interface or real notification is claimed.

## Rebuild on a new Windows VPS (setup only)

Select an approved CPython 3.12.14 AMD64 executable. This does not register a service
or connect to a broker. Run from a checkout of the reviewed revision:

```powershell
$Phase3EPython = 'C:\Path\To\Python312\python.exe'
if (Test-Path -LiteralPath .venv) { throw 'Inspect existing .venv; do not recreate it.' }
& $Phase3EPython -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
& ./.venv/Scripts/python.exe -m pip --isolated --disable-pip-version-check install --only-binary=:all: --index-url https://pypi.org/simple -r requirements-dev.txt -c constraints-windows-py312.txt
if ($LASTEXITCODE -ne 0) { throw 'install failed' }
& ./.venv/Scripts/python.exe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'dependency check failed' }
& ./.venv/Scripts/python.exe -B tests/run_offline.py
if ($LASTEXITCODE -ne 0) { throw 'verification failed' }
& ./.venv/Scripts/python.exe -m pip freeze
```

Inspect SKIP counts too: unittest's success exit status alone does not certify all
real dependencies were exercised. For a minimal monitor host substitute
`-r requirements-monitor.txt`; for portable core use `-r requirements-core.txt`.
Constraints restrict selected packages; they do not install the whole dev stack.
Do not provision secrets as part of this setup. Pinning versions is not a hash-based
supply-chain lock; reviewed wheel hashes/offline wheelhouse retention remain future
hardening work. Package index availability is required unless an approved wheelhouse
is supplied using `--no-index --find-links` instead of `--index-url`.

## Mac / Apple Silicon and production gates

Runtime requirements retain portable ranges; no MetaTrader5, pywin32 or Windows
service dependency was added to Core. The Windows constraints are explicitly
platform-scoped and are not an Apple Silicon compatibility claim. On a Mac, create
a native Python 3.12 venv, resolve `requirements-core.txt` against native wheels,
run applicable tests, and record a separate reviewed constraint set. Do not force
Windows wheel files or copy the Windows venv. Full Mac/ARM integration is untested.

The seven missing-library gates from Phase 3D are closed, and local Waitress ingress
is verified. Production blockers still include real Collector/probe/writer startup,
durable sender boot/sequence handling, real MT5 session behavior, TLS/reverse proxy,
service permissions/shutdown, independent uptime delivery, approved secret setup,
real gateway deduplication, restore operations and 24-hour/7-day soak tests. Follow
the still-unchecked [real deployment checklist](PHASE3D_READINESS.md) before Phase 4
VPS rollout. Monitoring readiness does not grant redistribution rights.

Existing runtime, provenance/policy, legacy registration, distribution boundaries,
GitHub Actions and historical data were not changed. No main merge was performed.
