[CmdletBinding()]
param(
    [switch]$NoJev,
    [switch]$WithJev,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CopilotArgs
)

# Portable launcher: starts Copilot CLI with el-jev forced off/on for this session only.
# Endpoint and deployment come from `python -m eljev configure` (config.json), not this script.
$RepoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$RepoRoot$([IO.Path]::PathSeparator)$env:PYTHONPATH" } else { $RepoRoot }

if ($NoJev) {
    $env:EL_JEV = "OFF"
    Write-Host "[el-jev] Decision hook disabled for this session." -ForegroundColor Yellow
} elseif ($WithJev) {
    $env:EL_JEV = "ON"
    Write-Host "[el-jev] Decision hook enabled for this session." -ForegroundColor Green
    python -m eljev daemon start | Out-Null
}

$Copilot = Get-Command -Name copilot -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Copilot) {
    Write-Error "copilot was not found on PATH."
    exit 1
}
& $Copilot.Source @CopilotArgs
exit $LASTEXITCODE
