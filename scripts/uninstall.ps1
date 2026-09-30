[CmdletBinding()]
param(
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$CopilotHome = if ($env:COPILOT_HOME) {
    [IO.Path]::GetFullPath($env:COPILOT_HOME)
} else {
    [IO.Path]::GetFullPath((Join-Path $HOME ".copilot"))
}
$ManifestTarget = Join-Path $CopilotHome "eljev-install-manifest.json"
$Mode = if ($Apply) { "APPLY" } else { "DRY-RUN" }

Write-Host "el-jev uninstaller [$Mode]"
Write-Host "Copilot home: $CopilotHome"

function Write-Plan {
    param([string]$Message)
    Write-Host "[$Mode] $Message"
}

if (-not (Test-Path -LiteralPath $ManifestTarget -PathType Leaf)) {
    Write-Host "No install manifest found: $ManifestTarget"
    Write-Host "Nothing was changed."
    exit 0
}

$manifest = Get-Content -LiteralPath $ManifestTarget -Raw | ConvertFrom-Json
foreach ($entry in @($manifest.files)) {
    $path = [string]$entry.path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-Plan "Already absent: $path"
        continue
    }
    $currentHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ($currentHash -ne [string]$entry.sha256) {
        Write-Host "Leave modified file: $path"
        continue
    }
    Write-Plan "Remove unchanged installed file: $path"
    if ($Apply) {
        Remove-Item -LiteralPath $path
    }
}

$directories = @(
    (Join-Path $CopilotHome "hooks\eljev"),
    (Join-Path $CopilotHome "skills\el-jev")
)
foreach ($directory in $directories) {
    if (Test-Path -LiteralPath $directory -PathType Container) {
        $children = @(Get-ChildItem -LiteralPath $directory -Force)
        if ($children.Count -eq 0) {
            Write-Plan "Remove empty directory: $directory"
            if ($Apply) {
                Remove-Item -LiteralPath $directory
            }
        }
    }
}

Write-Plan "Remove install manifest: $ManifestTarget"
if ($Apply) {
    Remove-Item -LiteralPath $ManifestTarget
} else {
    Write-Host "No files were changed. Re-run with -Apply to apply the printed removals."
}
