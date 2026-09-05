# Phase 3B — Collector Manager / Watchdog / External Heartbeat / Notification

## Scope and architecture

Implemented: portable internal health evaluation, collector observation/sending
manager, external heartbeat receiver API and timeout loop, bounded thread-safe
ingress, SQLite state/outbox, notification retries, generic HTTPS webhook,
logging/fake providers and configuration examples. The Phase 3A HealthSnapshot,
HeartbeatReceipt, severity and NotificationProvider contracts are reused without
changing their schemas. WatchdogEvent extends NotificationEvent with exact
first_seen_at; its base contract remains compatible.

```text
London Windows VPS                         Separate Tokyo monitor host
  MT5 #1..5 / cTrader (future adapters)       expected collector inventory
  HealthProbe -> CollectorManager --------> authenticated ingress adapter (future)
  internal health evaluation   heartbeat     -> bounded HeartbeatInbox
  last tick and last write independently     -> ExternalMonitor.receive
                                            -> SQLite receipt / replay state
London-02, MT5 #6..10 ---------------------> timeout and health evaluation
                                            -> incident state + durable outbox
                                            -> NotificationProvider
                                               logging / fake / HTTPS webhook
                                               future LINE / Push / Email
```

The monitor's process/host runs independently of collectors. It uses its own
receipt clock and a configured inventory, so a collector VPS needs to send nothing
at all for the external monitor to detect its disappearance. Never-reported nodes
are also detected after a persisted startup grace period. Restarting the monitor
does not reset that grace or forget known down nodes.

No HTTP listener, public endpoint, hosted monitor, Windows service, MT5/cTrader
connection, trading, process restart, LINE/SMTP API or live notification was
started. HTTPS adapters are implemented but only exercised through fakes. The
production ingress/auth adapter and deployment are follow-on integration work;
this is not a deployed 24/7 service.

This phase does not acquire, transform, upload or publish market data. Phase 1/2
policy, provenance, legacy registration and MT/ZIP/Drive/Streamlit/workflow gates
remain unchanged. Health messages contain no prices, tick arrays, source licence
approvals or downloadable datasets. No existing history/registry was modified.

## Files and dependency boundaries

| File | Responsibility |
|---|---|
| `fxtick/watchdog/config.py` | Strict monitor/node configuration and UTC windows |
| `health.py` | Internal evaluation, HealthProbe/MonitoringSchedule/RecoveryAction interfaces, read-only disk probe |
| `heartbeat.py` | Envelope schema, authentication interface and bounded ingress |
| `store.py` | Dedicated SQLite state and Phase 3A HeartbeatStore.latest contract |
| `monitor.py` | External receive/evaluate/dispatch loops and incident transitions |
| `manager.py` | Collector-side probe/send cadence; no broker acquisition |
| `providers.py` | HTTPS, logging/fake notification and heartbeat transport adapters |
| `delivery_config.py` | Channel/provider selection with secret references only |
| `monitor_demo.py` | Synthetic offline scenario with temporary SQLite |
| `configs/external-monitor.example.json` | Tokyo monitor watching London-01/02 and Frankfurt-01 |
| `configs/notification.example.json` | Generic push gateway logical references |
| `tests/test_phase3b*.py` | Fake-clock/auth/transport and temporary-state verification |

All new modules use the Python standard library. No Windows, MT5, cTrader,
DuckDB, PyArrow or Streamlit import is required. Core still runs independently of
Windows adapters; OS-specific probes belong behind HealthProbe/TerminalAdapter.
Native macOS/Apple Silicon/Linux execution is not inferred from simulated imports.

## Configuration

Monitor JSON uses schema_version 1 and explicit fields; duplicate keys, extra
fields, missing fields, malformed IDs/windows and duplicate collectors are denied.
It is separate from Phase 3A deployment JSON and its unchanged monitoring policy
example. Join the two configurations by stable collector_id/terminal_id when
deploying. The external expected-node inventory must exist independently of
received heartbeats. Examples are fictional, not discovery of real VPS/account IDs.

Defaults per node:

| Setting | Default / meaning |
|---|---|
| heartbeat_interval_seconds | 60; collector send cadence |
| warning_seconds | 120; receipt age >= threshold produces WARNING |
| critical_seconds | 180; receipt age >= threshold produces CRITICAL |
| startup_grace_seconds | 180; never-seen node becomes CRITICAL at this age |
| max_clock_skew_seconds | 30; maximum future sender timestamp |
| max_payload_age_seconds | 90; oldest accepted sender timestamp |
| health.last_tick_timeout_seconds | 180; stale tick WARNING |
| health.last_write_timeout_seconds | 300; stale write WARNING |
| health.min_disk_free_bytes | 1 GiB; lower capacity is CRITICAL |
| health.cooldown_seconds | 300; unchanged confirmed incident reminder interval |
| retry_seconds | 60; retry an unconfirmed delivery at most once per interval |

`interval < warning < critical` is required. The Phase 3A field
`health.heartbeat_timeout_seconds` is retained for schema compatibility; the
external monitor uses the explicit warning_seconds/critical_seconds pair.
`health.recovery_notification` controls routing only: a RECOVERY event is always
generated and the incident state updated, even if routing is disabled explicitly.

`active_windows` is an ordered list of disjoint half-open UTC minute intervals
since Monday 00:00, in [0,10080]. `[[0,10080]]` means always open; `[]` means
closed. These affect tick/write age checks only, never heartbeat, disk, process,
source or collector liveness. No default FX holiday/session assumptions are made.
Operators must configure appropriate sessions; examples deliberately use 24/7.

Implement `MonitoringSchedule.active(collector_id, check, now)` for symbol,
holiday, maintenance or post-open warmup rules. It can resolve collector IDs to
Phase 3A symbol configuration. Phase 3B's snapshot age is collector-level: true
per-symbol tick/write observations and incident keys need a future additive model.
Do not claim per-symbol outage detection for a collector aggregating many symbols.

## Internal watchdog and Collector Manager

`HealthProbe.sample(now)` returns the existing HealthSnapshot. It must use the
requested collector ID and current UTC observation time. Probe failure becomes an
unknown snapshot with stable `probe-failed` error code; raw exception text is not
forwarded or logged. This degraded heartbeat can still reach the external monitor.

`evaluate_health` evaluates:

- False collector/source/process/accessibility state -> CRITICAL.
- Unknown boolean/age/free-space state -> WARNING, never implicitly healthy.
- Stable collector error code -> CRITICAL.
- Low free space -> CRITICAL.
- Old last_tick_time -> WARNING, independently of collector/process liveness.
- Old last_successful_write -> WARNING even while fresh ticks arrive.
- Missing registered terminal observation -> WARNING; false process_alive -> CRITICAL.

Closed schedules suspend tick/write checks; they do not fabricate recovery for an
existing incident. A new open-session observation can resolve it. While a heartbeat
is stale/missing, component checks are suspended rather than treating cached values
as proof of component recovery.

`probe_disk(path)` uses read-only directory/access/free-space inspection. It creates
no write-test file and does not touch market-data contents. Accessibility is advisory:
ACLs, quotas, races or later I/O failures can still prevent writing. Actual confirmed
write timestamps must come from a writer adapter; they are never derived from ticks.

`CollectorManager.step()` samples/sends due collectors and returns internal health
decisions. `run(stop_event)` supplies a stoppable polling loop. Cadence uses a
monotonic clock; UTC clock jumps do not trigger rapid sends. Failed transport does
not stop other collectors. The manager does not launch collectors or acquire data;
future broker/writer adapters supply observations.

Supply a new externally authorized boot_id after a process restart, or separately
persist/restore a sequence. Reusing an old boot with sequence zero is rejected by
the receiver. The manager does not generate credentials or authorize new boots.

## Heartbeat and security boundary

Envelope: schema_version, boot_id, sequence, status and the complete schema-1
HealthSnapshot. The nested snapshot provides collector_id, observed_at UTC,
last_tick_time, last_successful_write, source_connected and health fields. Status
is derived and checked: degraded / unknown / observed; `observed` does not bypass
the monitor's age/capacity rules or mean guaranteed healthy.

Receive order:

1. Limit payload to 64 KiB; require UTF-8 strict JSON fields, unique keys and schema.
2. Validate HealthSnapshot, logical IDs and bounded integer sequence.
3. Require registered collector and injected authentication of exact payload bytes,
   bound to that collector and authorized boot. No accept-all production default.
4. Validate timestamp age/skew, expected terminals, increasing sequence and
   non-regressing observation time. Reject previously retired boot IDs.
5. Atomically persist receipt timestamp, payload, active boot and replay history.

Authentication proof is out of band; it is never stored in the payload, SQLite or
logs. `HTTPSHeartbeatTransport` resolves references through SecretProvider and
sends an Authorization header. Production authentication must authorize boot
changes; merely allowing arbitrary sender-selected boot IDs is insufficient.
This phase supplies the interface, not secret creation, enrollment, signature
verification or a production trust configuration. `DemoAuth` exists only in the
offline demo; it must never be mounted on an endpoint.

`HeartbeatInbox.submit()` accepts bounded work from listener threads and returns a
Future. `ExternalMonitor.run_once()` drains it on the SQLite owner thread. An HTTP
adapter must await successful Future completion before acknowledging acceptance;
queue insertion is not receipt/authentication success. Cancel timed-out requests
before processing where possible. Invalid messages receive sanitized failures and
do not crash monitoring. Storage/internal failures propagate to supervision.

Before publishing an endpoint, implement authenticated ingress, TLS termination,
header/body/request limits, rate limits, secret provisioning/rotation and deployment
authorization. None was exposed here. Restrict network destinations at deployment;
only operator-provisioned secret references select webhook URLs.

## Incidents, delivery and recovery

Alert key = collector ID + check + optional terminal ID. Durable state includes
first_seen, last_seen, current_state, recovered_at, last_event_at, last_notified_at, event_id and
notification_sent. first_seen is detection time, not an asserted exact physical
outage onset. After an unobserved gap, downtime is consequently detection-to-recovery.

Initial anomaly emits an event. A severity change emits immediately. An unchanged
incident emits no new event before cooldown; an unconfirmed current event is retried
with its stable ID instead of generating repeated cooldown copies. Recovery emits
one RECOVERY event and closes the incident. A new failure opens a fresh epoch.
After a delayed successful delivery, cooldown also runs from its confirmation
time; an overdue reminder cannot fire immediately after that delivery succeeds.

Event payload includes collector/check/terminal, severity, exact first_seen_at,
occurred_at, recovered_at and whole-second outage_seconds. Example synthetic
scenario: warning at 120s, critical at 180s, reminder at 480s, recovery at 622s:
detected outage = 502s (8m22s). Repeated healthy observations do not emit recovery again.

State changes and outbox enqueue share a SQLite transaction. Sending occurs after
commit. Pending deliveries store last_attempt and delivered status per event/route.
Only confirmed success marks delivered; notification_sent means all queued routes
for the incident's current event confirmed. Failed delivery cannot terminate health
evaluation. Retry ordering is per incident and route, so a recovery follows its
outage notification, while another collector/route can still proceed independently.

Delivery is **at least once**, not exactly once. A crash after remote acceptance but
before local confirmation can repeat a send. Generic webhook passes event_id as the
Idempotency-Key; the receiving service must honor it to prevent duplicates. Retried
events retain the original occurrence time. Prolonged outages can leave escalation/
recovery history queued for later delivery; dashboards should show those timestamps.

## Notification adapters / LINE / smartphone push

The Core calls NotificationProvider.send(event, route); it imports no LINE or push
SDK. `DeliveryConfig` accepts logging, fake or webhook, with LINE/Push/Email channel
labels. Config stores only logical endpoint_reference/token_reference, never actual
URL/token/recipient values. SecretProvider resolves these separately at send time.

- LoggingNotificationProvider reports only typed event identity/state.
- FakeNotificationProvider records confirmed deliveries in memory for tests/demo.
- GenericWebhookProvider posts structured event JSON to an operator-selected gateway.
- HTTPSPoster requires HTTPS, bounded timeout (default 10s), disables redirects and
  implicit environment proxy inheritance, and never logs response bodies/credentials.
  Only 2xx counts as success. Invalid headers, endpoints and provider exceptions fail.

To add LINE, implement a NotificationProvider for **LINE Messaging API**, map the
event to a message, and resolve recipient/token through the separate secret layer.
Do not use LINE Notify. Add fake HTTP tests and confirmed-send semantics before
registering the provider. No LINE API implementation/credentials were used here.

For smartphone push, point GenericWebhookProvider at an authorized gateway or add
a provider with the same contract. Email similarly uses a future provider or gateway;
there is no SMTP connection in this phase. Do not embed raw ticks or arbitrary health
exceptions in notifications. Generic webhook support is not proof of compatibility
with any specific vendor API.

## Persistence and deployment wiring

SQLiteState uses its own file, application ID and schema version. It rejects an
unrelated SQLite database before creating tables. Its tables contain expected-node
enrollment, last receipts, boot history, incident states and delivery outbox. No
market-data or provenance database should be used as this file. Do not delete state
to silence alarms; retain it across restarts. Backups/retention/schema migration are
operator responsibilities for the next deployment phase.

One monitor process/thread owns SQLite writes. Listener threads use HeartbeatInbox.
Run the receiver/state loop independently of collector hosts; use Tokyo-01 plus
London-01/02 from the examples, with optional Frankfurt-01. The monitoring example
matches the ten fictional terminal IDs in the Phase 3A Windows deployment example
(five on each London collector). Frankfurt has no registered terminals. Missing
London terminal observations consequently warn until the probe adapter supplies
them; this is not evidence that real MT5 processes were inspected here.

Wiring sequence for a future approved deployment:

1. Load MonitorConfig and DeliveryConfig on the independent host.
2. Provision a dedicated state path and real authentication/SecretProvider adapters.
3. Construct routes/providers, SQLiteState, HeartbeatInbox and ExternalMonitor.
4. Attach the separately deployed authenticated listener to inbox.submit; await its
   acceptance Future. Run monitor.run(stop_event) on the SQLite owner thread.
5. On each VPS, construct ManagedCollector with policy, bounded HealthProbe,
   HTTPSHeartbeatTransport and authorized boot ID. Run CollectorManager.
6. Test synthetic failures/recovery, confirm monitoring host independence, then
   separately authorize real deployment. Add external supervision for the monitor.

Probe/send callbacks are synchronous and must be bounded. Slow callbacks across ten
collectors/routes can delay polling: use separate processes/managers or a tested
concurrent adapter in production, and capacity-test against heartbeat thresholds.
The HTTP timeout alone is not a hard total budget for every OS/DNS/probe operation.
RecoveryAction is an extension interface only; monitoring never invokes restarts.

## Offline verification and remaining work

Executed using the existing bundled Python, without installs or external API access:

```text
python -S -B -m unittest discover -s tests -p "test_phase3b*.py" -q
python -S -B -m unittest discover -s tests -p "test_*.py" -q
python -S -B monitor_demo.py
git diff --check
```

Phase 1: 61 PASS. Phase 2: 63 PASS, 7 explicitly unexecuted/skipped. Phase 3A: 70
PASS. Phase 3B: 87 PASS. Total: 281 PASS, 0 FAIL, 7 SKIP (288 discovered).
Tests include full sender-to-receiver fake transport, persistence/replay/recovery,
closed schedules, provider failure/secret redaction, thread ingress, independent
collectors/routes and unrelated-database preservation. The demo uses only temporary
synthetic state; its temporary directory is removed when it finishes.

Existing real-library integration tests remain unexecuted because acquisition,
DuckDB/PyArrow/Streamlit and pyzipper dependencies are unavailable. After preparing
an independently authorized environment, run:

```text
python -B -m unittest discover -s tests -p "test_phase2_real.py" -v
python -B -m unittest discover -s tests -p "test_*.py" -v
```

Not verified: native Apple Silicon/Linux, real MT5/cTrader processes/connections,
external HTTP/TLS/proxy behavior, actual LINE/Push/Email delivery, production
authentication/boot enrollment, fleet load, prolonged operation and service recovery.
Approved development-branch Git pushes are the only external operations performed.

Recommended Phase 3C: real OS/dependency matrix; authenticated ingress and boot
ownership; bounded real probes and per-symbol/session models; service packaging and
concurrency/load tests; durable-state backup/retention; idempotent vendor adapters;
independent monitor supervision; then separately approved synthetic deployment.
