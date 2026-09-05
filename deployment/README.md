# Deployment handoff — not a registered or running service

These files are reviewed configuration templates, not real credentials or broker
configuration. No VPS/monitor was selected, no service was registered, and no real
notification was sent. Keep Collector and Monitor on separate hosts/failure domains.
The existing runtime currently supplies monitoring, not a complete real broker
collector entry point. The missing entry point/real probes and durable sender
sequence/boot recovery are explicit production blockers, not hidden launch steps.

## Collector Windows VPS (London-01, London-02)

Suggested layout on each independently provisioned VPS:

```text
FxTickSync-Collector/
  repo/                          reviewed revision, no secrets
  venv/                          dedicated approved Python environment
  config/
    deployment.json              from collector/deployment.example.json
    environment.env              optional private operator input; not auto-loaded
  terminals/                     operator-installed MT5, logical IDs retained
  runtime/
    data/<collector_id>/         short-term buffer; existing provenance boundaries
    temp/<collector_id>/
    logs/<collector_id>/
    exports/<collector_id>/      no distribution permission implied
    sender-state/                future durable sequence/boot state
    backups/                     future approved sender-state backup
  private-config/                owner-explicit provenance ledger, if applicable
```

The Phase 3A template's relative roots are resolved against its configuration
directory. If copied to `config/`, set the roots to the intended target layout
explicitly (absolute Windows paths are supported there). Do not use `..` traversal
or guess a drive-letter mapping. Keep data/temp/log/export roots disjoint and the
legacy ledger outside them. Do not move/register existing history as a side effect
of installing monitoring.

The template maps five fictional terminals to london-01 and five to london-02.
It is a fleet registry; select the relevant collector on each VPS. MT5 installation,
interactive session requirements and the real HealthProbe/writer callbacks are not
implemented by this template. A single shared last_tick field does not detect a
stalled individual symbol while another symbol remains active.

Verified configuration-only command, from the repository:

```powershell
& $CollectorPython -B collector_plan.py --config deployment/collector/deployment.example.json --collector london-01
```

This prints a plan; it does not start a collector. The eventual startup command is
the reviewed sender entry point with its reviewed config, then CollectorManager
with HealthProbe and SignedHeartbeatTransport. Do not substitute collector_plan.py
as a service and claim collection is running. No such real sender CLI is shipped
in Phase 3D.

Task Scheduler is the implemented configuration-generation candidate:
`fxtick.platform.scheduled_task.CollectorTask(...).write_new(...)` creates new XML
only, globally disabled, with a boot trigger, IgnoreNew and bounded restarts.
Supply reviewed Python/script/config/working-directory paths before generating it.
Windows Service wrapping is a later candidate, not an installed service here.

After separately approved registration/enabling of an actual collector task, the
following would start/stop that named task (not executed during Phase 3D):

```powershell
Start-ScheduledTask -TaskName 'fx-collector-london-01'
Stop-ScheduledTask -TaskName 'fx-collector-london-01'
```

Stop-ScheduledTask is forced termination; use the collector's reviewed graceful
stop/flush procedure first during routine maintenance. Failure-injection drills
must use explicitly selected tasks/data. The current generator uses LocalService;
review filesystem/SecretProvider permissions and MT5 session constraints before
enabling. It is not permission to run MT5 GUI automation as LocalService.

`collector/environment.example.env` contains empty references only. It is not
automatically loaded. Inject the per-collector HMAC secret and HTTPS endpoint through
the approved service's restricted SecretProvider. The collector should not contain
LINE/Email/Push credentials. Do not reset sequence to zero under a previously used
boot: persist the sequence or enroll a new authorized boot before restart.

## Independent External Monitor (e.g. Tokyo)

Repository-local staging layout supplied by these templates:

```text
deployment/monitor/
  monitor.example.json            thresholds / expected nodes / terminal IDs
  production.example.json         private server / key references / state path
  environment.example.env         empty reference template only
  runtime/                       created only during approved setup, Git-ignored
    state/monitor.sqlite
    logs/
    backups/
```

The JSON files can be copied together to another configuration directory without
code changes. `monitor_config` and `state_path` are relative to production config.
The included production profile uses logging only, fictional boot-example and no
credential values. Provision production references/boots before starting a real
deployment. Use a dedicated monitor venv or the separately approved verification
venv; dependency installation is not performed by the server.

Verified no-side-effect check:

```powershell
& $MonitorPython -B monitor_server.py --config deployment/monitor/production.example.json --check
```

In a later approved setup, create the three runtime directories and start the
foreground backend from the repository:

```powershell
New-Item -ItemType Directory -Force deployment/monitor/runtime/state, deployment/monitor/runtime/logs, deployment/monitor/runtime/backups
& $MonitorPython -B monitor_server.py --config deployment/monitor/production.example.json
```

Stop the foreground process with Ctrl+C and wait for exit; the runner invokes
runtime.stop in its cleanup. Confirm the process exited before opening maintenance
tools or switching state paths. A hung synchronous provider can delay shutdown;
service-supervisor stop policy and forced-termination recovery need staging tests.

Use a Windows Service/Task Scheduler wrapper or a Linux process supervisor on the
chosen monitor host after its shutdown/restart behavior is reviewed. Record stdout/
stderr under runtime/logs with rotation and retention; the current runner does not
create a rotating log file automatically. Do not log raw headers, credentials or
notification destination secrets in the wrapper/proxy.

Bind the backend to loopback only (default 127.0.0.1:8765). Terminate HTTPS at a
maintained local reverse proxy with bounded body/header/request/connection limits.
Do not expose wsgiref: Phase 3D uses it solely as an offline test server. Real
Waitress/TLS/proxy acceptance and parser behavior remain unverified until that
environment is prepared. Configure external collectors with the chosen monitor's
HTTPS /v1/heartbeat, not the collector's localhost address.

SQLite backups: schedule through the owning connection, or stop the monitor for
maintenance and use SQLiteState.backup_to with a new filename under runtime/backups.
The backup API refuses overwrite. Verify restoration at a separate path before a
switch; never replace the only source DB or delete corrupt state to regain readiness.
Offline hard-kill/rollback/backup/restore verification is documented in Phase 3D.
Operational retention/rotation and state-capacity thresholds are still required.

## External uptime monitoring of the Monitor

Use a third host/service outside both Collector and Monitor failure domains:

```text
London Collector -> Tokyo Monitor -> notification adapter
Independent uptime checker ------> Tokyo HTTPS /healthz
```

Configure GET /healthz, HTTPS certificate validation, a bounded request timeout,
non-200/unreachable detection and independent alert delivery. For example, a
30-second check with two consecutive failures is a deployment starting point to
review, not a service registered here. Recovery should be deduplicated by that
independent checker too. Do not require the stopped Monitor to report its own death.

Health returns 503 for worker/DB/freshness failure or notification degradation;
an unreachable host has no response. Notification degradation alone keeps heartbeat
acceptance ready, avoiding false VPS outages caused by a failed push service.
Thresholds, expected downtime, maintenance windows and severity belong in the
chosen uptime service's configuration after approval.

## Migration and real notification handoff

On replacement hosts, check out the same reviewed revision, create dedicated venvs,
place config, provision separately approved secrets, preserve logical collector IDs,
and preserve/restore the correct monitor state and sender sequence/boot ownership.
Do not run two active collectors with the same identity. Template validation does
not provide a fleet lease or automatic state/credential transfer.

Generic webhook already formats smartphone messages and carries stable event IDs.
The gateway must deduplicate by event ID plus route/recipient; the Phase 3D crash
tests show duplicates at a non-idempotent sink. LINE Messaging API and Email have
injected transport interfaces; a concrete reviewed vendor transport is still
needed. No LINE Notify, real LINE/Push/Email sends or credential enrollment were
performed. Configure them only after VPS/Monitor destinations are selected.
