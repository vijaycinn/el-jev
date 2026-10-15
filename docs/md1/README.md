# Microsoft-Decision-1 (`md1` / `el-mds`) implementation brief

This folder captures the implementation direction for running `el-jev` on `Microsoft-Decision-1` (Azure deployment `Decision-1`) while preserving the `eljev.decision/1` contract, loopback security model, and policy semantics.

## Executive summary

- Keep one codebase (`el-jev`) with an engine pivot (`md1|cohere`) and `el-mds` CLI alias.
- Keep one policy source of truth in Python (`apply_gate`, calibration artifacts, `decisions.jsonl`): GHCP extension calls daemon APIs, not direct provider APIs.
- Default pilot mode remains advisory (`always_abstain_v0` or `ELJEV_EXTENSION_MODE=advisory`): inject budget guidance only, no tool denial.
- Active tool denial is allowed only in calibrated+enforce mode with all required gate heads selected.
- Benchmark result (`30/30` vs `29/30`) is directional smoke evidence only (`n=30`), not statistical proof.

## Contract invariants that remain mandatory

| Invariant | Enforcement in md1 plan |
|---|---|
| No per-decision process launch on hot path | Resident daemon and/or resident extension process only |
| Loopback daemon binds to `127.0.0.1` only | Keep bind guard and host/origin checks |
| Default policy abstains; `selected` only in calibrated mode | `apply_gate` remains sole gate function |
| Malformed output fails closed; transport/auth remains advisory | `invalid_response` vs `engine_error` split retained |
| No secret leakage in logs | Never log bearer tokens, auth headers, raw provider bodies, or raw prompt text |

## Runtime flow and single source of truth

```mermaid
flowchart LR
  U[User Prompt] --> X[GHCP extension: el-mds]
  X -->|POST /v1/gate<br/>AbortSignal.timeout(hookTimeoutMs)| D[Local daemon 127.0.0.1]
  D -->|md1/cohere dispatch + apply_gate + calibration + decisions.jsonl| F[Azure Foundry SystemOne]
  F --> D
  D --> X
  X -->|additionalContext budget text| LLM[Copilot turn]
  LLM -->|Tool request| P[onPreToolUse]
  P -->|Optional POST /v1/decide for helper tool| D
  P -->|allow/deny per gate policy| T[Tools]
```

Extension calls are bounded by `AbortSignal.timeout(hookTimeoutMs)` and **must fail open** (`return undefined`) on timeout, daemon unavailability, `engine_error`, `invalid_response`, or any non-`selected` gate status.

```javascript
async function gateViaDaemon(state, sessionId) {
  try {
    const r = await fetch("http://127.0.0.1:8787/v1/gate", { method: "POST", signal: AbortSignal.timeout(hookTimeoutMs), body: JSON.stringify({ state, session_id: sessionId }) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}
```

Default extension gate calls omit `"engine"` in the `/v1/gate` body so the daemon resolves `config.engine(default="md1")` (or `ELJEV_ENGINE`) per request; gate responses include `budget_text` for advisory injection.

## Extension enforcement policy (contract-aligned)

- **Default/pilot behavior** (`always_abstain_v0` or `ELJEV_EXTENSION_MODE=advisory`):
  - `onUserPromptSubmitted`: may inject advisory `additionalContext`.
  - `onPreToolUse`: observe-only (`enforceable: false`), returns `undefined`.
- **Deny behavior is authorized only when all are true**:
  1. `coverage_policy == "calibrated"`
  2. `ELJEV_EXTENSION_MODE == "enforce"`
  3. Gate heads required for `fast_direct` have `status == "selected"` and `exit_code == 0`

## Verified live findings (must drive implementation)

### 1) Endpoint and deployment-name gotcha

- Endpoint route:
  - `POST https://<your-resource>.services.ai.azure.com/providers/microsoft/v1/systemone`
- Azure deployment:
  - Deployment name: `Decision-1`
  - Underlying model: `Microsoft-Decision-1` (version `1`)
- Live-verified behavior:
  - Request body `"model": "Microsoft-Decision-1"` -> `404 DeploymentNotFound`
  - Request body `"model": "Decision-1"` -> `200 OK`

**Requirement:** wire `model` must be deployment name, never model family name.

### 2) Wire shape and mapping requirements

Request payload shape:

```json
{
  "model": "Decision-1",
  "state": "<string | object | array>",
  "questions": {
    "<question_id>": {
      "type": "choice" | "noul" | "score",
      "instructions": "<optional string>",
      "criteria": { "<id>": "<description>" }
    }
  }
}
```

Adapter requirements:

- `choice`: `criteria` keys in request must exactly match probability keys in response.
- `noul`: keep contract field `"noul": <float>` and also emit
  - `"probabilities": {"true": p_true, "false": 1.0 - p_true}`.
- `score`: md1 uses 0-based expectations; contract output remains 1-based for compatibility.

### 3) `noul` tie semantics via `apply_gate`

For `noul`, derive:

- `top = max(p_true, p_false)`
- `runner_up = min(p_true, p_false)`

Then call `apply_gate(top=top, runner_up=runner_up, ...)` and **do not compute `choice` outside the gate path**.
When `p_true == 0.5`, `top == runner_up == 0.5`, so gate returns `status="abstain_tie"` and `exit_code=2`; caller must null:

- `choice = None`
- `choice_index = None`

Index mapping remains `true -> 0`, `false -> 1`.

### 4) md1 calibration semantics (engine-scoped)

`calibration_for(calibration, kind, *, engine="cohere")` becomes engine-scoped:

- For `engine == "md1"` lookup only `calibration["engines"]["md1"]["kinds"][kind]`.
- Never fall back to legacy top-level or `calibration["kinds"][kind]` (Cohere domain).
- If `coverage_policy == "calibrated"` and md1 block is missing, return `None` with note:
  - `"no valid md1 calibration for kind '<kind>'"`
  - result must be `needs_review` / `exit_code=2`.

Calibrated probability transform for md1:

- `T == 1.0`: use native md1 probabilities unchanged.
- `T != 1.0`: apply power scaling in log-probability space:
  - `log_p_i = math.log(max(p_i, 1e-12)) / T`
  - then stable softmax renormalization.

## Benchmark framing (directional smoke, not decision-grade)

| Scenario | Decision-1 | Cohere-rerank-v4.0-pro |
|---|---:|---:|
| Intent routing, 30 prompts, 5-way | **30/30 (100.0%)**, warm p50 **195.2 ms** | 29/30 (96.7%), warm p50 183.0 ms |
| `screen` 50 candidates | **100%** (`item_49`), **196.7 ms** | baseline existing path |
| `screen` 250 candidates | **393.0 ms** | baseline existing path |

Interpretation:

- This is a directional smoke benchmark (`n=30`).
- `mean p_top = 0.9971` indicates very sharp native probabilities.
- Engine-specific calibration is required before enabling calibrated enforcement.

## Config snapshot

### `config.json` keys

| Key | Type | Notes |
|---|---|---|
| `engine` | `md1` \| `cohere` | runtime engine |
| `md1_endpoint` | string | normalized Foundry resource endpoint |
| `md1_deployment` | string | deployment name, e.g. `Decision-1` |
| `extension_mode` | `advisory` \| `enforce` | extension behavior |
| `hook_timeout_ms` | int | timeout used by extension hook calls |

### Timeout fallback order

`ELJEV_MD1_TIMEOUT_MS` -> `ELJEV_TIMEOUT_MS` -> `2500`.

## Document index

- Detailed phased plan: [`implementation-plan.md`](implementation-plan.md)
- Top-level pointer mirror: [`../md1.md`](../md1.md)
- Contract delta target: `CONTRACT.md` additive `v1.2` section (planned change)
