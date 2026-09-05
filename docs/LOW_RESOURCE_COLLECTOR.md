# Low resource collector foundation

This change adds optional execution budgets, local resource observations, loss-explicit
batch buffering and a plan-first Windows terminal launcher. It does not install MT5,
connect a broker, change Windows settings, register services or enable distribution.
The Phase 4B runtime still uses FakeSourceAdapter; real tick storage is a future adapter.

## Profiles and collector wiring

Use `--execution-profile configs/execution.collector.json` with the existing
`python -m fxtick.collector --config ... --runtime ...` command (including `--dry-run`).
The existing runtime JSON and schema-1 signed heartbeat remain unchanged.
Omitting the option preserves existing behavior. Windows probing stays behind the
platform adapter; another OS can inject its own resource probe into CollectorRuntime.

Collector defaults: sample every 30 seconds; WARNING below 3 GiB available RAM;
CRITICAL below 1.5 GiB; 16 MiB payload buffer / 10,000 records per batch;
5 MiB logs with five backups. The buffer has Python record/container overhead in
addition to its payload budget. Analysis has its own larger configurable budgets;
neither profile changes source polling cadence, drops ticks or enables parallel
analysis automatically. `low_resource_mode` records the selected operating intent;
the explicit budgets determine behavior. No analysis/backtest runs on the VPS.

The collector keeps its existing Event.wait loop and one-second synthetic polling.
Metrics sample independently of source polling and never kill a process. Existing
synthetic SQLite writes are not batched or weakened. The new BoundedBatch is an
adapter building block, not an already integrated market-data/Parquet writer.

## Metrics, memory guard and leak observation

WindowsResourceProbe uses read-only Win32 calls for total/available RAM, system commit,
CPU delta, working set, private bytes, cumulative process read/write I/O, process
count and disk free space. CPU is UNKNOWN on the first sample. Process I/O includes
non-disk I/O and is not a physical-disk activity measurement. Inaccessible metrics
stay UNKNOWN. PID reuse cannot be attributed to the original process.

ResourceObserver writes bounded `resources.jsonl` alongside collector logs, including
memory WARNING/CRITICAL/RECOVERY transitions and private-memory deltas at configured
1h, 6h, 24h and 7d intervals. Deltas describe growth, not a proven memory leak;
process restarts reset anchors. It retains one anchor per interval, not every sample
in RAM. Defaults retain enough ordinary 30-second samples for a seven-day review.
Review actual record size and retention during the soak test. WARNING/CRITICAL
remain local structured events; no real LINE/email/webhook is sent by this change.

ResourceMetrics.to_dict is a separate versioned future-heartbeat payload with optional
queue_depth, tick_rate and write_latency. Those values stay UNKNOWN until a real writer
supplies evidence. Resource metrics and memory events are **not yet sent to the
independent Monitor**: schema negotiation and authenticated receiver integration must
be implemented and tested together before remote resource alerts are claimed.
Resource-log I/O failure is carried as fixed `resource-monitor-failed` in the existing
health error field, preserving source/write errors when present; it does not stop the
source merely to protect telemetry. Disk/state write failures retain existing behavior.

Read-only sampling command (existing dedicated output directory required):

```powershell
& ./.venv/Scripts/python.exe -B -m fxtick.resource_monitor --profile configs/execution.collector.json --directory <new-log-directory> --disk C:/ --duration-seconds 60
```

Use `--pid <verified-process-id>` to observe an individual MT5. The observer does not
install a background schedule or claim a completed 24h/7d test. Physical disk throughput
must be measured separately with Windows performance counters. Do not subtract summed
process working sets from total RAM as an exact “without Codex” estimate: shared pages,
caches and companion processes make that arithmetic misleading. A measured Codex-free
baseline requires a later run with Codex absent and the collector/session preserved.

## Buffer and Parquet boundary

BoundedBatch only accepts immutable bytes. Before exceeding bytes or record count it
attempts an emergency flush. A sink must durably commit before returning exactly True.
Failure retains the complete batch and raises BufferPressure before accepting the
incoming record. Oversized records are explicitly refused. The caller MUST retain or
replay unaccepted input and apply backpressure; catching the exception and discarding
the record violates the contract. An ambiguous commit requires idempotent downstream
writes. This RAM buffer is not a durable source cursor and cannot prevent crash loss
without the future adapter's cursor/replay/spool protocol. Never advance an acquisition
cursor before a successful durable write. Bound queued batches as well as each batch.

Future Parquet acquisition must create bounded Arrow RecordBatches with a single-owner
writer, preserve provenance, and finalize via the existing artifact/policy boundaries.
No unguarded Parquet/export sink is added here. The old transformation/history functions
are preserved; this change does not claim to convert them all to streaming.

## MT5 startup and acceptance

Plan only:

```powershell
& ./.venv/Scripts/python.exe -B -m fxtick.platform.terminal_start --config <ten-terminal-registry> --profile configs/execution.collector.json --count 1
```

Only after installer-signature verification and independent Data Folder acceptance,
add `--start` to explicitly launch the selected registered executables, minimized with
`/portable`. Each selected terminal directory must be distinct. The launcher skips
an already observed exact executable, waits the configured ten seconds between new
launches, and refuses further starts on WARNING/CRITICAL/UNKNOWN available RAM.
Already running processes are retained. No login is supplied. `startup_settle_seconds`
is the capacity-test settling budget; the launcher does not perform capacity tests.
The launcher is not an autostart service and cannot prove Data Folder isolation.

Acceptance order: idle baseline; inspect setup publisher/signature/version; install
mt5-01; verify its Data Folder; clean shutdown/restart; apply only verified collection-
neutral settings; measure x1; expand to x5 and measure; expand to x10 only with margin;
recheck all Data Folders; create shortcuts; prepare manual taskbar pins; activate registry.
Wait at least the configured settling interval before each steady-state measurement;
sample startup separately. Stop expansion below 3 GiB available or on instability,
and stop the capacity exercise below 1.5 GiB without killing important collectors.

No broker login is allowed in the current staging request, so live tick continuity
before/after MT5 tuning cannot yet be verified. Do not change subscriptions, polling,
bars or collection-sensitive settings based on assumptions. Empty charts/no custom
indicators/no EA/no tester, minimized operation and optional news settings require
real-terminal observation before recording them as applied. RDP disconnect/reconnect,
logoff and reboot behavior still need a real acceptance test. Do not configure Windows
autologon, weaken security, disable Defender/Update/RDP, or delete MT5 logs for performance.

References: [MT5 portable startup](https://www.metatrader5.com/en/terminal/help/start_advanced/start),
[MT5 settings](https://www.metatrader5.com/en/terminal/help/startworking/settings),
[Windows I/O counters](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getprocessiocounters).

## Verification

Run the complete offline suite, pip check and git diff --check. New tests cover
threshold ordering/unknowns, pressure retention and retries, sampling dedup/recovery,
growth intervals, resource-log failure persistence, distinct terminal directories,
and continued fake collection under CRITICAL memory. A pre-existing test now resolves
TEMP's Windows 8.3 alias consistently with runtime path normalization.
Native MT5 launches, real tick continuity, capacity x1/x5/x10, RDP/reboot persistence
and long-duration leak acceptance remain separate deployment gates.
