[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$InstallHook
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$SkillSource = Join-Path $RepoRoot "skill\el-jev"
$HookSource = Join-Path $RepoRoot "hooks\eljev_pre_turn.py"
$McpSource = Join-Path $RepoRoot "mcp\server.py"

$CopilotHome = if ($env:COPILOT_HOME) {
    [IO.Path]::GetFullPath($env:COPILOT_HOME)
} else {
    [IO.Path]::GetFullPath((Join-Path $HOME ".copilot"))
}
$SkillTarget = Join-Path $CopilotHome "skills\el-jev"
$McpTarget = Join-Path $CopilotHome "mcp-config.json"
$HookTargetDir = Join-Path $CopilotHome "hooks\eljev"
$HookTarget = Join-Path $HookTargetDir "eljev_pre_turn.py"
$HookConfigTarget = Join-Path $CopilotHome "hooks\eljev.json"
$ManifestTarget = Join-Path $CopilotHome "eljev-install-manifest.json"

$Mode = if ($Apply) { "APPLY" } else { "DRY-RUN" }
Write-Host "el-jev installer [$Mode]"
Write-Host "Repository: $RepoRoot"
Write-Host "Copilot home: $CopilotHome"
Write-Host ""

function Write-Plan {
    param([string]$Message)
    Write-Host "[$Mode] $Message"
}

function Add-ManifestEntry {
    param(
        [System.Collections.Generic.List[object]]$Entries,
        [string]$Path
    )
    if ($Apply -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        $Entries.Add([ordered]@{ path = $Path; sha256 = $hash })
    }
}

if (-not (Test-Path -LiteralPath $SkillSource -PathType Container)) {
    throw "Skill source is missing: $SkillSource"
}
if (-not (Test-Path -LiteralPath $HookSource -PathType Leaf)) {
    throw "Hook source is missing: $HookSource"
}
if (-not (Test-Path -LiteralPath $McpSource -PathType Leaf)) {
    throw "MCP server source is missing: $McpSource"
}

$ManifestEntries = [System.Collections.Generic.List[object]]::new()
Write-Host "Skill installation (additive; existing destination files are never overwritten):"
Get-ChildItem -LiteralPath $SkillSource -File -Recurse | ForEach-Object {
    $relative = $_.FullName.Substring($SkillSource.Length).TrimStart("\")
    $destination = Join-Path $SkillTarget $relative
    if (Test-Path -LiteralPath $destination -PathType Leaf) {
        Write-Plan "Leave existing skill file: $destination"
    } else {
        Write-Plan "Copy skill file: $($_.FullName) -> $destination"
        if ($Apply) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $destination
            Add-ManifestEntry -Entries $ManifestEntries -Path $destination
        }
    }
}

Write-Host ""
Write-Host "MCP registration (never written by this script):"
Write-Host "Target path for user review: $McpTarget"
$McpBlock = [ordered]@{
    mcpServers = [ordered]@{
        eljev = [ordered]@{
            command = "python"
            args = @($McpSource)
            env = [ordered]@{
                ELJEV_HOST = "127.0.0.1"
                ELJEV_PORT = "8787"
            }
        }
    }
}
Write-Host ($McpBlock | ConvertTo-Json -Depth 6)
Write-Host "No existing MCP config file will be edited."

Write-Host ""
if ($InstallHook) {
    Write-Host "Hook installation (explicit opt-in):"
    if (Test-Path -LiteralPath $HookTarget -PathType Leaf) {
        Write-Plan "Leave existing hook script: $HookTarget"
    } else {
        Write-Plan "Copy hook script: $HookSource -> $HookTarget"
        if ($Apply) {
            New-Item -ItemType Directory -Force -Path $HookTargetDir | Out-Null
            Copy-Item -LiteralPath $HookSource -Destination $HookTarget
            Add-ManifestEntry -Entries $ManifestEntries -Path $HookTarget
        }
    }

    $HookBlock = [ordered]@{
        version = 1
        hooks = [ordered]@{
            userPromptTransformed = @(
                [ordered]@{
                    type = "command"
                    exec = "python"
                    args = @($HookTarget)
                    timeoutSec = 0.25
                }
            )
        }
    }
    if (Test-Path -LiteralPath $HookConfigTarget -PathType Leaf) {
        Write-Plan "Leave existing hook config: $HookConfigTarget"
    } else {
        Write-Plan "Create additive hook config: $HookConfigTarget"
        Write-Host "Hook config block:"
        Write-Host ($HookBlock | ConvertTo-Json -Depth 8)
        if ($Apply) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $HookConfigTarget) | Out-Null
            $HookBlock | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $HookConfigTarget -Encoding utf8
            Add-ManifestEntry -Entries $ManifestEntries -Path $HookConfigTarget
        }
    }
    Write-Host "The hook remains off until ELJEV_HOOK_ENABLED=1 is set."
} else {
    Write-Host "Hook installation: not requested; default is OFF."
}

Write-Host ""
Write-Host "Daemon verification:"
$daemonModule = Join-Path $RepoRoot "eljev\daemon.py"
$healthUri = "http://127.0.0.1:8787/health"
$python = Get-Command python -ErrorAction SilentlyContinue
$daemonStartArgs = @("-c", "from eljev.daemon import run; run()")
if (-not (Test-Path -LiteralPath $daemonModule -PathType Leaf)) {
    Write-Host "NOT RUN: daemon entrypoint is not present yet ($daemonModule)."
    Write-Host "A1 owns eljev\daemon.py. The apply path will start it when that file exists."
} elseif ($null -eq $python) {
    Write-Host "NOT RUN: python was not found on PATH."
} elseif (-not $Apply) {
    Write-Plan "Start '$($python.Source) -c `"from eljev.daemon import run; run()`"' and GET $healthUri; dry-run does not start processes."
} else {
    $daemonProcess = $null
    $startedHere = $false
    try {
        $health = Invoke-RestMethod -Uri $healthUri -Method Get -TimeoutSec 2
        if ($health.status -eq "ok") {
            Write-Host "Health check passed against an already-running daemon: $healthUri"
        } else {
            throw "daemon returned status '$($health.status)'"
        }
    } catch {
        Write-Plan "Start '$($python.Source) -c `"from eljev.daemon import run; run()`"' for health verification."
        $daemonProcess = Start-Process -FilePath $python.Source -ArgumentList $daemonStartArgs -WorkingDirectory $RepoRoot -PassThru -WindowStyle Hidden
        $startedHere = $true
        $healthy = $false
        for ($attempt = 1; $attempt -le 20; $attempt++) {
            Start-Sleep -Milliseconds 100
            try {
                $health = Invoke-RestMethod -Uri $healthUri -Method Get -TimeoutSec 1
                if ($health.status -eq "ok") {
                    $healthy = $true
                    break
                }
            } catch {
                if ($daemonProcess.HasExited) {
                    break
                }
            }
        }
        if (-not $healthy) {
            throw "daemon did not return status ok from $healthUri"
        }
        Write-Host "Health check passed: $healthUri"
    } finally {
        if ($startedHere -and $null -ne $daemonProcess -and -not $daemonProcess.HasExited) {
            Stop-Process -Id $daemonProcess.Id
            Write-Host "Stopped temporary daemon process $($daemonProcess.Id)."
        }
    }
}

if ($Apply -and $ManifestEntries.Count -gt 0) {
    $manifest = [ordered]@{
        schema = "eljev.install/1"
        files = @($ManifestEntries)
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ManifestTarget -Encoding utf8
    Write-Host "Wrote install manifest: $ManifestTarget"
} elseif (-not $Apply) {
    Write-Host "No files were changed. Re-run with -Apply to apply the printed additive changes."
}
