[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("on", "enable", "off", "disable", "status", "start", "stop")]
    [string]$Action
)

$RepoRoot = "<path-to-el-jev>"
$env:PYTHONPATH = "$RepoRoot;" + $env:PYTHONPATH
$env:ELJEV_COHERE_ENDPOINT = "https://<your-resource>.services.ai.azure.com"
$env:ELJEV_COHERE_DEPLOYMENT = "Cohere-rerank-v4.0-pro"

switch ($Action) {
    { $_ -in "on", "enable" } {
        python -m eljev on
    }
    { $_ -in "off", "disable" } {
        python -m eljev off
    }
    "status" {
        python -m eljev status
    }
    "start" {
        python -m eljev daemon start
    }
    "stop" {
        python -m eljev daemon stop
    }
}
