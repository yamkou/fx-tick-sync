# Phase 4A — Windows Server 2025 x64 staging package

**Phase 4B-0 update:** the generic runtime and durable sender are now connected for
explicit fake staging. Use [the current runtime runbook](../../docs/PHASE4B0_COLLECTOR_RUNTIME.md)
and `preflight --runtime ...` for that scope. The Phase 4A real-source gates and
historical test record below remain applicable to actual MT5 integration.

## Verification record

Local Python 3.12.14 dedicated .venv: `python -B tests/run_offline.py` completed
**401 PASS / 0 FAIL / 0 SKIP**, 40.500 seconds. This includes all previous 383 cases
and 18 new staging tests. The new tests cover staging/quarantine/one-terminal rules,
preserved existing history/config, rejected path escape and distribution templates,
dry-run non-effects, secret/exception redaction, missing prerequisites, opt-in network
with a fake checker, write/disk failures, and bounded log rotation.

PowerShell AST syntax validation passed. An attempted no-install bootstrap smoke in
a temporary synthetic layout was refused by this host's PowerShell execution policy
before the script ran. **Bootstrap execution is unverified**, not a passing smoke
test or unittest SKIP. No execution-policy bypass/change was performed. Validate the
script on the authorized staging host using its approved script-signing/execution
policy. Windows Server 2025, MT5, services, real secrets and external Monitor were
not exercised here. No VPS/RDP connection occurred.

This package prepares one MT5 terminal on a new Windows Server 2025 x64 VPS using
Python 3.12. It does not connect to a VPS, start MT5, log in to a broker, register a
task/service, change firewall/RDP settings, generate credentials or enable distribution.
The broker and logical IDs are configurable; `icmarkets-01` is an example ID only.

**Gate:** the current repository has CollectorManager, HealthProbe/TerminalAdapter
interfaces and signed transport, but no real MT5 collector entry point with durable
sender boot/sequence recovery. Preparation/dry-run works; actual collection and
heartbeat startup remain blocked until that integration is implemented and reviewed.
Do not install a dry-run or collector_plan command as a running collector service.

## Standard relocatable layout

Choose a new explicit root, for example `C:\fx-tick-sync` (not hard-coded in Python):

```text
<root>/
  app/                         repository clone
    .venv/                     isolated Python; ignored by Git
  config/collector.staging.json
  config/reference-ledger.json  reserved; not created/registered automatically
  data/london-01/               local QUARANTINE only
  logs/london-01/               collector.log, watchdog.log, heartbeat.log, error.log
  state/london-01/              future durable sender state
  temp/london-01/
  exports/london-01/            reserved; no distribution permission
  terminals/icmarkets-01/terminal64.exe
  backup/                      separate-path backups, never overwrite only copy
```

Generated paths are absolute to the selected root. To migrate, run preparation at
the new root and explicitly transfer approved state/config; do not copy a venv or
reuse stale absolute paths. No Administrator profile path is assumed. Existing files
are never deleted; config creation uses exclusive create. Redirected operational
directories are rejected. Use an operator-owned root with restricted ACLs; preflight
path checks do not defend against a privileged actor racing filesystem changes.

## Bootstrap on the future VPS

Install approved Git and Python 3.12 x64 separately. These commands are instructions
for Phase 4B, not actions taken on a VPS during Phase 4A:

```powershell
git clone --branch codex/dukascopy-isolation --single-branch https://github.com/yamkou/fx-tick-sync.git C:\fx-tick-sync\app
Set-Location -LiteralPath C:\fx-tick-sync\app
git status --short --branch
# Verify checkout revision against the reviewed commit before running scripts.
./deployment/windows-staging/bootstrap.ps1 -Root C:\fx-tick-sync -Python 'C:\Path\To\Python312\python.exe' -CollectorId london-01 -TerminalId icmarkets-01 -Broker your-broker-id -InstallDependencies
```

The script requires the clone at `<root>/app`, checks Python/x64, creates `.venv`
only if absent, validates an existing venv without recreating it, optionally installs
the Phase 3E pinned dev stack, runs pip check, creates a new staging config/layout,
and runs a config-only dry-run. Omitting `-InstallDependencies` performs no download.
Existing config is validated, not updated from new ID arguments: to change IDs,
review/edit config explicitly or select a new deployment root. An installation
failure may leave an incomplete venv; inspect/retry the pinned install, never delete
it automatically. No execution-policy bypass or global Python package install occurs.

`collector.template.json` is input to `fxtick.staging init`, not a directly runnable
deployment file. Generated config has exactly one MT5 collector and terminal, a
matching broker mapping, `environment=staging`, and local `QUARANTINE` storage.
Production, DISTRIBUTION storage and additional terminals are rejected by staging
validation. The existing provenance/policy gates remain the distribution authority.
An unreviewed broker is not redistributable merely because acquisition is possible.

## Preflight and smoke commands

```powershell
& ./.venv/Scripts/python.exe -m pip check
& ./.venv/Scripts/python.exe -B -m fxtick.preflight --config C:\fx-tick-sync\config\collector.staging.json
& ./.venv/Scripts/python.exe -B -m fxtick.staging dry-run --config C:\fx-tick-sync\config\collector.staging.json --component collector
& ./.venv/Scripts/python.exe -B -m fxtick.staging dry-run --config C:\fx-tick-sync\config\collector.staging.json --component heartbeat
& ./.venv/Scripts/python.exe -B tests/run_offline.py
```

Preflight reports fixed PASS/FAIL/NOT_RUN codes, never environment values, endpoints,
raw exceptions or file contents. It checks Windows Server 2025, x64, Python 3.12,
Git on PATH, repository .venv isolation, installed versions against the tested 58
constraints, config/IDs/terminal registry, MT5 executable existence, secret presence,
HTTPS monitor endpoint, writable per-collector data/log/state directories and at
least 5 GiB free on each. The default free-space floor is only a staging prerequisite,
not a tick-history capacity estimate. Writes are restricted to newly created temporary
probe files that are removed afterward; existing data is untouched.

Exit code **2** means blocked; current code deliberately reports
`real-collector-probe-and-durable-sender-entrypoint=FAIL` even if prerequisites pass.
Dry-run exit 0 means configuration validation only; its output always says
`runtime_ready=false`, `network_sent=false`, `distribution_enabled=false`.
Missing MT5/secrets are expected before separate operator setup. Do not waive the
runtime blocker to claim a working collector.

Only after the Monitor endpoint and network check are explicitly approved:

```powershell
& ./.venv/Scripts/python.exe -B -m fxtick.preflight --config C:\fx-tick-sync\config\collector.staging.json --check-network
```

This sends a bounded HTTPS GET to that host's `/healthz`, with certificate validation,
no redirect/proxy inheritance and no HMAC or other credential headers. It never sends
a heartbeat or market data. A degraded monitor may return 503 despite functioning
network connectivity; this check tests readiness as well as reachability.
No such external check was run in Phase 4A.

## Secret setup boundary

`.env.example` contains **variable names only**, not dotenv assignments, and is not
loaded by bootstrap. Required on Collector: `FX_STAGING_HMAC` and
`FX_STAGING_MONITOR_ENDPOINT` (HTTPS `/v1/heartbeat`, no userinfo/query/fragment).
Use the existing EnvironmentSecrets interface for later restricted process
environment injection. A Windows Credential Manager/provider adapter is a future
option, not implemented here. Presence is not proof of a matching Monitor key.

Do not paste values into commands, chat, JSON, task XML, shell history or transcripts.
Provision via an approved private secret-management UI/provider, outside this repo,
under the actual run identity. This package does not generate or register real values.
Enroll matching collector/key/boot identity on the independent Monitor separately;
do not reset sequence under a used boot or run two senders with the same identity.

LINE/Email belong on the independent Monitor; their variable-name-only reference
template is `monitor.env.example`. Broker/Google references in
`optional-connectors.env.example` are reserved, not consumed by this package and not
required by preflight. Do not copy unused notification or Google credentials to the
Collector. Concrete connector wiring and account permissions need separate review.

## MT5 x1 and startup decision

Place one operator-approved installation under the selected terminal directory, or
explicitly configure an existing terminal64.exe path. Preflight checks existence only,
not publisher signature, broker login or process identity. No install/start/login is
automatic. `TerminalAdapter.process_alive(Terminal) -> bool | None` remains the process
probe contract. Match the configured executable path and terminal ID, not just any
process named terminal64.exe. Unknown status must remain unknown; do not synthesize
healthy tick/write timestamps. Real probe integration remains a gate.

| Candidate | Suitability for first MT5 GUI staging |
|---|---|
| Task Scheduler | Recommended after integration: dedicated least-privilege interactive identity, logon trigger, single instance, reviewed restart/backoff |
| Windows Service | Suitable for a future headless sender/probe; MT5 interactive session behavior needs separate validation |

For the first GUI trial, use an interactive-user logon task rather than assuming a
LocalService boot task can operate MT5. After reboot, automatic continuation requires
the designated interactive session to exist; **no automatic login is configured**.
Unattended reboot recovery remains an acceptance gate. An existing
`CollectorTask.write_new` can generate disabled LocalService boot XML for a reviewed
headless Python entry point, but that identity is not the recommended GUI MT5 task.

Later Task Scheduler steps: select the reviewed real sender entry point; use
`app/.venv/Scripts/python.exe`, explicit config and working directory `app`; dedicated
interactive run identity; logon trigger; IgnoreNew; bounded restart; initially disabled.
Never put a password in XML or CLI. Review XML/identity/session behavior before separate
manual registration and enabling. No real entry command is fabricated in Phase 4A.
Routine shutdown must flush durable sender/writer state before task termination;
forced termination is reserved for approved fault drills.

## Logging and rollback

`StagingLogs` provides collector/watchdog/heartbeat/error files, UTC timestamps,
5 MiB rotation and five backups per file. It accepts only fixed Event enum values,
rejects arbitrary message strings and writes errors to error.log too. Existing
third-party/root loggers are not redirected into it. Wire real runtime callbacks to
this restricted interface when implementing the collector. Use one process owner per
log directory; rotation is not multi-process safe. Restrict directory ACLs and review
retention. Files are created lazily on the first event.
For an explicit local-only logging smoke, add `--write-logs` to either dry-run command.
Ordinary dry-run does not create files. Preflight currently prints its report; it
does not silently activate application logging or dump environment variables.

Rollback: stop the newly introduced process/task only after recording its exact
identity and reviewed flush procedure; do not stop unrelated MT5 installations.
Disable only the new task if one was separately registered. Preserve config, logs,
venv and state for diagnosis. Switch to a separately prepared previously reviewed
checkout/venv and a verified separate-path state backup; never overwrite the only DB
or delete history. Do not use reset --hard, git clean, recursive root deletion,
uninstall MT5, or change firewall/RDP to undo this package. Bootstrap performs no OS
settings changes to roll back. Backup restore/migration must retain sender identity
and monotonic sequence, or use separately enrolled boot identity.

## Phase 4B handoff

Provide non-secret OS/build/architecture, region, chosen deployment root, Python/Git
versions, disk capacity, collector/terminal logical IDs, broker name, symbol list,
terminal path and independent Monitor architecture/HTTPS hostname via the approved
channel. Address/RDP account details should use the private connection workflow,
not a pasted credential bundle. Never paste Administrator/RDP passwords, broker login
credentials, HMAC keys, LINE tokens, Email passwords, Google JSON/tokens or private keys
into chat. No such information is needed to finish Phase 4A.

Phase 4B order: (1) review/implement real collector and durable sender gates locally;
(2) separately authorize VPS access; (3) inventory existing host without modifications;
(4) clone reviewed commit at new root, bootstrap and offline tests; (5) run preflight;
(6) operator places one MT5 and privately provisions required identities/secrets;
(7) approve Monitor connectivity check; (8) supervised foreground startup with genuine
tick/write evidence; (9) review/register startup task separately; (10) rehearse reboot,
collector/MT5 stop, timeout, recovery, Monitor failure and separate-path restore;
(11) complete 24-hour/7-day checklist before wider deployment. No production
distribution is enabled during these steps.
