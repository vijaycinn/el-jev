[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [int]$Limit = 10,
    [string]$LiveGate = "",
    [switch]$AzureMetrics,
    [switch]$OpenChart
)

$ErrorActionPreference = "Stop"

if ($Limit -lt 1) {
    throw "Limit must be a positive integer."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) {
    "$RepoRoot$([IO.Path]::PathSeparator)$env:PYTHONPATH"
} else {
    $RepoRoot
}
$env:ELJEV_REPO_ROOT = $RepoRoot

function Write-TelemetryHeader {
    param([string]$Title)
    Write-Host ""
    Write-Host $Title -ForegroundColor Cyan
}

function Format-OptionalValue {
    param($Value)

    if ($null -eq $Value) {
        return "-"
    }

    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) {
        return "-"
    }

    return $text
}

function Show-LiveGateProbe {
    param([string]$StateText)

    if ([string]::IsNullOrWhiteSpace($StateText)) {
        return
    }

    Write-TelemetryHeader "Live Gate Probe"
    $gateOutput = & python -m eljev gate --state-text $StateText 2>&1
    $gateExitCode = $LASTEXITCODE
    $gateText = ($gateOutput | ForEach-Object { "$_" }) -join [Environment]::NewLine

    try {
        $gate = $gateText | ConvertFrom-Json -ErrorAction Stop
        Write-Host ("tier: {0}" -f (Format-OptionalValue -Value $gate.tier))
        Write-Host ("intent: {0}" -f (Format-OptionalValue -Value $gate.intent))
        Write-Host ("needs_deep_analysis: {0}" -f (Format-OptionalValue -Value $gate.needs_deep_analysis))
        Write-Host ("elapsed_ms: {0}" -f (Format-OptionalValue -Value $gate.elapsed_ms))
    } catch {
        Write-Warning "Gate output was not valid JSON (exit code $gateExitCode)."
        if (-not [string]::IsNullOrWhiteSpace($gateText)) {
            Write-Host $gateText
        }
    }
}

function Get-ResourceNameFromEndpoint {
    param([string]$Endpoint)

    if ([string]::IsNullOrWhiteSpace($Endpoint)) {
        return $null
    }

    try {
        $uri = [Uri]$Endpoint
    } catch {
        return $null
    }

    $endpointHost = $uri.Host
    if ([string]::IsNullOrWhiteSpace($endpointHost)) {
        return $null
    }

    if ($endpointHost -notmatch '^(?<resource>[^.]+)\.services\.ai\.azure\.com$') {
        return $null
    }

    return $Matches["resource"]
}

function Show-AzureMetrics {
    param([string]$RootPath)

    Write-TelemetryHeader "Azure Monitor Metrics (last 24h)"

    $az = Get-Command -Name az -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $az) {
        Write-Warning "Azure CLI (az) was not found on PATH."
        return
    }

    $configPath = Join-Path $RootPath ".eljev\config.json"
    if (-not (Test-Path -LiteralPath $configPath)) {
        Write-Warning "Config file not found at $configPath"
        return
    }

    try {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        Write-Warning "Failed to read config from ${configPath}: $($_.Exception.Message)"
        return
    }

    $resourceNames = @(
        Get-ResourceNameFromEndpoint -Endpoint $config.md1_endpoint
        Get-ResourceNameFromEndpoint -Endpoint $config.cohere_endpoint
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique

    if (-not $resourceNames -or $resourceNames.Count -eq 0) {
        Write-Warning "No Foundry resource name could be extracted from md1/cohere endpoints."
        return
    }

    $subscription = ""
    if ($config.PSObject.Properties.Name -contains "subscription" -and -not [string]::IsNullOrWhiteSpace($config.subscription)) {
        $subscription = [string]$config.subscription
    }
    $subArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($subscription)) {
        $subArgs = @("--subscription", $subscription)
    }

    $start = (Get-Date).ToUniversalTime().AddHours(-24).ToString("o")
    $end = (Get-Date).ToUniversalTime().ToString("o")
    $metrics = @("TotalCalls", "SuccessCalls", "Latency", "ProcessedPromptTokens", "GeneratedTokens")
    $metricCsv = [string]::Join(",", $metrics)

    foreach ($resourceName in $resourceNames) {
        Write-Host ""
        Write-Host ("Resource: {0}" -f $resourceName) -ForegroundColor Yellow

        $resourceId = & az resource list `
            --name $resourceName `
            --resource-type "Microsoft.CognitiveServices/accounts" `
            --query "[0].id" `
            -o tsv `
            @subArgs 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($resourceId)) {
            Write-Warning "Could not resolve resource ID for $resourceName."
            continue
        }

        $metricJson = & az monitor metrics list `
            --resource $resourceId `
            --metric $metricCsv `
            --interval "PT1H" `
            --aggregation "Total" "Average" `
            --start-time $start `
            --end-time $end `
            -o json `
            @subArgs 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($metricJson)) {
            Write-Warning "Metric query failed for resource $resourceName."
            continue
        }

        try {
            $metricPayload = $metricJson | ConvertFrom-Json -ErrorAction Stop
        } catch {
            Write-Warning "Metric payload was not valid JSON for $resourceName."
            continue
        }

        $rows = New-Object System.Collections.Generic.List[object]
        foreach ($metric in @($metricPayload.value)) {
            if (-not $metric) {
                continue
            }

            $allPoints = New-Object System.Collections.Generic.List[object]
            foreach ($series in @($metric.timeseries)) {
                foreach ($point in @($series.data)) {
                    if ($point) {
                        [void]$allPoints.Add($point)
                    }
                }
            }

            $totals = @($allPoints | ForEach-Object { $_.total } | Where-Object { $_ -ne $null })
            $averages = @($allPoints | ForEach-Object { $_.average } | Where-Object { $_ -ne $null })

            $sumTotal = "-"
            if ($totals.Count -gt 0) {
                $sumTotal = [Math]::Round((($totals | Measure-Object -Sum).Sum), 3)
            }

            $avgValue = "-"
            if ($averages.Count -gt 0) {
                $avgValue = [Math]::Round((($averages | Measure-Object -Average).Average), 3)
            }

            [void]$rows.Add([PSCustomObject]@{
                Metric   = $metric.name.value
                Total24h = $sumTotal
                Avg24h   = $avgValue
            })
        }

        if ($rows.Count -eq 0) {
            Write-Warning "No metric datapoints were returned for $resourceName."
            continue
        }

        $rows | Format-Table -AutoSize | Out-String | Write-Host
    }
}

function Show-OpenChart {
    param([string]$RootPath)

    $chartPath = Join-Path $RootPath "docs\md1\telemetry-savings.html"
    if (-not (Test-Path -LiteralPath $chartPath)) {
        Write-Warning "Chart file not found at $chartPath"
        return
    }

    Start-Process -FilePath $chartPath | Out-Null
    Write-Host "Opened $chartPath"
}

Show-LiveGateProbe -StateText $LiveGate

$telemetryPython = @'
import contextlib
import io
import json
import math
import os
import pathlib
import statistics
import sys
from typing import Any

from eljev import cli
from eljev.client import EljevClient


def read_status() -> dict[str, Any]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli._status(default_engine="md1")
    raw = buffer.getvalue().strip()
    if not raw:
        return {}
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    candidate = lines[-1] if lines else raw
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            payload = line.strip()
            if not payload:
                continue
            try:
                item = json.loads(payload)
            except Exception:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(math.ceil((p / 100.0) * len(ordered)) - 1)))
    return ordered[idx]


def fmt_num(value: float | int | None, ndigits: int = 1) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{ndigits}f}"
    except Exception:
        return "-"


def decision_elapsed_ms(record: dict[str, Any]) -> float | None:
    elapsed = record.get("elapsed_ms")
    if isinstance(elapsed, dict):
        total = elapsed.get("total")
        if isinstance(total, (int, float)):
            return float(total)
        return None
    if isinstance(elapsed, (int, float)):
        return float(elapsed)
    return None


def clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def as_text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def to_utc_text(ts: Any) -> str:
    text = as_text(ts, "-")
    if text == "-":
        return text
    try:
        dt = text.replace("Z", "+00:00")
        parsed = __import__("datetime").datetime.fromisoformat(dt)
        return parsed.astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return text


def print_row(cells: list[str], widths: list[int]) -> None:
    padded = [cells[i].ljust(widths[i]) for i in range(len(widths))]
    print(" | ".join(padded))


limit = 10
if len(sys.argv) > 1:
    try:
        limit = max(1, int(sys.argv[1]))
    except Exception:
        limit = 10

status = read_status()
health = EljevClient().health()

repo_root = pathlib.Path(os.environ.get("ELJEV_REPO_ROOT", ".")).resolve()
log_dir = repo_root / ".eljev" / "logs"
decision_records = read_jsonl(log_dir / "decisions.jsonl")
hook_records = read_jsonl(log_dir / "hook.jsonl")

daemon = status.get("daemon") if isinstance(status.get("daemon"), dict) else {}

print("Section 1: Daemon & Engine Status")
section_one_rows = [
    ("engine", status.get("engine")),
    ("md1_deployment", status.get("md1_deployment")),
    ("cohere_deployment", status.get("cohere_deployment")),
    ("extension_mode", status.get("extension_mode")),
    ("coverage_policy", status.get("coverage_policy")),
    ("warm", health.get("warm", daemon.get("warm")) if isinstance(health, dict) else daemon.get("warm")),
    ("pid", health.get("pid", daemon.get("pid")) if isinstance(health, dict) else daemon.get("pid")),
]
for key, value in section_one_rows:
    print(f"  {key:18}: {as_text(value)}")

print("")
print("Section 2: Engine Latency & Volume Chart")
engines = ["Decision-1", "Cohere-rerank-v4.0-pro"]
latencies: dict[str, list[float]] = {engine: [] for engine in engines}
for record in decision_records:
    engine = record.get("engine")
    if engine not in latencies:
        continue
    elapsed = decision_elapsed_ms(record)
    if elapsed is None:
        continue
    latencies[engine].append(elapsed)

counts = {engine: len(values) for engine, values in latencies.items()}
max_count = max(counts.values()) if counts else 0
for engine in engines:
    count = counts.get(engine, 0)
    if count <= 0 or max_count <= 0:
        bar = ""
    else:
        bar_length = max(1, int(round((count / max_count) * 30)))
        bar = "#" * bar_length
    print(f"  {engine:32} | {bar:<30} {count}")

print("")
print("  Engine                           | Count | p50 ms | p95 ms | min ms | max ms")
print("  ---------------------------------|-------|--------|--------|--------|--------")
for engine in engines:
    values = latencies[engine]
    p50 = pct(values, 50)
    p95 = pct(values, 95)
    min_v = min(values) if values else None
    max_v = max(values) if values else None
    print(
        "  "
        + f"{engine:32} | "
        + f"{str(len(values)).rjust(5)} | "
        + f"{fmt_num(p50, 1).rjust(6)} | "
        + f"{fmt_num(p95, 1).rjust(6)} | "
        + f"{fmt_num(min_v, 1).rjust(6)} | "
        + f"{fmt_num(max_v, 1).rjust(6)}"
    )

print("")
print("Section 3: Hook Telemetry (hook.jsonl)")
hook_total = len(hook_records)
hook_injected = sum(1 for row in hook_records if row.get("event") == "decision")
hook_fail_open = sum(1 for row in hook_records if row.get("event") == "fail_open")
hook_elapsed = [float(row.get("elapsed_ms")) for row in hook_records if isinstance(row.get("elapsed_ms"), (int, float))]
hook_median = statistics.median(hook_elapsed) if hook_elapsed else None
print(f"  total hook runs         : {hook_total}")
print(f"  decisions injected      : {hook_injected}")
print(f"  fail-open count         : {hook_fail_open}")
print(f"  median hook elapsed_ms  : {fmt_num(hook_median, 1)}")

print("")
print(f"Section 4: Last {limit} Decisions")
print("UTC Time | Engine | Question | Kind | Choice | Conf | Status | ms")

decision_rows = [row for row in decision_records if row.get("schema") == "eljev.decision/1"]
tail = decision_rows[-limit:] if limit > 0 else []

widths = [19, 24, 24, 8, 18, 7, 14, 8]
separator = "-+-".join("-" * w for w in widths)
print(separator)
for row in tail:
    elapsed = decision_elapsed_ms(row)
    question = row.get("question_id") or row.get("criterion") or "-"
    confidence = row.get("confidence")
    if isinstance(confidence, (int, float)):
        conf_text = f"{confidence:.3f}"
    else:
        conf_text = "-"
    cells = [
        clip(to_utc_text(row.get("ts")), widths[0]),
        clip(as_text(row.get("engine")), widths[1]),
        clip(as_text(question), widths[2]),
        clip(as_text(row.get("kind")), widths[3]),
        clip(as_text(row.get("choice")), widths[4]),
        clip(conf_text, widths[5]),
        clip(as_text(row.get("status")), widths[6]),
        clip(fmt_num(elapsed, 1), widths[7]),
    ]
    print_row(cells, widths)
'@

$telemetryOutput = $telemetryPython | python - $Limit 2>&1
$telemetryExitCode = $LASTEXITCODE
$telemetryText = ($telemetryOutput | ForEach-Object { "$_" }) -join [Environment]::NewLine

if ($telemetryText) {
    Write-Host $telemetryText
}

if ($telemetryExitCode -ne 0) {
    exit $telemetryExitCode
}

if ($AzureMetrics) {
    Show-AzureMetrics -RootPath $RepoRoot
}

if ($OpenChart) {
    Show-OpenChart -RootPath $RepoRoot
}

exit 0
