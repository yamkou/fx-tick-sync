# Phase 4B-0 — runnable synthetic staging Collector

The formal entry point is **`python -m fxtick.collector`**. It wires the existing
staging identity/terminal registry, HealthSnapshot, CollectorManager,
SignedHeartbeatTransport, HMAC and independent Monitor. It is runnable with an
explicit fake source. No MT5/cTrader connection, VPS/RDP session or real credential
registration was performed. The real-source adapter gate remains; the generic
runtime/sender integration gate is now closed for synthetic staging.

## Configuration and commands

Keep the Phase 4A `<root>/app`, `config`, `data`, `logs`, `state`, `temp`, `terminals`,
`backup`, `exports` layout. Bootstrap now exclusively creates
`config/runtime.staging.json` from `deployment/windows-staging/runtime.template.json`
when absent, and invokes the formal dry-run. Existing runtime files are never
overwritten. Bootstrap's PowerShell syntax was checked; script execution remains
unverified because the local execution policy refused it in Phase 4A. No policy
change or bypass was made.

For an existing initialized staging root, create the runtime config once:

```powershell
Set-Location -LiteralPath C:\fx-tick-sync\app
[IO.File]::Copy((Join-Path (Get-Location) 'deployment/windows-staging/runtime.template.json'), 'C:\fx-tick-sync\config\runtime.staging.json', $false)
```

The file contains only schema version, `adapter=fake`, public boot/key IDs and
heartbeat interval (1–60 seconds; default 60). Unknown fields, duplicate keys,
non-fake adapters and invalid intervals are rejected. The deployment config still
requires staging, one terminal/collector and local QUARANTINE. Broker remains
configurable. Fake status must never be interpreted as a real broker observation.

First command on the future VPS, after reviewed clone/bootstrap, without secrets:

```powershell
& ./.venv/Scripts/python.exe -B -m fxtick.collector --config C:\fx-tick-sync\config\collector.staging.json --runtime C:\fx-tick-sync\config\runtime.staging.json --dry-run
```

Dry-run validates both configs, logical identity, registry and path containment;
initializes unknown health and constructs/decodes a heartbeat without signing it.
It does not create state/logs, open sockets, call source adapters, inspect secret
values, register tasks, or log in to MT5. Sequence zero in this local validation
envelope is never sent.

After separate private secret setup and Monitor enrollment:

```powershell
& ./.venv/Scripts/python.exe -B -m fxtick.preflight --config C:\fx-tick-sync\config\collector.staging.json --runtime C:\fx-tick-sync\config\runtime.staging.json
& ./.venv/Scripts/python.exe -B -m fxtick.collector --config C:\fx-tick-sync\config\collector.staging.json --runtime C:\fx-tick-sync\config\runtime.staging.json
```

The second command is the unique foreground start command after a successful
preflight. It runs until stop; it is **not** executed against an external endpoint
during development. Missing secrets or invalid HTTPS endpoint fail before runtime
state opens. Existing Phase 4A preflight calls without `--runtime` retain their
conservative real-source blocker for compatibility. With `--runtime`, readiness is
scoped to `fake-staging-only`; actual MT5 presence is not applicable and
`real_source_ready=false` is explicit. An invalid runtime config never passes.
Preflight cannot prove remote key enrollment or receipt; outbound checking is still
an explicit, separately approved `--check-network` operation. It also does not
reserve the sender lease; a racing second process can correctly fail at startup.

## Sender and Monitor contract

The Collector reads `FX_STAGING_HMAC` and `FX_STAGING_MONITOR_ENDPOINT` through the
existing EnvironmentSecrets interface. HMAC key material requires at least 32 UTF-8
bytes. The endpoint must be HTTPS `/v1/heartbeat`, without userinfo/query/fragment.
No values are stored in runtime JSON, SQLite, logs or Git. Do not paste credentials
in chat, shell command lines/history, task XML or PowerShell transcripts.

Enroll the same collector ID, terminal ID, boot ID and public key ID in the separate
Monitor's existing configuration. Its secret-reference mapping must resolve the
matching private HMAC material under the Monitor's own environment. The template
public IDs are `staging-boot-01` and `staging-key-01`; the default terminal is
`icmarkets-01`. Do not assume the previous multi-node example already matches these
IDs. The Monitor rejects unregistered terminals even when HMAC is valid.

Existing SignedHeartbeatTransport signs the exact payload using timestamp/nonce and
sends through bounded HTTPSPoster (10-second timeout, no inherited proxy/redirect).
The manager schedules using monotonic time; UTC observations remain the data basis.
No distribution or market acquisition API is called by this integration.

## Fake source and health propagation

FakeSourceAdapter implements the SourceAdapter poll/close contract. Tests set its
mode to normal, disconnect, stale-tick, write-failure, reconnect or exception. The
formal CLI uses normal fake mode; there is no arbitrary plugin loading or broker
connection flag. The abstraction can be implemented by reviewed MT5/cTrader adapters
later without changing health/HMAC policy.

Normal synthetic observations advance last_tick_time. A successful **actual SQLite
commit of synthetic timestamp evidence** advances last_successful_write. No market
tick/prices or CSV/Parquet are generated. A bounded single-row `synthetic_write`
table retains that evidence; it is not historical broker data or a distributable
dataset. Disconnect reports source_connected=false; stale mode preserves activity
timestamps; write failure sets `write-failed` and retains the prior successful write
until a subsequent successful write clears it.

Health includes collector_alive, source_connected, activity timestamps, disk state,
error code and logical terminal ID. Real terminal process_alive remains **unknown**:
the fake adapter does not assert that MT5 exists or is running. The Monitor may
therefore retain a terminal WARNING independently of heartbeat recovery. Real
terminal probing and per-symbol tick completeness remain future real-adapter work.

## Durability, ownership and stopping

`state/<collector_id>/sender.sqlite` stores schema v1, collector/boot identity and
the last reserved sequence. Every send reserves/commits the next sequence **before**
calling transport. Failed delivery/crash may create a safe sequence gap; a restart
never reuses a reserved value. Key IDs may rotate independently under Monitor
authorization. Boot mismatch, corrupt state and unsupported schema fail closed;
there is no reset, deletion or silent migration. Never copy an old state backup into
service while the Monitor remembers a newer sequence; coordinate a separately
authorized boot/state migration instead.

A separate `sender.lock.sqlite` connection holds an exclusive transaction for the
runtime lifetime. Local second ownership is rejected even while sequence commits
occur. OS process death releases the lock. Use local disk, not shared/network SQLite;
this is not a distributed fleet lease. Restricted directory ACLs remain required.

One bounded worker polls the source each second and lets CollectorManager schedule
heartbeat sends. No extra unjoined sender thread exists. Ctrl+C, SIGTERM and Windows
SIGBREAK handlers call request_stop; a future service adapter can call the same
method. After an in-flight bounded operation, the loop ends, closes the source,
closes already-committed SQLite state/lease, and flushes/closes rotating logs.
Repeated close is safe. Hard OS termination is not graceful and is handled by the
independent Monitor's timeout. Real adapters must implement bounded calls; this
foundation cannot interrupt arbitrary hanging native drivers.

Unexpected source/storage exceptions end the worker after fixed-code failure logging;
the stopped Collector does not try to notify its own death. Write-failure injection
is a reported observation, while a genuine unavailable state store is fatal.
Sender reservation failure is fatal even though the existing manager catches
transport exceptions. Ordinary network delivery failures consume a sequence and
retry at the next scheduled interval. Logs accept only the existing fixed Event
vocabulary; no exception body, endpoint, secret or account value is logged.

## Verification and remaining gates

Run `./.venv/Scripts/python.exe -B tests/run_offline.py` from the repository.
All prior 401 tests are retained. Added tests exercise startup/config, synthetic
write evidence, propagation, stale/disconnected/reconnected source, sticky write
failure, cadence, durable failed sends, local single ownership, corrupt/mismatched
state refusal, graceful stop, exception cleanup, dry-run, preflight integration,
HMAC receipt and an actual process hard-kill/restart over localhost.

Verification result: **422 PASS / 0 FAIL / 0 SKIP** (401 retained + 21 new).
PowerShell bootstrap syntax validation also passed; actual .ps1 execution remains
unverified under the local execution-policy restriction noted above.

The hard-kill fixture uses the real base Python interpreter directly (standard
library only) rather than the Windows venv redirector, so the test owns the worker
process it kills. No process-name-wide termination or real Collector is targeted.
Normal library tests continue in the dedicated .venv. Tests route the production
SignedHeartbeatTransport through an injected numeric-loopback poster to the real
Monitor/SQLite and fake notification sink. HTTPS is not disabled in the CLI.

The observed hard-crash sequence is receipt 1, process kill, external timeout at
181 seconds -> CRITICAL, restart with the same state -> receipt 2 -> one RECOVERY.
Reevaluation does not emit another recovery. Source/terminal health incidents are
separate from the heartbeat incident; terminal unknown is not a false recovery.

Phase 4B still needs approved access, Windows/MT5 session validation, independent
Monitor/TLS and uptime deployment, private HMAC enrollment, real MT5/cTrader adapter
and writer integration, service/task stop semantics, clock synchronization, storage
capacity/backup operations, and 24-hour/7-day testing. Fake runtime success is not
proof of real market collection. Production distribution remains disabled.
No VPS/RDP, real credentials, real notification delivery or main merge occurred.
