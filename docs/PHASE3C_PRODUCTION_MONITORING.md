# Phase 3C — Production Monitoring / Notification / Recovery Foundation

## Implemented and intentionally unverified boundaries

Phase 3C supplies an authenticated WSGI heartbeat application, a private production
server entry point, monitor worker/self-health, rotating HMAC authentication,
durable nonce consumption, event-time smartphone messages, LINE Messaging API and
Email adapter interfaces, database integrity/backup operations, disabled Windows
Task Scheduler XML and non-executing recovery plans.

The production entry point requires Waitress and an operator-managed TLS proxy.
Waitress is absent in the development environment. Its proposed dependency range
is isolated in requirements-monitor.txt; no package was installed and no current
patch/security status was fetched. Select and lock a reviewed patched version in
the deployment environment before exposing this service. A WSGI harness exercised
the actual application, monitor thread, HMAC and temporary SQLite; this does not
prove production HTTP parsing, socket serving, TLS or reverse-proxy behavior.

No listener was started, credential generated/enrolled, external API contacted,
Task Scheduler job registered or recovery executed during implementation. Approved
development-branch Git pushes were the only external operations. No existing market
data, legacy registry, provenance/policy or distribution boundary was changed.

## Runtime structure

```text
Collector Manager + HealthProbe (Phase 3B)
    -> SignedHeartbeatTransport / separate SecretProvider
    -> HTTPS proxy on independent monitor host
    -> private Waitress WSGI backend
    -> HeartbeatApplication -> bounded inbox + acceptance Future
    -> monitor owner thread: HMAC, replay, receipt transaction
    -> Phase 3B evaluation / dedup / cooldown / durable outbox
    -> Generic Webhook / injected LINE Messaging API / injected Email transport

Independent uptime service -> HTTPS /healthz
                            -> frontend process + monitor worker + cached DB/evaluation/delivery health
```

SQLite remains owned by one worker thread. HTTP threads never operate that
connection. Receipt acknowledgement occurs only after authentication and durable
commit. The owner performs integrity checks, evaluates independently of collector
hosts, and records evaluation freshness before sending notifications. A failed or
stuck worker cannot remain indefinitely healthy merely because the HTTP process
still answers requests.

## HTTP routes and deployment boundary

- `POST /v1/heartbeat`: strict UTF-8 schema-1 heartbeat, Content-Type application/json
  (optional charset=utf-8), explicit Content-Length, at most 65536 bytes by default.
  Existing collector/schema/activity-time/terminal validation remains authoritative.
- `GET /healthz`: returns schema version, process_alive, monitor_worker_alive,
  db_accessible, last_evaluation_time, notification_state, ready and healthy.

Status semantics:

| Status | Meaning |
|---|---|
| 202 | Authenticated receipt committed; queue insertion alone is insufficient |
| 400 | Malformed JSON/schema/headers/framing or unsupported query |
| 403 | Unknown/unauthorized/replayed sender, invalid signature/time/boot, or insecure transport |
| 404 / 405 | Wrong route / method |
| 411 / 413 / 415 | Missing length / oversized body / unsupported content type |
| 429 | Rate limit |
| 503 | Worker/DB unavailable, ingress full, or acceptance deadline exceeded |

Query parameters, compressed bodies and application-visible Transfer-Encoding are
rejected. Duplicate/ambiguous Content-Length and raw HTTP request framing must also
be rejected by the maintained server/proxy before WSGI; collapsed WSGI headers
cannot reconstruct every raw-header ambiguity. Waitress runner options bound body
and header sizes, idle channels, connections and worker threads. Responses are
JSON with no-store and never reflect request bodies, authentication material or
raw exceptions. Configure upstream logs to omit credentials/proof headers too.

The included runner only binds an explicit loopback address (default 127.0.0.1:8765).
Terminate HTTPS at a reviewed local reverse proxy. Plain HTTP is allowed only from
loopback when allow_loopback_http is true; otherwise WSGI must identify HTTPS.
The application does not trust X-Forwarded-For/Proto supplied by clients. Do not
expose a development server, forward arbitrary trusted-proxy headers or publish
the backend port. The server/proxy must impose total request/header/body deadlines
and limits as well as the application limits; DNS/OS behavior is not a hard budget
guarantee from the Python adapter timeout alone.

TokenBucketLimiter is a bounded, thread-safe per-peer implementation of RateLimiter.
Defaults: burst 30, 120 requests/minute, up to 1024 peer buckets. A same-host proxy
shares one peer bucket across collectors; tune deliberately. A fleet/shared limiter
or edge policy can replace it. The application deadline defaults to 5 seconds. A
timeout cancels queued work where possible; already executing work may have committed
even when the response is lost. Never treat an unconfirmed response as success.

## HMAC authentication and rotation

Authentication uses HMAC-SHA256 with constant-time hmac.compare_digest. Headers:

```text
X-FX-Key-Id: public logical key identifier
X-FX-Timestamp: UTC Unix seconds
X-FX-Nonce: 32 lowercase hexadecimal characters
X-FX-Signature: 64 lowercase hexadecimal characters
```

The signed message joins the following ASCII fields with newline separators:

```text
fx-heartbeat-hmac-v1
POST
/v1/heartbeat
collector_id
boot_id
key_id
timestamp
nonce
SHA256(exact UTF-8 payload bytes)
```

The signature binds method, route, sender, boot, public key ID, time, nonce and exact
body. A signature timestamp window (default +/-90 seconds, configurable up to 300)
is independent of Phase 3B snapshot age/future-skew checks. Keys are resolved at use
through SecretProvider and must encode at least 32 bytes. Length is not proof of
entropy: production keys require separately authorized secure provisioning. No real
key was generated here; tests use clearly synthetic fixtures.

SenderKeys maps each registered collector to allowed key IDs/references and an
explicit boot allowlist. Unknown keys/boots fail closed. Nonce hashes and expiry
are stored under a reserved monitor_meta namespace in the same transaction as
the receipt. This closes the nonce-check/receipt-write race and survives restarts.
Expired nonce records are pruned only after their signature window ends. Phase 3B
sequence, observation ordering and retired-boot checks still apply. Rotation cannot
reopen an already consumed nonce by changing key ID.

Rotation procedure (not performed here):

1. Obtain approval for provisioning the new production secret separately.
2. Add its public key ID/reference to the receiver alongside the old key; configure
   distinct per-collector secrets and service-restricted access.
3. Switch the sender to the new key, retaining increasing sequence/fresh nonces.
4. After the overlap/acceptance window and verification, remove the old key mapping
   and secret. Restart/reload through a reviewed deployment procedure.

EnvironmentSecrets maps logical references to environment variable names; ordinary
JSON contains only names/references. Missing keys make startup unready. Do not
store values in JSON, Git, Task XML, process arguments, heartbeat bodies or logs.
Other secret stores can implement the same interface. Under a service identity,
provision a restricted provider rather than exposing secrets machine-wide merely
to make user-session environment variables visible.

New sender boots must be explicitly authorized. An automatic restart must either
restore a durable increasing sender sequence for its authorized boot, or use a
new authorized boot. This phase does not implement a sender enrollment service or
silently allow arbitrary new boot IDs. Account/service restarts must be designed
around this constraint before enabling the Windows task.

## Smartphone messages and notification adapters

WatchdogEvent now retains an optional immutable AlertObservation (receipt/tick/write
times and the threshold used). Outbox serialization preserves it. Older Phase 3B
events lacking this field still deserialize and show UNKNOWN where appropriate.
Delayed notification renders the original event context, not a later snapshot.

```text
[CRITICAL] london-01 DOWN
Collector: london-01
Last heartbeat: 2026-09-06 03:10:00 UTC
Last tick: 2026-09-06 03:09:00 UTC
Last write: 2026-09-06 03:09:00 UTC
Reason: heartbeat timeout >= 180 sec

[RECOVERY] london-01 ONLINE
Collector: london-01
Check: heartbeat
Downtime: 8m22s
Recovered: 2026-09-06 03:18:00 UTC
```

These are formatting examples, not observed VPS data. Component recoveries say
DISK RECOVERED etc., not that the whole node is online. Unknown activity does not
claim a measured stale duration. No error_state text, credentials, raw prices or
tick arrays are forwarded to notifications.

GenericWebhookProvider adds message to its structured JSON while keeping route,
channel, event ID and Idempotency-Key. Its HTTPS transport remains implemented;
only fake POST delivery was exercised here. Any smartphone gateway must accept
the configured payload or use a dedicated NotificationProvider.

LineMessagingProvider wraps a LineMessagingTransport.push_text interface with
recipient/token resolved separately. It formats messages and passes stable event ID
for transport idempotency. It is for LINE Messaging API, never LINE Notify. The
vendor HTTP endpoint/SDK implementation is deliberately not assumed or contacted;
implement and review the transport against the applicable API before real use.

EmailProvider similarly wraps EmailTransport.send_text, with separate sender,
recipient and credential references, a compact subject and text body. Header newline
injection in sender/recipient is rejected. No SMTP connection was implemented or
opened. LINE/Email transports are injected programmatically; the JSON runner's
existing DeliveryConfig still selects logging/fake/webhook. Add a reviewed factory
for a concrete vendor transport when deploying it; do not mistake a channel label
for a configured real provider.

Provider failures return false without raw exception logging. Phase 3B's persistent
per-incident/per-route retries, cooldown after delayed confirmation and recovery
ordering remain unchanged. At-least-once delivery still needs receiver idempotency
to cover a crash after remote acceptance but before local confirmation.

## Escalation and configuration

The existing external-monitor JSON remains the source of collector IDs,
heartbeat_interval_seconds, warning_seconds, critical_seconds, last tick/write
thresholds, cooldown and recovery_notification. Phase 3B tests and defaults remain
unchanged. Production wiring references that file instead of duplicating its rules.

Receipt age 120 seconds produces WARNING and 180 seconds produces CRITICAL by
default. Configure health.cooldown_seconds=600 for ten-minute ongoing reminders
after the previous event/confirmation; this is not a claim that a reminder occurs
exactly ten minutes after the physical outage began. Severity changes bypass the
unchanged-state cooldown; pending delivery is retried instead of accumulating copies.
Recovery closes the incident once. Detection-time first_seen and duration survive
restarts. Schedule exceptions for closed markets still suspend tick/write age checks.

Production wiring configuration additionally includes monitor_config/state_path,
listen address/port, senders/allowed boots, secret_environment reference names,
routes, receiver limits, evaluation freshness and HMAC time window. It rejects
extra/missing fields, public bind addresses and mismatched signing/node inventories.
Relative paths resolve against the production configuration directory. Validation
creates no directories, opens no DB, reads no credential values and starts no server.

```text
python -S -B monitor_server.py --config configs/production-monitor.example.json --check
```

The sample uses logging notifications and fictional boot-example. It is not
production enrollment. Preparing service directories, real references/keys, valid
boot/sequence handling and vendor transports is required before enabling collection.

## Monitor self-health and persistence

/healthz returns 200 only when the worker is alive, evaluation is fresh, DB is
accessible and notification state is healthy/disabled. Degraded delivery returns
503 for uptime alerting while ready stays true, so heartbeats remain accepted.
Unavailable DB/dead or stale worker makes both health and heartbeat acceptance 503.
Freshness defaults to 30 seconds. The HTTP process itself being alive is insufficient.
Place the uptime checker outside the monitor's host/failure domain; this phase did
not provision such a service.

Startup checks SQLite quick_check, application ID and compatible schema version.
Runtime integrity/I/O failure stops the worker and makes health unavailable. No
empty replacement DB, fake recovery or automatic deletion/reset is performed.
Clock regression also remains fail-closed under Phase 3B's persisted clock guard;
supervision and correct system time are operational prerequisites.

SQLite schema remains version 1: nonce metadata uses the existing metadata table,
and event observation is an additive, backward-readable JSON field. No destructive
migration was needed. Future structural migrations must be explicit versioned
transactions, preceded by a verified backup, tested against older schemas and
refuse unknown future versions. Do not reset state merely to bypass a version check.

SQLiteState.backup_to(new_path) uses SQLite's backup API and verifies integrity.
The destination must not already exist; overwriting is refused. Run it on the
connection owner thread, or in an approved maintenance window with the monitor
stopped and the source reopened. Do not copy only the database file while an active
journal/WAL might be involved. A failed backup can leave a new incomplete destination:
never restore it without verification.

Maintenance example, for an operator-approved monitor state (not executed here):

```python
from fxtick.watchdog.store import SQLiteState
state = SQLiteState("monitor-state.sqlite")
try:
    state.backup_to("new-verified-backup.sqlite")
finally:
    state.close()
```

Test restoration to a separate path and verify receipt/incident/outbox state before
any switch. Preserve the original corrupt file and backup for investigation. This
phase tested backup/restore with temporary synthetic state only.

## Windows startup and RecoveryAction foundation

fxtick/platform/scheduled_task.py generates Task Scheduler XML only. CollectorTask
requires explicit absolute Windows paths to an approved Python executable, reviewed
collector entry script, configuration and working directory. It does not execute
schtasks, PowerShell, a broker process or any registration command.

The XML includes a boot trigger, bounded restart-on-failure, IgnoreNew instance
policy, built-in LocalService with LeastPrivilege, and a globally disabled task.
write_new refuses to overwrite existing XML. No password/token is included.
Review permissions and change/enable the task only in a separately approved VPS
deployment. LocalService is not a solution for interactive MT5 GUI startup/session
requirements. A reviewed collector entry point and durable sequence/boot handling
must be supplied; the generator is not a new real MT5 collector implementation.

RecoveryPlanner accepts an explicit collector/target/action allowlist and creates
typed plans for collector restart, MT5 restart, cTrader reconnect or service restart.
Plans carry requires_approval=true and executable=false. ApprovedRecoveryExecutor
is an interface with no implementation/call site. The original Phase 3B
RecoveryAction contract remains intact. OS reboot is not an available action.
No alert automatically triggers a restart or a notification-dependent recovery.

## Verification and Phase 3D

Executed with the existing bundled Python, fake request/server transports and
temporary SQLite, without installs or external service access:

```text
python -S -B -m unittest discover -s tests -p "test_phase3c*.py" -q
python -S -B -m unittest discover -s tests -p "test_*.py" -q
python -S -B monitor_server.py --config configs/production-monitor.example.json --check
git diff --check
```

Results: Phase 1 61 PASS; Phase 2 63 PASS and 7 SKIP; Phase 3A 70 PASS;
Phase 3B 87 PASS; Phase 3C 63 PASS. Total 351 discovered, 344 PASS, 0 FAIL,
7 explicitly unexecuted/skipped real-library tests.

The existing Phase 2 real-library tests remain unavailable due to missing acquisition,
DuckDB/PyArrow/Streamlit and pyzipper dependencies. Run them later in an approved
environment with `python -B -m unittest discover -s tests -p "test_phase2_real.py" -v`.

Also unverified (not counted as successful tests): Waitress socket/HTTP parser and
TLS/proxy behavior; native Apple Silicon/Linux; Windows task registration/XML runtime
validation; real LINE/Email/vendor delivery; production key rotation/enrollment;
long-running load, slow adapters and disaster recovery. Standard-library WSGI tests
do not replace these deployment checks. Synchronous adapters can still delay the
owner loop; stale health makes that visible but does not eliminate the bottleneck.

Phase 3D priorities: lock/review dependencies and server/proxy limits; test actual
socket/TLS/auth ingress; provision approved secrets and boot/sequence persistence;
implement reviewed vendor transports and bounded concurrent delivery; native OS and
Windows service tests; backup/restore drills; independent uptime monitoring; then
separately approved synthetic staging deployment before real production rollout.
