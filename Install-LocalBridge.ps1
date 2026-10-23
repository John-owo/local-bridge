param(
    [string]$Package = (Join-Path $PSScriptRoot 'local-bridge'),
    [string]$UserRoot = $env:USERPROFILE,
    # Runs and worker state go here; put it on the drive that should take the (small) write load.
    [string]$DataRoot = (Join-Path $env:USERPROFILE 'Documents\Codex'),
    # Default backend profile: 'qwen27b' (llama-server) or 'flash-next' (Strata).
    [ValidateSet('qwen27b', 'flash-next')]
    [string]$Profile = 'qwen27b',
    # qwen27b: llama-server from LM Studio's Vulkan runtime and its vendor DLL folder, plus the GGUF model.
    [string]$LlamaRuntime = (Join-Path $env:USERPROFILE '.lmstudio\extensions\backends\llama.cpp-win-x86_64-vulkan-avx2-2.37.0'),
    [string]$LlamaVendor = (Join-Path $env:USERPROFILE '.lmstudio\extensions\backends\vendor\win-llama-vulkan-vendor-v2'),
    [string]$Model = 'E:\LLM\models\unsloth\Qwen3.8-27B-GGUF\Qwen3.8-27B-UD-IQ3_XXS.gguf',
    # flash-next: Strata's command and `strata launch` arguments. Mapped experts with a 12 GiB RAM tier fit next to
    # a normal desktop in 40 GB; the full in-RAM arena (about 23 GiB for Coder IQ1_M) does not.
    [string]$Strata = (Join-Path $env:LOCALAPPDATA 'Strata\terminal\current\bin\strata.cmd'),
    # Where Strata keeps its model files (about 63 GB); without it Strata defaults to Documents on C:.
    [string]$StrataDataDir = 'E:\Strata-data',
    [string[]]$StrataLaunchArgs = @('--family', 'coder', '--model', 'iq1_m', '--context', '32k', '--vision', 'off', '--access', 'local', '--port', '8080',
                                    '--set', 'mmap-experts', '--set', 'resident-budget-gib=12')
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
$LlamaServer = Join-Path $LlamaRuntime 'llama-server.exe'
if (-not (Test-Path -LiteralPath $LlamaServer)) { Write-Warning "llama-server not found at $LlamaServer; the qwen27b profile cannot start its server until the config is fixed." }
if (-not (Test-Path -LiteralPath $Model)) { Write-Warning "Model not found at $Model." }
if (-not (Test-Path -LiteralPath $Strata)) { Write-Warning "strata command not found at $Strata; the flash-next profile needs Strata installed first." }

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
    profile   = $Profile
    runs_root = $RunsRoot
    state_dir = $StateDir
    profiles  = [ordered]@{
        qwen27b      = [ordered]@{
            endpoint = 'http://127.0.0.1:1234/v1'
            model_id = 'qwen3.8-27b'
            context  = 32768
            output   = 4096
            # Measured under agent load on 40 GB RAM: --load-mode none keeps the model out of the file cache,
            # --cache-ram 0 stops the host prompt cache (default 8 GiB) and -np 1 drops three idle slots' host
            # buffers. Together: RAM working set 17.8 -> 5.7 GB, free RAM 2.7 -> 14.8 GB, same speed. One slot
            # means concurrent dispatches queue instead of splitting the 32K context.
            server   = [ordered]@{
                kind         = 'process'
                exe          = $LlamaServer
                path_prepend = @($LlamaVendor, $LlamaRuntime)
                args         = @('-m', $Model, '-a', 'qwen3.8-27b', '-ngl', '99', '-c', '32768', '-fa', 'auto', '--jinja',
                                 '--chat-template-kwargs', '{"enable_thinking":false}', '--load-mode', 'none', '--cache-ram', '0', '-np', '1',
                                 '--host', '127.0.0.1', '--port', '1234')
            }
        }
        'flash-next' = [ordered]@{
            endpoint = 'http://127.0.0.1:8080/v1'
            model_id = 'strata'
            context  = 32768
            output   = 8192
            server   = [ordered]@{ kind = 'strata'; strata = $Strata; launch_args = @($StrataLaunchArgs + @('--data-dir', $StrataDataDir)) }
        }
    }
}
$ConfigPath = Join-Path $Canonical 'local-bridge.config.json'
[IO.File]::WriteAllText($ConfigPath, ($Config | ConvertTo-Json -Depth 6), $Utf8)
[pscustomobject]@{
    skill        = $Canonical
    claude       = $Claude
    claudeTarget = (Get-Item -LiteralPath $Claude).Target
    config       = $ConfigPath
    profile      = $Profile
    runsRoot     = $RunsRoot
    stateDir     = $StateDir
} | ConvertTo-Json -Depth 4
