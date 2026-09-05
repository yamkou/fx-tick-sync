[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$CollectorId = 'london-01',
    [string]$TerminalId = 'icmarkets-01',
    [string]$Broker = 'example-broker',
    [switch]$InstallDependencies
)
$ErrorActionPreference = 'Stop'
$DeploymentRoot = [IO.Path]::GetFullPath($Root)
$AppRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if ($AppRoot -ne (Join-Path $DeploymentRoot 'app')) { throw 'Clone must be under selected root/app.' }
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 required.' }
foreach ($LogicalId in @($CollectorId, $TerminalId, $Broker)) {
    if ($LogicalId.Length -gt 63 -or $LogicalId -cnotmatch '^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$') { throw 'Invalid logical ID.' }
}
Push-Location -LiteralPath $AppRoot
try {
    @'
import sys,struct
sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 else 1)
'@ | & $Python -B -
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 required.' }
    $VenvPython = Join-Path $AppRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath (Join-Path $AppRoot '.venv'))) {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'venv creation failed.' }
    }
    # Preserve an existing venv; verify it before any optional install.
    @'
import sys,pathlib
sys.exit(0 if sys.version_info[:2]==(3,12) and pathlib.Path(sys.prefix).resolve()==pathlib.Path('.venv').resolve() and sys.prefix!=sys.base_prefix and 'include-system-site-packages = false' in pathlib.Path('.venv/pyvenv.cfg').read_text() else 1)
'@ | & $VenvPython -B -
    if ($LASTEXITCODE -ne 0) { throw 'Existing venv needs operator inspection; no recreation performed.' }
    if ($InstallDependencies) {
        & $VenvPython -m pip --isolated --disable-pip-version-check install --only-binary=:all: --index-url https://pypi.org/simple -r requirements-dev.txt -c constraints-windows-py312.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency install failed.' }
    }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency failed.' }
    $ConfigPath = Join-Path $DeploymentRoot 'config/collector.staging.json'
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        & $VenvPython -B -m fxtick.staging init --root $DeploymentRoot --template deployment/windows-staging/collector.template.json --collector-id $CollectorId --terminal-id $TerminalId --broker $Broker
        if ($LASTEXITCODE -ne 0) { throw 'Staging layout initialization failed; retained for inspection.' }
    }
    & $VenvPython -B -m fxtick.staging dry-run --config $ConfigPath --component collector
    if ($LASTEXITCODE -ne 0) { throw 'Staging configuration invalid.' }
    $RuntimePath = Join-Path $DeploymentRoot 'config/runtime.staging.json'
    if (-not (Test-Path -LiteralPath $RuntimePath)) {
        # Exclusive creation: never overwrite an existing reviewed boot/key selection.
        $RuntimeStream = [IO.File]::Open($RuntimePath, [IO.FileMode]::CreateNew)
        try {
            $RuntimeBytes = [IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'runtime.template.json'))
            $RuntimeStream.Write($RuntimeBytes, 0, $RuntimeBytes.Length)
        } finally { $RuntimeStream.Dispose() }
    }
    & $VenvPython -B -m fxtick.collector --config $ConfigPath --runtime $RuntimePath --dry-run
    if ($LASTEXITCODE -ne 0) { throw 'Collector runtime configuration invalid.' }
    Write-Output 'Preparation finished. No collector, heartbeat, terminal or service started. Run preflight next.'
} finally {
    Pop-Location
}
