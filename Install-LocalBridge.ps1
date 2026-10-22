param(
    [string]$Package = (Join-Path $PSScriptRoot 'local-bridge'),
    [string]$UserRoot = $env:USERPROFILE,
    # Runs and worker state go here; put it on the drive that should take the (small) write load.
    [string]$DataRoot = (Join-Path $env:USERPROFILE 'Documents\Codex'),
    [string]$Endpoint = 'http://127.0.0.1:8080/v1',
    [string]$Strata = (Join-Path $env:LOCALAPPDATA 'Strata\terminal\current\bin\strata.cmd'),
    # Arguments for `strata launch` used by `local_bridge.py server start`. Mapped experts with a RAM budget:
    # the full in-RAM arena (about 23 GiB for Coder IQ1_M) does not fit next to a normal desktop in 40 GB.
    [string[]]$LaunchArgs = @('--family', 'coder', '--model', 'iq1_m', '--context', '32k', '--vision', 'off', '--access', 'local', '--port', '8080',
                              '--set', 'mmap-experts', '--set', 'resident-budget-gib=12'),
    [int]$Context = 32768
)
$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$Canonical = Join-Path $UserRoot '.agents\skills\local-bridge'
$Claude = Join-Path $UserRoot '.claude\skills\local-bridge'

if (-not (Test-Path -LiteralPath (Join-Path $Package 'SKILL.md'))) { throw 'Skill package missing SKILL.md.' }
if (-not (Test-Path -LiteralPath (Join-Path $Package 'scripts\local_bridge.py'))) { throw 'Skill package missing helper.' }
$Package = (Resolve-Path -LiteralPath $Package).Path
if (Test-Path -LiteralPath $Canonical) { throw "Skill already exists at $Canonical; inspect before updating." }
if (Test-Path -LiteralPath $Claude) { throw "Claude skill already exists at $Claude; inspect before updating." }
if (-not (Test-Path -LiteralPath $Strata)) { Write-Warning "strata command not found at $Strata; server start/stop will be unavailable until the config is fixed." }

New-Item -ItemType Directory -Path (Split-Path $Canonical -Parent) -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path $Claude -Parent) -Force | Out-Null
Copy-Item -LiteralPath $Package -Destination $Canonical -Recurse
Get-ChildItem -LiteralPath $Canonical -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
try {
    New-Item -ItemType Junction -Path $Claude -Target $Canonical | Out-Null
} catch {
    throw "Canonical skill installed at $Canonical, but CC junction failed: $($_.Exception.Message)"
}

$RunsRoot = Join-Path $DataRoot 'local-bridge-runs'
$StateDir = Join-Path $DataRoot 'local-bridge-state'
New-Item -ItemType Directory -Path $RunsRoot, $StateDir -Force | Out-Null
$Config = [ordered]@{
    endpoint    = $Endpoint
    model_id    = 'strata'
    context     = $Context
    output      = 8192
    runs_root   = $RunsRoot
    state_dir   = $StateDir
    strata      = $Strata
    launch_args = $LaunchArgs
}
$ConfigPath = Join-Path $Canonical 'local-bridge.config.json'
[IO.File]::WriteAllText($ConfigPath, ($Config | ConvertTo-Json -Depth 4), $Utf8)
[pscustomobject]@{
    skill        = $Canonical
    claude       = $Claude
    claudeTarget = (Get-Item -LiteralPath $Claude).Target
    config       = $ConfigPath
    runsRoot     = $RunsRoot
    stateDir     = $StateDir
} | ConvertTo-Json -Depth 4
