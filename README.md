# el-jev

`el-jev` is a fast typed-decision sidecar and Copilot CLI pre-turn hook that mimics TypeSafe AI Jev decision primitives (**choice**, **noul**, **score**) on Azure AI Foundry Cohere. It is designed for auditable abstention first: by default, non-trivial decisions stay advisory until calibration is fitted on labelled data.

## Architecture

- 🌐 [Interactive system schematic](docs/architecture.html)
- 🌐 [Interactive decisioning flow](docs/decisioning-flow.html)
- 📖 [Architecture deep dive](docs/architecture.md)

<p align="center">
  <img src="docs/architecture-schematic.png" alt="el-jev architecture schematic" width="100%">
</p>

<p align="center">
  <img src="docs/decisioning-flow.png" alt="el-jev decisioning flow" width="100%">
</p>

## What changed in v0.2.0

- Added unified runtime config resolution (`env > config.json > defaults`) with repo-local state in `.eljev/`.
- Added `eljev configure`, `install-hook`, `uninstall-hook`, `on`, `off`, and richer daemon lifecycle commands.
- Added built-in Shape A Cohere-backed `choice | noul | score` decisions with fail-closed engine handling.
- Added single shared gate (`apply_gate`) for all shapes with calibration-driven thresholds.
- Hardened loopback daemon against browser/DNS-rebinding style abuse (`Origin`, `Host`, `Content-Type` checks).

See [docs/v2-plan.md](docs/v2-plan.md) and [CHANGELOG.md](CHANGELOG.md) for details.

## Measured latency (single machine)

These are single-machine measurements from 2026-09-24 and 2026-09-30, not cross-environment SLAs.

| Measurement | Result |
|---|---:|
| Cohere rerank warm p50 (common flows) | ~175-183 ms |
| Reused connection mean vs fresh connection mean | 245 ms vs 764 ms |
| Warm daemon loopback round trip | ~1.8 ms |
| Cold CLI process startup path | ~700 ms |
| Hook, end to end per Copilot turn via the Windows PowerShell hook runner (v0.2.0, 2026-09-30) | ~1.4-1.6 s |
| - of which PowerShell start for the hook command | ~0.65 s |
| - of which Python interpreter start for the hook process | ~0.55 s |
| - of which intent decision in the warm daemon | ~0.15-0.35 s typical |
| Cohere tail latency above the 500 ms hook budget | observed (e.g. 685 ms); that turn fails open |
| First decision after daemon start | ~0.7 s; exceeds the 500 ms hook budget, so that turn fails open |

The hook runs as a new process on every Copilot turn, so process start-up, not the decision, is most of its cost. On this machine the hook adds roughly 1.5 s per turn. Use `hook_mode=marker` (only explicit requests) or `EL_JEV=OFF` when that per-turn cost is not worth the advisory, and raise `ELJEV_HOOK_TIMEOUT_MS` if you prefer fewer fail-opens over lower worst-case latency.

## Azure prerequisites

1. Create or use an Azure AI Foundry / AI Services resource.
2. Deploy `Cohere-rerank-v4.0-pro` (or `Cohere-rerank-v4.0-fast`).
3. Use keyless auth (Entra token scope `https://ai.azure.com/.default`).
4. Assign **Cognitive Services User** at resource scope.

```powershell
az role assignment create `
  --role "Cognitive Services User" `
  --assignee "<user-or-principal-id>" `
  --scope "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<your-resource>"
```

```bash
az role assignment create \
  --role "Cognitive Services User" \
  --assignee "<user-or-principal-id>" \
  --scope "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<your-resource>"
```

Managed identity is supported for VM/App Service/Container App. Set `--auth managed_identity` and set `AZURE_CLIENT_ID` when using a user-assigned identity. Role assignment propagation can take about five minutes.

If you are signed in to more than one Azure account, pin the subscription that owns the Foundry resource so tokens always come from the right account, whatever your `az` default is: `python -m eljev configure --subscription <subscription-id>`. Without it, a different default account yields `400 Token tenant ... does not match resource tenant`.

## Quick start

```powershell
git clone https://github.com/vijaycinn/el-jev.git
Set-Location .\el-jev
python -m pip install -e .
az login
python -m eljev configure --endpoint https://<your-resource>.services.ai.azure.com
python -m eljev daemon start
python -m eljev status
python -m eljev install-hook --scope user
# Repo-scoped hook alternative:
# python -m eljev install-hook --scope repo --repo <path-to-repo>
```

```bash
git clone https://github.com/vijaycinn/el-jev.git
cd el-jev
python -m pip install -e .
az login
python -m eljev configure --endpoint https://<your-resource>.services.ai.azure.com
python -m eljev daemon start
python -m eljev status
python -m eljev install-hook --scope user
# Repo-scoped hook alternative:
# python -m eljev install-hook --scope repo --repo <path-to-repo>
```

Verify with a direct typed decision:

```powershell
@'
{
  "id": "route-next-step",
  "kind": "choice",
  "instructions": "Pick the best next action",
  "options": [
    {"id":"reply","description":"Reply now with details"},
    {"id":"schedule","description":"Schedule a follow-up meeting"},
    {"id":"delegate","description":"Delegate to the owner"}
  ]
}
'@ | Set-Content -Path .\question.choice.json
python -m eljev decide --state-text "Customer asked for ETA and owner details." --question .\question.choice.json
```

```bash
cat > question.choice.json <<'JSON'
{
  "id": "route-next-step",
  "kind": "choice",
  "instructions": "Pick the best next action",
  "options": [
    {"id":"reply","description":"Reply now with details"},
    {"id":"schedule","description":"Schedule a follow-up meeting"},
    {"id":"delegate","description":"Delegate to the owner"}
  ]
}
JSON
python -m eljev decide --state-text "Customer asked for ETA and owner details." --question ./question.choice.json
```

## Turning el-jev on and off

| Control | PowerShell | bash |
|---|---|---|
| User-level switch OFF | `[System.Environment]::SetEnvironmentVariable("EL_JEV","OFF","User")` | Add `export EL_JEV=OFF` to your shell profile |
| User-level switch ON | `[System.Environment]::SetEnvironmentVariable("EL_JEV","ON","User")` | `export EL_JEV=ON` |
| Runtime toggle | `python -m eljev off` / `python -m eljev on` | `python -m eljev off` / `python -m eljev on` |
| Status | `python -m eljev status` | `python -m eljev status` |
| Copilot session override | `.\scripts\copilot.ps1 -NoJev` or `.\scripts\copilot.ps1 -WithJev` | n/a |

`eljev on/off` persists `enabled` in config, then reports the effective value and source. If an env var overrides config, status output calls that out.

## Hook modes

`ELJEV_HOOK_MODE` / `hook_mode` supports:

- `intent` (default): for prompts with at least `ELJEV_HOOK_MIN_WORDS` words (default 4), classify into a **choice** over five intents: `code_modification`, `review_audit`, `investigation_search`, `execution_testing`, `advisory_explanation`.
- `marker`: only act on explicit `<!--eljev.request:{...}-->` markers or `eljev.hook/1` payloads.

Hook timeout defaults to `ELJEV_HOOK_TIMEOUT_MS=500`. If daemon is down, the hook attempts one debounced spawn and returns `{}` immediately. Only `selected`, `needs_review`, and `abstain_tie` decisions inject advisory text.

Example injected block:

```text
[el-jev advisory]
kind: choice | decision: review_audit | confidence: 0.70 | margin: 0.58
gate: needs_review (exit 2) -> treat as a hint; reason normally
probabilities: review_audit=0.698, investigation_search=0.117, code_modification=0.086
[/el-jev advisory]
```

## Decision gating and exit behavior

`el-jev` applies one gate function for every shape.

| Status | Exit | Meaning |
|---|---:|---|
| `selected` | 0 | Calibrated gate passed for that kind. |
| `needs_review` | 2 | Advisory outcome, including default policy abstention. |
| `abstain_tie` | 2 | Exact top/runner-up tie. |
| `trivial` | 2 | Pregate short-circuit. |
| `engine_error` | 2 | Remote engine failed; decision stays advisory. |
| `invalid_response` | 2 | Malformed engine output; fail closed. |
| `invalid_input` | 1 | Contract/input validation error. |

Default policy is `always_abstain_v0`, so non-trivial decisions are advisory (`exit 2`). Exit `0` requires `coverage_policy=calibrated` and valid calibration for the request kind.

Example calibration file:

```json
{
  "calibration_version": "cal-2026-10-01",
  "temperature": 1.0,
  "threshold": 0.8,
  "margin_threshold": 0.2,
  "kinds": {
    "choice": {
      "calibration_version": "cal-choice-1",
      "temperature": 0.05,
      "threshold": 0.8,
      "margin_threshold": 0.3
    },
    "noul": {
      "calibration_version": "cal-noul-1",
      "temperature": 0.05,
      "threshold": 0.85,
      "margin_threshold": 0.4
    }
  }
}
```

## CLI usage examples

Choice:

```powershell
python -m eljev decide --state-text "PR adds migration and new API." --question .\question.choice.json
```

```bash
python -m eljev decide --state-text "PR adds migration and new API." --question ./question.choice.json
```

Noul (`criteria` is optional; supply it to describe what true/false mean):

```json
{
  "id": "deploy-gate",
  "kind": "noul",
  "instructions": "Is this safe to deploy now?",
  "criteria": {"true": "Tests pass and the change is low risk", "false": "Risky, untested, or blocking issues remain"}
}
```

Score (`criteria` is an ordered list of 2–10 levels, lowest first; the record's `score` is the expected 1-based level):

```json
{
  "id": "severity",
  "kind": "score",
  "instructions": "Rate incident severity",
  "criteria": ["minor user impact", "degraded feature", "major degradation", "critical outage"]
}
```

Screen:

```powershell
python -m eljev screen --criterion "most urgent customer issue today" --candidates .\candidates.json --top-n 3
```

```bash
python -m eljev screen --criterion "most urgent customer issue today" --candidates ./candidates.json --top-n 3
```

## Observability

- State dir: `<repo>/.eljev/` (override `ELJEV_DIR`).
- Decision log: `<ELJEV_DIR>/logs/decisions.jsonl`.
- Hook log: `<ELJEV_DIR>/logs/hook.jsonl` (no prompt text).
- Tail local records: `python -m eljev log 20`.
- Disable local logging: `ELJEV_LOGGING=OFF` (legacy: `ELJEV_NO_LOG=1`).
- Azure Foundry usage and cost are visible in Azure metrics; decision-level context remains local in `decisions.jsonl`.

## Configuration reference

Resolution order is **environment variable > `config.json` > default**.

| Setting | Env var | config.json key | Default |
|---|---|---|---|
| on/off switch | `EL_JEV` (ON/OFF), then `ELJEV_ENABLED`, then Windows user env `EL_JEV` from `HKCU\Environment` | `enabled` | ON |
| Cohere endpoint | `ELJEV_COHERE_ENDPOINT` | `cohere_endpoint` | none (required for Cohere) |
| deployment | `ELJEV_COHERE_DEPLOYMENT` | `cohere_deployment` | `Cohere-rerank-v4.0-pro` |
| gate policy | `ELJEV_COVERAGE_POLICY` | `coverage_policy` | `always_abstain_v0` |
| auth | `ELJEV_AUTH` | `auth` | `azcli` |
| az token subscription | `ELJEV_AZURE_SUBSCRIPTION` | `subscription` | unset (az default account) |
| hook mode | `ELJEV_HOOK_MODE` | `hook_mode` | `intent` |
| calibration file | `ELJEV_CALIBRATION_PATH` | — | `<repo>/eval/calibration.json` |
| logging | `ELJEV_LOGGING` (ON/OFF) | — | ON |
| log dir | `ELJEV_LOG_DIR` | — | `<ELJEV_DIR>/logs` |
| state dir | `ELJEV_DIR` | — | `<repo>/.eljev` |
| hook timeout | `ELJEV_HOOK_TIMEOUT_MS` | — | 500 |
| hook min words | `ELJEV_HOOK_MIN_WORDS` | — | 4 |
| engine timeout | `ELJEV_TIMEOUT_MS` | — | 2500 |
| redaction | `ELJEV_REDACT` | — | 1 |
| daemon port | `ELJEV_PORT` | — | 8787 |
| passthrough backend | `ELJEV_SYSTEMONE_URL` | — | unset |

Legacy note: `ELJEV_HOOK_ENABLED` is removed from runtime control and should not be used. `ELJEV_NO_LOG=1` is still honored.

## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md).

## Tests

```powershell
python -m unittest discover -s tests
```

```bash
python -m unittest discover -s tests
```

Live Azure tests run only when `ELJEV_LIVE_TESTS=1` and an endpoint is configured.

### Functional eval

To check a live deployment end to end (routing accuracy, latency, gate and hook), run
`python eval/scripts/functional_eval.py` with the daemon started. It exits `0` only when every
check passes. Last run: 29/30 intent routing, 20/20 `noul`, 20/20 50-candidate `screen`, all
gate and hook checks passing. See [eval/README.md](eval/README.md#functional-eval-smoke-check).

## License

MIT
