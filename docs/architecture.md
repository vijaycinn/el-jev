# el-jev architecture and decisioning

`el-jev` is a typed decision sidecar for Copilot CLI. It runs a loopback daemon, evaluates bounded decision tasks against Cohere rerank, and returns an auditable `eljev.decision/1` record with explicit abstention behavior.

## Interactive schematics

- 🌐 [System schematic](architecture.html)
- 🌐 [Decisioning flow schematic](decisioning-flow.html)

## Topology diagrams

![el-jev architecture schematic](architecture-schematic.png)

![el-jev decisioning flow](decisioning-flow.png)

## Topology summary

- **Hook surface:** Copilot `userPromptTransformed` via `hooks/eljev_pre_turn.py`.
- **Daemon:** loopback-only HTTP service on `127.0.0.1:8787`.
- **Engine:** Cohere rerank route on Azure AI Foundry with Entra auth.
- **State/logs:** repo-local `.eljev/` (`config.json`, `daemon.pid`, `spawn.stamp`, logs).
- **Config precedence:** environment variable > `config.json` > default.

## Statement-to-decision mapping

A prompt is mapped to a typed shape instead of a free-form completion:

| Shape | Input form | Typical use |
|---|---|---|
| `choice` | fixed option list | routing, triage, categorical selection |
| `noul` | binary decision | yes/no gate with probabilities |
| `score` | ordered levels | severity/priority scoring |
| `screen` | open candidate list (1-250) | reranking against a criterion |

In hook `intent` mode, auto-classification is a **choice** over five intents (`code_modification`, `review_audit`, `investigation_search`, `execution_testing`, `advisory_explanation`) for prompts meeting min-word and length bounds. `noul` and `score` are reached through explicit marker payloads, CLI, or MCP calls.

## Dual-gate decision policy

All shapes use one gate implementation (`apply_gate`) after probabilities are produced.
`selected` (exit 0) requires both conditions:

- `p(top) >= τ`
- `p(top) - p(runner_up) >= Δ`

`τ` and `Δ` come from calibration data, not fixed runtime constants.
Default policy (`always_abstain_v0`) forces non-trivial outputs to `needs_review` (exit 2).

## Fail-closed and advisory behavior

- Cohere malformed output -> `invalid_response` (exit 2), no selected decision.
- Cohere transport/auth/rate/timeout failures -> `engine_error` (exit 2), advisory only.
- If endpoint is not configured, local heuristic is explicitly untrusted (`needs_review`), never `selected`.

## Daemon lifecycle and warm path

- Daemon start is idempotent and writes its own JSON pidfile (`pid`, `port`, `started_at`, `exe`).
- `daemon stop` asks a live daemon to exit via `POST /v1/shutdown`. It only terminates a pid taken from the pidfile when the process creation time proves it is the daemon that wrote it, so a recycled pid is never killed.
- Windows uses exclusive bind; second daemon cannot bind the same port.
- Stop path calls `POST /v1/shutdown`, then terminates pid if needed.
- Orphan recovery uses `/health` pid checks when pidfile is stale or absent.
- Hook spawn is debounced through `.eljev/spawn.stamp`; no prompt-path sleep.
- Warm-up thread pre-mints Entra token and primes TLS on start, then every 30s, replacing an idle keep-alive connection the server has already closed.

## Loopback hardening

Daemon rejects:

- requests with any `Origin` header (403)
- `Host` not `127.0.0.1` / `localhost[:port]` (403)
- POST without `Content-Type: application/json` (415)

This prevents browser and DNS-rebinding abuse against paid remote calls.

## Authentication model

- `azcli` mode: uses `az account get-access-token -o json`, caches token with real expiry, refreshes five minutes early.
- `managed_identity` mode: supports IMDS and App Service identity endpoint/header; `AZURE_CLIENT_ID` selects user-assigned identity.
- Required scope: `https://ai.azure.com/.default`.
- Required role: **Cognitive Services User** on the AI Services resource.

## API surfaces

- `GET /health`: includes pid, warm state, auth mode, and engine readiness.
- `POST /v1/screen`: Shape B rerank.
- `POST /v1/decide`: Shape A `choice | noul | score`.
- `GET /v1/log`: recent decision records.
- `POST /v1/shutdown`: controlled daemon stop.

