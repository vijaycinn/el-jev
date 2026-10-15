# Microsoft-Decision-1 (`md1` / `el-mds`) implementation plan

## 1. Scope and success criteria

This plan upgrades `el-jev` from Cohere-emulated System-1 to native `Microsoft-Decision-1` while preserving `eljev.decision/1`, loopback security, and fail-safe behavior.

### In-scope

1. New md1 engine (`eljev/engines/md1.py`) with keyless Entra auth and connection reuse.
2. Engine pivot (`md1|cohere`) across daemon, CLI, config, and MCP.
3. `screen` path implemented through md1 `choice` (up to 250 candidates).
4. `el-mds` CLI alias and md1-first defaults.
5. Compound gate for anti-overthinking (`intent + needs_deep_analysis + execution_tier`).
6. Native GHCP extension with advisory-first behavior and enforceable deny path.
7. Engine-specific calibration and validation hardening.
8. Contract additive delta documentation (`CONTRACT.md` v1.2 section).

### Out-of-scope (this increment)

- Breaking `eljev.decision/1` consumers.
- Non-loopback daemon exposure.
- Removal of Cohere path (kept for rollback/comparison).

## 2. Design constraints (non-negotiable)

| Constraint | Required behavior |
|---|---|
| No hot-path process spawn | Daemon and extension must be resident |
| Loopback security | Daemon binds `127.0.0.1`; preserve host/origin checks |
| Abstain-by-default | `always_abstain_v0` remains advisory (`exit 2`) |
| Fail-closed malformed | Parse/shape issues -> `invalid_response` (`exit 2`) |
| Advisory transport/auth | Timeout/auth/http issues -> `engine_error` (`exit 2`) |
| Secret safety | No bearer tokens, auth headers, prompt bodies, or raw provider error bodies in logs/notes |

## 3. Runtime architecture and single source of truth

The GHCP extension is a control-plane client to the local Python daemon:

- `POST http://127.0.0.1:8787/v1/gate`
- `POST http://127.0.0.1:8787/v1/decide`

All extension daemon calls are bounded with `AbortSignal.timeout(hookTimeoutMs)`.

`apply_gate`, calibration logic, and `decisions.jsonl` remain authoritative in Python. The extension does not re-implement gating math.

### Fail-open rules in extension hooks

The extension must return `undefined` (allow) when any of these occur:

- timeout
- daemon unavailable
- `engine_error`
- `invalid_response`
- any required head not `status == "selected"` / `exit_code == 0`

### Enforceability rules

`permissionDecision: "deny"` in `onPreToolUse` is allowed only when all are true:

1. `coverage_policy == "calibrated"`
2. `ELJEV_EXTENSION_MODE == "enforce"`
3. Gate heads needed for `fast_direct` are `selected` with `exit_code == 0`

Default pilot mode (`always_abstain_v0` or `ELJEV_EXTENSION_MODE=advisory`) is observe-only: advisory `additionalContext` injection, no denials.

## 4. Configuration surface and precedence

### 4.1 Engine resolution order (strict)

Per request/command, resolve engine in this order:

1. per-request JSON `"engine"` field / CLI `--engine`
2. `ELJEV_ENGINE`
3. `config.json` `"engine"`
4. entrypoint default (`"md1"` for `el-mds`, `"cohere"` for `eljev`)

`ELJEV_ENGINE` (when set) always overrides the entrypoint default; the entrypoint default applies only when neither env nor `config.json` provides an engine.

For extension policy mode, the daemon must evaluate `config.extension_mode()` on every `/v1/gate` request with precedence:

`ELJEV_EXTENSION_MODE` -> `config.json` `"extension_mode"` -> `"advisory"`

This keeps `python -m eljev configure --extension-mode advisory` effective on the very next prompt without restarting the daemon.

### 4.2 `config.json` keys (complete)

| Key | Values | Notes |
|---|---|---|
| `engine` | `md1` \| `cohere` | default engine for local runtime |
| `md1_endpoint` | string URL | normalized endpoint base |
| `md1_deployment` | string | Azure deployment name, e.g. `Decision-1` |
| `extension_mode` | `advisory` \| `enforce` | extension behavior |
| `hook_timeout_ms` | positive integer | extension daemon call timeout |

Timeout fallback order for md1 requests:

`ELJEV_MD1_TIMEOUT_MS` -> `ELJEV_TIMEOUT_MS` -> `2500`

### 4.3 Endpoint normalization and host validation

Add:

```python
def normalize_md1_endpoint(raw: str) -> str: ...
```

Rules:

1. Require `https://`.
2. Reject credentials, query, or fragment.
3. Host must end with:
   - `.services.ai.azure.com`, or
   - `.cognitiveservices.azure.com`
4. Accept either:
   - bare resource URL, e.g. `https://<your-resource>.services.ai.azure.com`
   - full route URL ending in `/providers/microsoft/v1/systemone`
5. If full route is supplied, strip `/providers/microsoft/v1/systemone` so `MD1_PATH` is never double-appended.

This prevents `https://ai.azure.com/.default` bearer tokens from being sent to arbitrary hosts.

### 4.4 `ELJEV_SYSTEMONE_URL` coexistence

- If `ELJEV_SYSTEMONE_URL` is set and resolved `engine == "cohere"`, passthrough path remains valid.
- If resolved `engine == "md1"`, always use `MD1Engine` directly.
- `configure` warns when `ELJEV_SYSTEMONE_URL` is set while `engine == "md1"` to avoid ambiguity.

### 4.5 Contract deliverable updates (`CONTRACT.md` v1.2 additive delta)

Add `CONTRACT.md` to file-level deliverables and document additive md1 fields:

1. `engine`: deployment name (e.g., `"Decision-1"`).
2. `tier_path`:
   - `["systemone", "md1"]` for `decide`
   - `["pregate", "md1"]` for `screen`
3. `raw_top_score`, `raw_runner_up`, `margin_raw`: raw unscaled md1 top/runner-up probabilities.
4. `score` handling:
   - add optional additive `raw_md1_score` in `[0, K-1]`
   - keep contract `score = raw_md1_score + 1.0` in `[1, K]`
   - re-key `probabilities` by `levels[i]` text for backward compatibility.
5. `results[].relevance_score`: raw md1 option probability for that candidate.
6. `GET /health` adds:
   - `"engine": "md1" | "cohere"`
   - `"engines": {"cohere":"ready|absent","md1":"ready|absent","systemone":"passthrough|md1|cohere|heuristic"}`
7. `POST /v1/screen` tier enum expands to:
   - `auto | md1 | cohere | local | pregate_only`
8. `POST /v1/gate` request/response:
   - request (extension default): `{"state":"...","session_id":"..."}` (omit `"engine"` so daemon resolves `config.engine(default="md1")`; explicit per-request `"engine"` remains an opt-in override).
   - response: `eljev.gate/1` envelope with DecisionRecords for `intent`, `needs_deep_analysis`, `execution_tier`, deterministic `tier` (`fast_direct|bounded_work|deep_system2`), `enforceable: bool`, and `budget_text`.
9. `CONTRACT.md` §1 erratum (pre-existing drift fix):
   - in `eljev.decision/1`, `noul` is `float | null` (`p_true`, matching `systemone.py:524`)
   - `probabilities` carries the object form: `{"true": p_true, "false": 1.0 - p_true}`.

## 5. Planned file-level changes and signatures

### 5.1 `pyproject.toml`

```diff
[project.scripts]
eljev = "eljev.cli:main"
+el-mds = "eljev.cli:main_mds"
```

### 5.2 `eljev/config.py`

Add engine and md1 readers:

```python
VALID_ENGINES = ("md1", "cohere")
DEFAULT_MD1_DEPLOYMENT = "Decision-1"

def engine(default: str = "cohere") -> str: ...
def md1_endpoint() -> str: ...
def md1_deployment() -> str: ...
def md1_timeout_ms() -> int: ...
def normalize_md1_endpoint(raw: str) -> str: ...
```

### 5.3 `eljev/engines/auth.py` (new)

Shared bearer mint/cache for Cohere and md1:

```python
class FoundryAuthError(RuntimeError): ...
class FoundryBearerProvider:
    def __init__(self, *, mode: str, scope: str = "https://ai.azure.com/.default", ...): ...
    def get_token(self) -> str: ...
    def warm(self) -> bool: ...
```

### 5.4 `eljev/engines/cohere.py`

Refactor to shared auth provider without behavior regression.

### 5.5 `eljev/engines/md1.py` (new)

Core adapter for `/providers/microsoft/v1/systemone`:

```python
class MD1Error(RuntimeError): ...

class MD1Client:
    def __init__(self, endpoint: str, deployment: str, timeout_ms: int, bearer: FoundryBearerProvider, ...): ...
    def ask(self, state: str | dict | list, questions: Mapping[str, Any]) -> dict[str, Any]: ...

class MD1Engine:
    def __init__(self, client: MD1Client): ...
    def decide(self, state: Any, question: Mapping[str, Any], *, engine: str = "md1") -> dict[str, Any]: ...
    def decide_batch(self, state: Any, questions: Mapping[str, Mapping[str, Any]], *, engine: str = "md1") -> dict[str, Any]: ...
```

#### Calibration behavior for md1

`calibration_for(calibration, kind, *, engine="cohere")`:

- for `engine == "md1"`, read only `calibration["engines"]["md1"]["kinds"][kind]`
- never fall back to top-level or `calibration["kinds"][kind]`
- if missing under calibrated policy, return `None` + note:
  - `"no valid md1 calibration for kind '<kind>'"`
  - map to `needs_review` (`exit_code=2`)

Probability scaling in calibrated mode:

- `T == 1.0`: keep native md1 probabilities
- `T != 1.0`: power scaling in log-prob space
  - `log_p_i = math.log(max(p_i, 1e-12)) / T`
  - stable softmax renormalization

#### `noul` mapping semantics

Keep contract-aligned payload:

- `"noul": <float>` (authoritative field)
- `"probabilities": {"true": p_true, "false": 1.0 - p_true}`

Do not decide `choice` before gating:

1. `top = max(p_true, p_false)`
2. `runner_up = min(p_true, p_false)`
3. call `apply_gate(top=top, runner_up=runner_up, ...)`
4. if tie (`p_true == 0.5`): expect `abstain_tie` and force `choice=None`, `choice_index=None`

#### Strict `validate_md1_response`

Validation rules:

1. `answers` keys must exactly match requested `questions` keys.
2. `choice`:
   - probability keys exactly match `criteria` keys (no missing/extra)
   - values finite, non-bool floats in `[0,1]`
   - `abs(sum(probs) - 1.0) <= 1e-4`
   - `confidence` finite non-bool in `[0,1]`
3. `noul`:
   - `noul` finite non-bool float in `[0,1]`
4. `score`:
   - probabilities keyed as `"0".."K-1"` where `K == len(levels)`
   - values finite non-bool floats in `[0,1]`
   - `abs(sum(probs) - 1.0) <= 1e-4`
   - `score` finite non-bool in `[0, K-1]`

#### HTTP status mapping table

| Provider status | Mapped status | error_kind | exit_code | Notes |
|---|---|---|---:|---|
| 400, 404 | `engine_error` | `http` | 2 | never exit 1 |
| 401, 403 | `engine_error` | `auth` | 2 | auth-class advisory |
| 408 | `engine_error` | `timeout` | 2 | retry within deadline first |
| 409, 429 | `engine_error` | `rate_limited` | 2 | retry within deadline first |
| 5xx | `engine_error` | `http` | 2 | retry within deadline first |

All retries must stay inside request deadline budget.

### 5.6 `eljev/daemon.py`

Daemon holds both engines and dispatches per request:

```python
self.cohere: CohereEngine | None
self.md1: MD1Engine | None
```

Request dispatch:

`payload.get("engine") or config.engine(default="md1")`

Use this for `/v1/decide`, `/v1/screen`, and `/v1/gate`, allowing `el-mds` and `eljev` to share one daemon without silent engine mismatch.

For `/v1/gate`, re-evaluate `config.extension_mode()` per request (`ELJEV_EXTENSION_MODE` -> `config.json` `"extension_mode"` -> `"advisory"`) so `configure --extension-mode ...` changes apply immediately without daemon restart.

Fix malformed mapping bug in `_decide`:

- `error_kind == "malformed"` must map to `status = "invalid_response"` (not generic engine error).
- Notes use fixed safe strings only (never echo raw provider error body).

### 5.7 `eljev/cli.py`, `eljev/client.py`, and `mcp/server.py`

Add explicit engine propagation:

- CLI resolves engine by precedence chain and includes `"engine"` in JSON body.
- Client sends `"engine"` for:
  - `POST /v1/decide`
  - `POST /v1/screen`
  - `POST /v1/gate`
- MCP/server path supports same pass-through.

CLI signatures:

```python
def main(argv: list[str] | None = None) -> int: ...
def main_mds(argv: list[str] | None = None) -> int: ...
```

### 5.8 `hooks/eljev_pre_turn.py`

Interim hook mode keeps advisory behavior; do not enforce deny outside extension enforce path.

### 5.9 `.github/extensions/el-mds/extension.mjs` and user-scope mirror

Extension files:

- Repo scope: `.github/extensions/el-mds/extension.mjs`
- User scope: `~/.copilot/extensions/el-mds/extension.mjs`

User scope is required to protect sessions outside `el-jev`.
Install/update via:

- `eljev install-extension --scope repo`
- `eljev install-extension --scope user`
- or `extensions_manage` workflows.

### 5.10 Exact Copilot SDK extension pattern (required)

```javascript
import { joinSession } from "@github/copilot-sdk/extension";

const gateBySession = new Map();
async function gateViaDaemon(state, sessionId) {
  try {
    const r = await fetch("http://127.0.0.1:8787/v1/gate", { method: "POST", signal: AbortSignal.timeout(hookTimeoutMs), body: JSON.stringify({ state, session_id: sessionId }) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

const session = await joinSession({
  tools: [
    {
      name: "el_mds_decide",
      description: "Run a fast System-1 decision (choice, noul, score, or screen) via el-mds / Microsoft-Decision-1.",
      parameters: {
        type: "object",
        properties: {
          state: { type: "string", description: "State text to evaluate" },
          question: { type: "object", description: "Question object (id, kind, instructions, criteria/options)" },
        },
        required: ["state", "question"],
      },
      handler: async (args, invocation) => { /* POST to http://127.0.0.1:8787/v1/decide */ },
    },
  ],
  hooks: {
    onUserPromptSubmitted: async (input, invocation) => {
      const gate = await gateViaDaemon(input.prompt, invocation.sessionId);
      gateBySession.set(invocation.sessionId, gate);
      return gate?.budget_text ? { additionalContext: gate.budget_text } : undefined;
    },
    onPreToolUse: async (input, invocation) => {
      const gate = gateBySession.get(invocation.sessionId);
      if (
        gate?.enforceable === true &&
        gate?.tier === "fast_direct" &&
        (input.toolName === "task" || input.toolName === "run_dynamic_workflow")
      ) {
        return {
          permissionDecision: "deny",
          permissionDecisionReason: "el-mds System-1 gate (calibrated fast_direct): use direct grep/view/edit instead of spawning a sub-agent.",
        };
      }
      return undefined;
    },
  },
});
```

## 6. Phased delivery plan

### Phase 1 — Shared auth + md1 engine

**Acceptance**

- md1 request composition uses deployment name in `model`.
- endpoint normalization and host allowlist checks enforce safe targets.
- malformed responses map to `invalid_response` (`exit 2`).

### Phase 2 — Engine pivot + md1 screen + `el-mds` alias

**Acceptance**

- `el-mds screen` works without Cohere configuration.
- single-candidate pregate short-circuit preserved.
- 250-candidate md1 screen path passes.

### Phase 3 — Compound gate + policy semantics

**Acceptance**

- `/v1/gate` supports per-request engine dispatch.
- policy output includes deterministic tier (`fast_direct|bounded_work|deep_system2`) and `enforceable`.
- advisory default produces `enforceable: false`.

### Phase 4 — Native GHCP extension circuit breaker

**Acceptance**

1. Extension runs with exact SDK pattern in repo and user scopes.
2. Calls to daemon are timeout-bounded and fail-open on all required failure classes.
3. In advisory pilot, `onPreToolUse` is observe-only.
4. Deny path triggers only under calibrated+enforce + selected-head conditions.
5. Validation check includes:
   - `extensions_manage({ operation: "inspect", name: "el-mds" })`

### Phase 5 — Calibration + exhaustive tests

**Acceptance**

- md1 calibration is engine-scoped with no legacy fallback.
- missing `engines.md1.kinds.<kind>` in calibrated mode yields `needs_review` (`exit 2`) with required note.
- probability scaling matches md1 power-scaling definition.
- smoke benchmark retained as directional evidence (`n=30`); not used as sole enforcement justification.

## 7. Test matrix (planned)

| Area | Test file(s) | Key cases |
|---|---|---|
| Endpoint normalization | `tests/test_config.py` | allowlist hosts, path stripping, reject creds/query/fragment |
| md1 client transport | `tests/test_md1_client.py` | deployment-name usage, retry budget, timeout, auth failure mapping |
| md1 response validation | `tests/test_md1_engine.py` | key parity, probability sums, finite float checks, score bounds |
| noul tie semantics | `tests/test_md1_engine.py` | `p_true=0.5` -> `abstain_tie`, `choice=None` |
| daemon dispatch | `tests/test_daemon.py` | per-request engine override, shared daemon mixed clients |
| malformed map fix | `tests/test_daemon.py` | `error_kind=malformed` -> `invalid_response` |
| gate endpoint | `tests/test_gate.py` (new) | `eljev.gate/1` envelope, tier/enforceable table |
| extension policy | `tests/test_extension_el_mds.py` | advisory observe-only vs enforce deny matrix |

## 8. Rollout and rollback

1. Dark-launch md1 behind engine flag.
2. Dual-engine CI jobs (`cohere`, `md1`).
3. `el-mds` canary ring.
4. Extension advisory pilot (`ELJEV_EXTENSION_MODE=advisory`).
5. Calibrated enforce rollout only after md1 calibration artifacts are present and validated.

Rollback:

- `ELJEV_ENGINE=cohere` or `config.json: {"engine":"cohere"}`
- extension default `/v1/gate` body omits `"engine"`, so daemon re-resolves `config.engine(default="md1")` every request (`ELJEV_ENGINE` > `config.json` > entrypoint default); rollback takes effect on the next prompt.
- `ELJEV_EXTENSION_MODE=advisory` to disable deny behavior immediately.
- daemon evaluates `config.extension_mode()` on every `/v1/gate` (`ELJEV_EXTENSION_MODE` > `config.json` `"extension_mode"` > `"advisory"`), so `python -m eljev configure --extension-mode advisory` applies immediately without restart.

## 9. Definition of done

1. md1 and cohere both pass contract/test suites.
2. `el-mds` and `eljev` can concurrently use one daemon via per-request engine dispatch.
3. Extension behavior is advisory by default and fail-open on all specified faults.
4. `CONTRACT.md` contains additive v1.2 md1 delta without breaking existing consumers.
5. No secret leakage in logs/notes/error surfaces.
