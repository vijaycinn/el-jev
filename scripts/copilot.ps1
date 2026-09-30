[CmdletBinding()]
param(
    [switch]$NoJev,
    [switch]$WithJev,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CopilotArgs
)

$RepoRoot = "<path-to-el-jev>"
$env:PYTHONPATH = "$RepoRoot;" + $env:PYTHONPATH
$env:ELJEV_COHERE_ENDPOINT = "https://<your-resource>.services.ai.azure.com"
$env:ELJEV_COHERE_DEPLOYMENT = "Cohere-rerank-v4.0-pro"

if ($NoJev) {
    $env:ELJEV_ENABLED = "0"
    Write-Host "[el-jev] Automatic decision routing disabled for this session." -ForegroundColor Yellow
} elseif ($WithJev) {
    $env:ELJEV_ENABLED = "1"
    Write-Host "[el-jev] Automatic decision routing enabled." -ForegroundColor Green
    python -m eljev on | Out-Null
}

$CopilotExe = "copilot"
if (Test-Path $CopilotExe) {
    & $CopilotExe @CopilotArgs
} else {
    $cmd = Get-Command -CommandType Application copilot | Select-Object -First 1 -ExpandProperty Source
    & $cmd @CopilotArgs
}
