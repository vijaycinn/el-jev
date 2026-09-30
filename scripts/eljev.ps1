[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("on", "enable", "off", "disable", "status", "start", "stop", "log")]
    [string]$Action
)

# Portable shortcut for `python -m eljev <action>` run from this checkout.
$RepoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$RepoRoot$([IO.Path]::PathSeparator)$env:PYTHONPATH" } else { $RepoRoot }

switch ($Action) {
    { $_ -in "on", "enable" } { python -m eljev on }
    { $_ -in "off", "disable" } { python -m eljev off }
    "status" { python -m eljev status }
    "start" { python -m eljev daemon start }
    "stop" { python -m eljev daemon stop }
    "log" { python -m eljev log }
}
exit $LASTEXITCODE
