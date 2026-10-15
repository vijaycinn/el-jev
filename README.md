# el-jev v2 — Microsoft-Decision-1 First System-1 Gate for Copilot CLI

`el-jev v2` is a fast **System-1 decision gate** and GitHub Copilot CLI harness extension powered by **Microsoft-Decision-1** (`md1`, `POST /providers/microsoft/v1/systemone`) on Azure AI Foundry.  
It also supports **Cohere Rerank v4.0 Pro** via `--engine cohere`, with a unified runtime switch: `--engine md1|cohere`.

At runtime, `el-jev v2` does three things:

1. gets a single-pass 3-head System-1 verdict (`intent`, `needs_deep_analysis`, `execution_tier`),
2. compiles that verdict into imperative, RLHF-native execution constraints,
3. enforces the tool budget with deterministic harness gating (`onPreToolUse`) when the gate is enforceable.

## Service Boundary (MD1-first dual-engine path)

```text
GHCP Extension (el-mds) / Hook     CLI (el-mds / eljev) / MCP / Custom App
              \                                       /
               ------> local el-jev daemon (:8787) <--
                                   |
                         --engine md1 | cohere
                        /                     \
     Foundry Microsoft-Decision-1 (MD1)     Foundry Cohere Rerank v4.0 Pro
     (/providers/microsoft/v1/systemone)     (/providers/cohere/v2/rerank)
```

## v2 Architecture & Flow (Front-and-Center)

<p align="center">
  <img src="docs/architecture-schematic.png" alt="el-jev v2 architecture schematic" width="100%">
</p>

<p align="center">
  <img src="docs/decisioning-flow.png" alt="el-jev v2 decisioning flow" width="100%">
</p>

- 🌐 [Interactive system schematic](docs/architecture.html)
- 🌐 [Interactive decisioning flow](docs/decisioning-flow.html)
- 📖 [Architecture deep dive](docs/architecture.md)

---

## What Was Built in v2 (Ground-Up Microsoft-Decision-1 Architecture)

### 1) Native Microsoft-Decision-1 engine (`eljev/engines/md1.py`)

- First-class `md1` engine path for `choice`, `noul`, `score`, and `screen` (up to 250 candidates).
- Multi-question single-pass batching (`output_tokens = N_questions`) for compact decision passes.
- Unified decision envelope and fail-closed normalization for malformed/failed provider responses.

### 2) 3-Head Compound System-1 Gate (`POST /v1/gate` → `eljev.gate/1`)

One forward pass evaluates:

- `intent` (`choice`)
- `needs_deep_analysis` (`noul`)
- `execution_tier` (`fast_direct` | `bounded_work` | `deep_system2`)

This is typically ~200–300 ms end-to-end, with measured server-side `TimeToResponse` around ~78 ms for Decision-1 calls in warm paths.

### 3) Unified engine pivot + `el-mds` alias

- Runtime switching: `--engine md1|cohere`
- `el-mds` entrypoint path for Decision-1-first flows
- `el-jev-telemetry.bat` for CLI telemetry viewing

### 4) Resident GHCP SDK extension (`.github/extensions/el-mds/extension.mjs`)

- Uses `onUserPromptSubmitted` to inject the gate contract
- Uses `onPreToolUse` to enforce direct-tool budgets on enforceable `fast_direct` turns
- Removes the v1 shell-hook process tax (~1.2s/turn from PowerShell+Python spawn overhead path)

---

## Why Raw `[el-jev advisory]` Tags Fail — and How v2 Actually Stops LLM Overthinking

### The old v1 advisory block

```text
[el-jev advisory]
kind: choice | decision: review_audit | confidence: 0.70 | margin: 0.58
gate: needs_review (exit 2) -> treat as a hint; reason normally
probabilities: review_audit=0.698, investigation_search=0.117, code_modification=0.086
[/el-jev advisory]
```

### Why a base LLM will not reliably obey that

1. **Zero Pre-Training Prior**  
   Base models (`Claude`, `Gemini`, `GPT`) were never trained on private tags like `[el-jev advisory]`, `noul`, `exit 2`, or local probability dumps.

2. **Self-defeating hint string**  
   `"treat as a hint; reason normally"` explicitly tells System-2 reasoning to proceed as usual, often increasing deliberation and token burn.

3. **Intent nouns are not execution budgets**  
   `review_audit` describes category, not behavior limits. It does not tell the model whether to run 2 direct reads or spawn multiple sub-agents.

## v2 Three-Stage Bridge (How `el-mds` makes System-1 actionable to any LLM)

### Stage 1 — Multi-head System-1 verdict (Microsoft-Decision-1)

`md1` evaluates `intent + needs_deep_analysis + execution_tier` in one pass.

### Stage 2 — Semantic Compiler (`md1` probabilities → imperative RLHF execution contract)

`el-jev v2` compiles numeric gate outputs into plain-English imperative constraints injected through the harness `<system_reminder>` / `additionalContext` channel:

```text
[el-mds gate]
tier: fast_direct | intent: review_audit | needs_deep_analysis: 0.005 | mode: advisory (needs_review)
directive: EXECUTION CONTRACT (bounded review_audit task): Execute directly without extended deliberation. Do NOT write an upfront multi-step plan. Do NOT spawn sub-agents (do not call task or run_dynamic_workflow). Complete using at most 3 direct tool calls (view, grep, edit, powershell) and reply in 3 sentences or fewer.
[/el-mds gate]
```

### Stage 3 — Deterministic harness circuit-breaker (`onPreToolUse`)

If an enforceable `fast_direct` turn still attempts `task` or `run_dynamic_workflow`, `onPreToolUse` denies it (`permissionDecision: "deny"`) **before** any sub-agent LLM call is launched.

---

## Quick Start (md1-first)

```powershell
git clone https://github.com/vijaycinn/el-jev.git
Set-Location .\el-jev
python -m pip install -e .
az login

# Configure Decision-1 first
python -m eljev configure --engine md1 --md1-endpoint https://<your-resource>.services.ai.azure.com --md1-deployment Decision-1 --extension-mode advisory

# Optional Cohere sidecar config for pivoting
python -m eljev configure --endpoint https://<your-resource>.services.ai.azure.com --cohere-deployment Cohere-rerank-v4.0-pro

python -m eljev daemon start
python -m eljev status
python -m eljev install-extension --scope user
python -m eljev install-hook --scope user
```

```bash
git clone https://github.com/vijaycinn/el-jev.git
cd el-jev
python -m pip install -e .
az login

# Configure Decision-1 first
python -m eljev configure --engine md1 --md1-endpoint https://<your-resource>.services.ai.azure.com --md1-deployment Decision-1 --extension-mode advisory

# Optional Cohere sidecar config for pivoting
python -m eljev configure --endpoint https://<your-resource>.services.ai.azure.com --cohere-deployment Cohere-rerank-v4.0-pro

python -m eljev daemon start
python -m eljev status
python -m eljev install-extension --scope user
python -m eljev install-hook --scope user
```

---

## CLI Usage Examples

### Gate (3-head compound verdict)

```powershell
python -m eljev gate --state-text "User asked to rename one local variable and update one unit test."
```

```bash
python -m eljev gate --state-text "User asked to rename one local variable and update one unit test."
```

### Decide (typed `choice|noul|score`)

```powershell
python -m eljev decide --state-text "PR adds migration and new API." --question .\question.choice.json --engine md1
```

```bash
python -m eljev decide --state-text "PR adds migration and new API." --question ./question.choice.json --engine md1
```

### Screen (candidate reranking up to 250)

```powershell
python -m eljev screen --criterion "most urgent customer issue today" --candidates .\candidates.json --top-n 3 --engine cohere
```

```bash
python -m eljev screen --criterion "most urgent customer issue today" --candidates ./candidates.json --top-n 3 --engine cohere
```

### Telemetry

```powershell
.\el-jev-telemetry.bat
```

---

## Latency & Savings Benchmarks

Measured local runs comparing fast typed decisioning vs sub-agent decisioning:

| System | Accuracy (30) | Median | p95 |
|---|---|---:|---:|
| **el-jev (Decision-1/Cohere path, warm daemon)** | 29/30 | **183 ms** | **351 ms** |
| task sub-agent (`gpt-5.6-luna`) | 30/30 | 3.84 s | 5.76 s |
| general-purpose sub-agent (`gemini-3.8-flash`) | 30/30 | 5.12 s | 10.48 s |

Practical takeaway:

- Decision classification/routing can be ~21–28× faster than full LLM reasoning paths.
- Biggest end-to-end gains occur when `el-jev` replaces LLM routing, not when it only annotates prompts.
- v2 extension enforcement preserves that speedup by blocking unnecessary sub-agent launches on enforceable `fast_direct` turns.

---

## Engine Pivot (`--engine md1|cohere`)

All decision commands support explicit engine selection:

- `python -m eljev gate ... --engine md1|cohere`
- `python -m eljev decide ... --engine md1|cohere`
- `python -m eljev screen ... --engine md1|cohere`

`main_mds` routes by default to `md1`; `eljev` uses configured engine unless overridden.

---

## Azure Prerequisites

1. Create/use an Azure AI Foundry or AI Services resource.
2. Deploy `Decision-1` (for `md1`) and optionally `Cohere-rerank-v4.0-pro`.
3. Use Entra auth scope: `https://ai.azure.com/.default`.
4. Assign **Cognitive Services User** at resource scope.

```powershell
az role assignment create `
  --role "Cognitive Services User" `
  --assignee "<user-or-principal-id>" `
  --scope "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<your-resource>"
```

---

## Configuration Reference

Resolution order: **environment variable > `config.json` > default**

| Setting | Env var | config key | Default |
|---|---|---|---|
| Engine | `ELJEV_ENGINE` | `engine` | `cohere` |
| MD1 endpoint | `ELJEV_MD1_ENDPOINT` | `md1_endpoint` | unset |
| MD1 deployment | `ELJEV_MD1_DEPLOYMENT` | `md1_deployment` | `Decision-1` |
| Cohere endpoint | `ELJEV_COHERE_ENDPOINT` | `cohere_endpoint` | unset |
| Cohere deployment | `ELJEV_COHERE_DEPLOYMENT` | `cohere_deployment` | `Cohere-rerank-v4.0-pro` |
| Extension mode | `ELJEV_EXTENSION_MODE` | `extension_mode` | `advisory` |
| Hook mode | `ELJEV_HOOK_MODE` | `hook_mode` | `intent` |
| Hook timeout | `ELJEV_HOOK_TIMEOUT_MS` | `hook_timeout_ms` | `750` |
| Coverage policy | `ELJEV_COVERAGE_POLICY` | `coverage_policy` | `always_abstain_v0` |
| On/off | `EL_JEV`, `ELJEV_ENABLED` | `enabled` | ON |
| State dir | `ELJEV_DIR` | — | `<repo>/.eljev` |
| Log dir | `ELJEV_LOG_DIR` | — | `<ELJEV_DIR>/logs` |
| Daemon port | `ELJEV_PORT` | — | `8787` |

---

## Observability

- Decision log: `.eljev/logs/decisions.jsonl`
- Hook log: `.eljev/logs/hook.jsonl`
- Tail log: `python -m eljev log 20`
- Daemon health: `python -m eljev status`

---

## Tests

```powershell
python -m unittest discover -s tests -q
```

---

## License

MIT
