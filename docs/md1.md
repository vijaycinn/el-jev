# md1 / el-mds

Top-level pointer for the `Microsoft-Decision-1` implementation package.

## Quick links

- Executive brief: [`docs/md1/README.md`](md1/README.md)
- Detailed phased plan: [`docs/md1/implementation-plan.md`](md1/implementation-plan.md)

## Decision summary

| Topic | Decision |
|---|---|
| Runtime architecture | Keep one `el-jev` codebase, add `md1|cohere` engine pivot |
| Policy source of truth | Keep `apply_gate`, calibration, and `decisions.jsonl` in local Python daemon |
| Extension transport | Extension uses `POST http://127.0.0.1:8787/v1/gate` + `/v1/decide` with timeout bounds |
| Extension default | Advisory pilot (`always_abstain_v0` / `ELJEV_EXTENSION_MODE=advisory`) is observe-only |
| Deny eligibility | Only when calibrated coverage + enforce mode + required heads `selected` (`exit_code=0`) |
| Error behavior | Timeout/daemon unavailable/engine_error/invalid_response/non-selected -> fail-open (`return undefined`) |
| `noul` semantics | Keep `"noul": float` and binary probabilities map; tie routes through `apply_gate` -> `abstain_tie` |
| Calibration | md1 uses engine-scoped calibration only (`engines.md1.kinds.<kind>`) |
| Benchmark framing | `30/30` vs `29/30` is directional smoke (`n=30`), not standalone proof |

## Architecture diagram

```mermaid
flowchart LR
  P[User prompt] --> E[GHCP extension: el-mds]
  E -->|POST /v1/gate<br/>AbortSignal.timeout(hookTimeoutMs)| D[el-jev daemon<br/>127.0.0.1 only]
  D -->|md1/cohere dispatch + apply_gate + calibration| A[Azure AI Foundry<br/>/providers/microsoft/v1/systemone]
  A --> D
  D --> E
  E -->|execution budget in additionalContext| C[Copilot main model]
  C -->|tool call request| G[onPreToolUse gate]
  G -->|allow| T[direct local tools]
  G -->|deny when authorized| B[task/run_dynamic_workflow blocked]
```

## Config keys to keep aligned

`config.json` keys for this workstream:

- `engine`
- `md1_endpoint`
- `md1_deployment`
- `extension_mode`
- `hook_timeout_ms`

Timeout fallback:

`ELJEV_MD1_TIMEOUT_MS` -> `ELJEV_TIMEOUT_MS` -> `2500`

## Critical wire gotcha

For md1 requests, body field `model` must be the **Azure deployment name** (`Decision-1`), not `Microsoft-Decision-1`.  
Using model-family string on wire returns `404 DeploymentNotFound`.
