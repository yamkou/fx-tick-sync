# Phase 3A — Cross-platform Core / Deployable Collector Foundation

## Implemented scope

Phase 3A adds portable non-secret configuration, collector/terminal identities,
content-bound acquisition observations, health and external-monitor interfaces,
notification contracts, a read-only deployment plan CLI and optional configuration
for the existing LOCAL_TEST CLI. No collector loop, MT5 terminal launcher, service,
watchdog, network listener, notifier or broker integration was started/implemented.

Phase 1/2 provenance schemas, policy, legacy ledger, MT/ZIP/Drive/Streamlit guards,
weekly workflow and their tests are unchanged. Existing history is untouched.
No acquisition, Drive operation, registration, data transfer, notification API or
package installation was performed. Authorized development-branch Git pushes are
the only external operations performed for this phase.

## Architecture and platform audit

```text
Windows VPS London-01                    Windows VPS London-02
  Collector london-01                    Collector london-02
  broker-a MT5 #1..5 (future adapter)      broker-b MT5 #6..10 (future adapter)
  cTrader/Python collectors (future)      cTrader/Python collectors (future)
  lightweight probes / short buffer      lightweight probes / short buffer
           |                                      |
           +----- storage / transfer (future) -----+
                                |
             Windows / Apple Silicon macOS / Linux Core
             config, identities, provenance, policy, artifact validation
             DuckDB, PyArrow, Parquet, format conversion
             future heavy QA / broker comparison / backtest / reports

Independent external monitor (e.g. tokyo-monitor-01, separate failure domain)
  expected collector registry + authenticated heartbeat receipts (future)
  receiver clock + persistent incident and per-route delivery state (future)
  NotificationProvider -> LINE / Push / Email adapters (future)
```

Inspection found no fixed `C:\Users\...`, Administrator, Desktop or Documents
paths in execution code. Existing concrete Windows paths are documentation
examples. Existing `Path`, `tempfile`, `os.replace`, `fsync` and binary MT format
writing are not inherently Windows-only. No wholesale module relocation was
necessary. Existing modules remain the processing Core; adding a `core/` wrapper
would provide no new isolation.

New `fxtick/platform/windows.py` contains only a guarded native path resolver and
`TerminalAdapter` protocol. Core does not import this adapter. It imports no
MetaTrader5/win32 module, starts no process and accesses no account. Actual MT5
Python integration and Windows service/task control must remain behind that
boundary in later phases. Existing CSV/HST format writers remain portable.

`requirements-core.txt` contains the existing DuckDB/PyArrow/NumPy ranges only.
Existing Actions/Cloud requirements are preserved. Core models use only the
standard library. SQL/Parquet/format operations still require their actual
libraries; mocks are not a replacement for deploying those dependencies.

QA, Python backtesting and report engines are architectural consumers, not new
implemented engines in this phase. Apple Silicon compatibility is a design target;
native hardware/OS validation remains outstanding.

## Configuration and path rules

`fxtick/config.py` loads strict JSON schema version 1. Examples:

- `configs/collector.example.json`: Dukascopy private-reference profile.
- `configs/windows-vps.example.json`: ten fictional terminals, five per collector.
- `configs/monitoring.example.json`: monitoring thresholds only, no destinations.

Deployment fields:

| Section | Fields |
|---|---|
| deployment | schema_version, environment |
| paths | data_root, temp_root, log_root, export_root, provenance_registry |
| collectors | collector_id, location, source_type, broker, symbols, storage_destination |
| terminals | terminal_id, collector_id, broker, path |
| storage | storage_id, kind, zone, location |

Environments: development, testing, staging, production. Source types: dukascopy,
ctrader, mt5, local. Unknown values, missing or extra fields, duplicate JSON keys,
duplicate IDs and malformed registry entries fail. No environment or source type
grants licence approval. `DISTRIBUTION` in a storage declaration is a requested
destination, never an authorization; actual delivery still needs Phase 2 checks.
An unreviewed broker stays UNKNOWN even if a collector record exists.

All relative filesystem paths are anchored to the configuration directory, not
the current shell directory. Examples use portable relative paths. Actual `Path`
resolution is host-native. `resolve_path(value, PureWindowsPath(...))` and its
PurePosixPath counterpart allow read-only target-platform inspection. A Windows
absolute path on macOS/Linux, or a POSIX absolute path on Windows, is rejected for
native use instead of silently becoming an incorrectly named relative directory.
A migration changes the path settings; there is no guessed drive-letter mapping.

Drive-relative/root-relative Windows paths, device paths, traversal, glob/env/home
expansion, reserved Windows components and URL credentials in filesystem paths
are rejected. Operational data/temp/log/export roots must be disjoint, including
nesting, and the legacy ledger must sit outside all four. Loading/validation does
not create directories or test write permissions. Health observations will carry
accessibility/free-space information from future explicit probes.

The plan namespaces operational roots by collector ID, e.g.
`runtime/data/london-01`. The local analysis CLI uses the configured root directly
and can select `london-01/ticks.csv`. This separates concurrent collector buffers
without putting an OS hostname, machine ID or absolute path in data identity.

`storage.kind=local` declares a path; `gdrive` uses a logical destination alias.
Storage transfer adapters and alias resolution are not implemented by this
configuration loader. Existing private/distribution Drive-root configuration and
ACL/provenance checks remain authoritative for the existing weekly job.

## Collector and terminal naming

IDs use 1–63 lowercase ASCII letters/digits with single hyphens, starting with a
letter. Examples: `london-01`, `frankfurt-01`, `tokyo-monitor-01`, `broker-a-01`.
Keep collector IDs stable when moving that collector to a new VPS. Do not derive
them from a cloud instance ID, hostname, user home or executable path.

The deployment registry rejects duplicate collector IDs and terminal IDs. A
terminal references an existing MT5 collector with the same broker; a registered
MT5 collector needs at least one terminal. A collector is one source/broker
acquisition identity, not necessarily an entire physical VPS. To run several
brokers or source types on one VPS, assign several collector IDs on that host.
The same broker may have several terminal IDs. There is no arbitrary hard limit
of ten; the included sample validates the intended ten-terminal arrangement.

Do not run old and replacement VPS instances simultaneously under the same
collector ID. A config file can detect duplicates within itself, not across
independently running deployments. Fleet-wide ownership/leases are Phase 3B work.

## Provenance compatibility

`AcquisitionRecord` lives beside provenance, not inside provenance schema v1.
It links dataset ID, SHA-256 of content and canonical lineage with collector ID,
location, environment, source, broker, selected symbol and UTC acquisition time.
It contains no filesystem/machine paths. Its source/provider/time must agree with
the actual raw artifact and selected collector. A derived artifact cannot be
relabeled as a fresh acquisition. Records are written only to a new explicitly
chosen file; this work did not write one for real data.

Phase 2 transformations already retain ancestor dataset IDs; later catalog/QA
code can join these acquisition observations through those IDs. This does not
require rewriting old Parquet footers or manifests. The observation is metadata,
not source approval or cryptographic proof of who operated a collector. It cannot
upgrade Dukascopy or UNKNOWN, bypass a content mismatch, or publish anything.
Automatic creation/cataloging of observations by live collectors is Phase 3B.

Legacy registration remains owner-explicit and path-specific. A VPS relocation
does not silently rewrite the old ledger or reclassify copied history. Existing
files and ledger must be preserved; separately authorized registration/migration
can establish new paths later.

## Commands and VPS migration

Read-only plan validation (these example profiles were validated locally):

```text
python -S -B collector_plan.py --config configs/collector.example.json --collector london-01
python -S -B collector_plan.py --config configs/windows-vps.example.json --collector london-02
```

Output explicitly says `mode=plan-only` and `runtime_implemented=false`. It is not
a collector start command and cannot fetch, publish, start MT5 or notify.

Optional existing LOCAL_TEST CLI configuration, for a future owner-selected input:

```text
python -B local_export.py --config configs/collector.example.json london-01/ticks.csv --format mt5 --tz utc --output new-mt5.txt
```

Here relative inputs use data_root, relative output uses export_root, DuckDB uses
temp_root, and an existing configured ledger is used unless explicitly overridden.
Configured output cannot escape export_root. Missing old registration still fails
closed. Without `--config`, the previous CLI argument/path behavior is preserved.
Neither the command above nor any real-data conversion was executed in Phase 3A.

New VPS recovery procedure:

1. Clone/check out a reviewed revision; retain the old VPS and its data intact.
2. Prepare an existing approved Python environment (3.12 recommended). Core uses
   requirements-core; the existing private sync uses requirements-actions. Install
   from a separately authorized source/offline wheels; no installs were done here.
3. Place a non-secret deployment configuration and change roots/location as needed.
4. Place authorized MT5 terminal installations at configured Windows paths. Keep
   logical terminal and broker mappings; do not launch them in this phase.
5. Provision secrets separately through the existing environment/secret-management
   mechanism. Do not put tokens, passwords or account IDs in JSON or Git.
6. Run tests and the plan-only CLI, inspect selected roots and IDs, and stop the
   old collector before handing identity to the replacement. Actual collector
   startup/service installation becomes available only after its later adapter.

Core migration to a Mac/Linux host follows the same config/test steps, omitting
Windows terminal setup. Existing history needs explicit separate transfer and
registration authorization; these steps do not perform it automatically.

## Phase 3B monitoring contract

`HealthSnapshot` can express collector alive, UTC last_tick_time and
last_successful_write, disk accessibility/free bytes, source connection, stable
error code and per-terminal process-alive states. Missing observations are `None`
(UNKNOWN), never silently healthy. Error fields accept codes rather than raw
credential-bearing exceptions. Schema-1 serialization preserves immutable models.

`HeartbeatReceipt` is owned by the external monitor: monitor ID, receiver timestamp,
snapshot, boot ID and monotonic sequence. Sender observation time must not serve
as the timeout clock. Sender/receiver clocks may differ. A separate monitor can
detect a stopped VPS by missing receipts even when the VPS sends nothing.

Phase 3B implementation should:

1. Maintain an expected collector inventory independent of received heartbeats,
   including startup grace for nodes that have never reported.
2. Authenticate receipts, identify boot epochs, reject replay/out-of-order sequence
   numbers and store receiver-owned timestamps durably outside the monitored VPS.
3. Evaluate heartbeat timeout externally (example >180 seconds). Probe MT5 process,
   cTrader connection, tick/write age, disk capacity and collector error state on
   appropriate nodes. Apply market-hours/maintenance rules to tick-age alarms.
4. Open an incident keyed by collector/check/terminal. Emit CRITICAL once, then
   suppress repeats until a configured cooldown or meaningful severity change.
5. Persist incident first_seen, notification and recovery times. After recovery,
   emit one RECOVERY event with outage duration (502 seconds = 8 min 22 sec), then
   close that incident. A subsequent outage starts a new incident epoch.
6. Use a stable event ID and per-route delivery state. Only confirmed sends advance
   successful-send/cooldown state. LINE success must not suppress a failed Email
   route; retries, backoff and idempotency need durable delivery state.

Prepared contracts: `HeartbeatStore`, `IncidentStore`, `NotificationDeliveryStore`,
`NotificationProvider`, incident/event/delivery models and `MonitoringPolicy`.
Prepared severity classes: INFO, WARNING, CRITICAL, RECOVERY. Route channels:
LINE, Push, Email. Route IDs are logical only. Provider-specific recipients, tokens
and transport configuration stay outside Core and ordinary JSON. `SecretProvider`
is a protocol only; it reads no environment variables and stores no secrets.

No timeout evaluator, dedup algorithm, persistent store, retry loop, network
transport, LINE integration, SMTP notification or OS process probe is running or
implemented here. These are explicit Phase 3B tasks, not tested production features.
The external monitor itself should have an independent failure domain and its own
monitoring; this foundation does not provision either host.

## Verification and remaining work

Offline verification: Phase 1 61 PASS, Phase 2 63 PASS, Phase 3A 70 PASS;
194 PASS total, with 7 real-library tests explicitly skipped (201 discovered).
The real-library test module was also run without `-S`; all seven remained
unexecuted due to unavailable dependencies, rather than disabled site loading.

The original Phase 1 **61** and Phase 2 offline **63** tests are unchanged. Phase
3A tests cover config validation, both path grammars, roots, duplicate IDs, ten
terminals, provenance associations, unchanged policy, Health serialization, remote
receipt timestamps, notification contracts, per-route delivery state and guarded
platform imports. The platform-import tests simulate `darwin`/`linux` in an isolated
Python subprocess and block Windows/third-party modules; they are not native OS runs.

Run with the selected existing interpreter:

```text
python -S -B -m unittest discover -s tests -p "test_phase3a*.py" -q
python -S -B -m unittest discover -s tests -p "test_*.py" -q
python -B -m unittest discover -s tests -p "test_phase2_real.py" -v
git diff --check
```

Phase 2's seven real-library tests remain unexecuted because required libraries
are absent. Also outstanding: native Apple Silicon/Linux runs, real MT5 adapter
behavior, live collector deployment and all external monitoring/provider behavior.
No such success is inferred from mocks or model tests. Prior Phase 2 risks and
legal/source review requirements remain applicable.

Recommended Phase 3B order: real-library/native-OS test matrix; collector lifecycle
and broker adapter with explicit provenance emission; non-destructive buffer and
transfer recovery; external authenticated heartbeat service; actual probes and
timeout rules; durable incident/per-route delivery stores; notification adapters;
then separately approved deployment/secret provisioning and synthetic live tests.
