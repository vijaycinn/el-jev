# el-jev interface contract v1.1

This contract is the integration baseline for daemon, CLI, MCP, hook, and skill surfaces.
Schema string remains `eljev.decision/1`; v1.1 is additive.

---

## 0. Design invariants

1. No per-decision process launch on the hot path.
2. Loopback daemon binds to `127.0.0.1` only.
3. Default policy abstains (`always_abstain_v0`), so non-trivial decisions are advisory unless calibrated.
4. Malformed remote output fails closed; transport/auth failures stay advisory.
5. Never log secrets, tokens, Authorization headers, or raw prompt text.

---

## 1. Decision record schema (`eljev.decision/1`)

Base fields remain required for all decisions.
Shape A (`choice | noul | score`) adds fields without changing schema name:

- `question_id`
- `kind`
- `confidence` (top probability)
- `probabilities` (`id -> probability`)
- `margin` (top minus runner-up probability)
- `noul` (`true`/`false` probability object for `noul`)
- `score` (expected 1-based score for `score`)
- `raw_top_score`, `raw_runner_up`, `margin_raw` are raw Cohere relevance-domain values
- `calibrated_probability`, `margin_calibrated` are post-temperature gate-domain values

All numeric fields are JSON numbers or `null` only.

---

## 2. Status values

| Status | Meaning | Exit |
|---|---|---:|
| `selected` | Calibrated gate passed for the request kind | 0 |
| `needs_review` | Advisory output, including default abstention and uncertain gates | 2 |
| `abstain_tie` | Exact tie at top of ranking/probability | 2 |
| `trivial` | Pregate short-circuit, no model call | 2 |
| `engine_error` | Engine transport/auth/rate/timeout failure | 2 |
| `invalid_response` | Malformed engine output | 2 |
| `invalid_input` | Input/contract validation failed | 1 |

`engine_error` and `invalid_response` apply to both Shape A and Shape B.

---

## 3. Exit codes

| Code | Meaning |
|---:|---|
| 0 | Selected decision, allowed only in calibrated mode with valid per-kind calibration |
| 1 | Invalid input/validation error |
| 2 | Advisory/abstention/engine-path outcome |

Provider failures must not produce exit code 1.

---

## 4. `error_kind` taxonomy

`auth`, `timeout`, `http`, `dns`, `tls`, `connection`, `malformed`, `rate_limited`,
`daemon_unavailable`, `daemon_starting`, `daemon_version_mismatch`, `internal`,
`forbidden`, `invalid_input`, or `null`.

---

## 5. Policy and calibration

### 5.1 Policies

- `always_abstain_v0` (default): non-trivial decisions are advisory (`exit 2`).
- `calibrated`: `selected` is possible when calibration is valid for the request kind.

### 5.2 Calibration lookup

`calibration_for(cal, kind)` resolves in this order:

1. `cal["kinds"][kind]` for `kind in {"screen","choice","noul","score"}`
2. Legacy fallback for `screen` only: top-level `temperature`, `threshold`, `margin_threshold`

Per-kind Shape A calibrations are currently hand-supplied.
`eval/scripts/calibrate.py` currently emits the `screen` block only.

### 5.3 Shared gate logic

One gate (`apply_gate`) is applied to every shape:

1. Exact tie -> `abstain_tie`
2. Untrusted heuristic -> `needs_review`
3. `always_abstain_v0` -> `needs_review`
4. Missing calibration params for kind in calibrated mode -> `needs_review` with note
5. No runner-up -> `needs_review`
6. Otherwise selected iff:
   - `p(top) >= threshold`
   - `p(top) - p(runner_up) >= margin_threshold`

---

## 6. Input caps

### 6.1 Shared caps

| Rule | Limit |
|---|---:|
| state text length | <= 100,000 chars |
| instructions length | <= 2,000 chars |

### 6.2 Shape B (`screen`)

| Rule | Limit |
|---|---:|
| candidates | 1-250 |
| chars per candidate | <= 2,000 |
| total candidate chars | <= 100,000 |
| duplicate ids | rejected |

### 6.3 Shape A (`decide`)

| Kind | Rule |
|---|---|
| `choice` | 2-250 options, unique ids, each option text <= 2,000 chars |
| `noul` | binary probabilities over true/false semantics |
| `score` | 2-10 levels |

Cap violations return HTTP 400 and exit code 1.

---

## 7. Daemon API (`http://127.0.0.1:8787`)

### 7.1 `GET /health`

Returns status and runtime state including:

- `pid`
- `warm`
- `auth`
- `engines.cohere` (`ready` | `absent`)
- `engines.systemone` (`passthrough` | `cohere` | `heuristic`)

### 7.2 `GET /v1/log?limit=N`

Returns recent decision records.

### 7.3 `POST /v1/screen`

Shape B request. Accepts criterion + candidates and returns a decision record.

### 7.4 `POST /v1/decide`

Shape A request.

- `kind`: `choice`, `noul`, or `score`
- `state`: string, object, or array
- response includes additive Shape A fields in §1

### 7.5 `POST /v1/shutdown`

Graceful daemon shutdown endpoint used by lifecycle commands.

### 7.6 Request hardening

Daemon rejects:

- Any request with an `Origin` header -> 403
- `Host` not in `127.0.0.1` / `localhost[:port]` -> 403
- POST without `Content-Type: application/json` -> 415

This prevents browser-driven and DNS-rebinding abuse against paid model calls.

---

## 8. Cohere endpoint and auth

Route:

```text
POST https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank
```

Rules:

- No `api-version` query parameter for this route.
- `azcli` auth reads token expiry from JSON (`expires_on`) and refreshes early under lock.
- `managed_identity` auth supports IMDS and App Service identity endpoint/header.
- `AZURE_CLIENT_ID` selects a user-assigned identity when needed.
- Warm-up thread pre-mints token and primes TLS at start and every 30s; it also replaces an idle keep-alive connection that the server has already closed, so the next request does not pay reconnect + retry.
- One retry is allowed when Azure closes an idle keep-alive connection.
- `408`, `409`, `429`, and `5xx` can retry within one deadline honoring `Retry-After`.
- Unexpected server failures return `500 {"error_kind":"internal"}` with no traceback.

---

## 9. Settings and environment variables

Resolution order is `env > config.json > default`.

| Setting | Env var | `config.json` key | Default |
|---|---|---|---|
| on/off switch | `EL_JEV`, then `ELJEV_ENABLED`, then Windows user env `EL_JEV` | `enabled` | ON |
| Cohere endpoint | `ELJEV_COHERE_ENDPOINT` | `cohere_endpoint` | none |
| deployment | `ELJEV_COHERE_DEPLOYMENT` | `cohere_deployment` | `Cohere-rerank-v4.0-pro` |
| gate policy | `ELJEV_COVERAGE_POLICY` | `coverage_policy` | `always_abstain_v0` |
| auth | `ELJEV_AUTH` | `auth` | `azcli` |
| az token subscription | `ELJEV_AZURE_SUBSCRIPTION` | `subscription` | unset (az default account) |
| hook mode | `ELJEV_HOOK_MODE` | `hook_mode` | `intent` |
| calibration file | `ELJEV_CALIBRATION_PATH` | — | `<repo>/eval/calibration.json` |
| logging | `ELJEV_LOGGING` | — | ON |
| log dir | `ELJEV_LOG_DIR` | — | `<ELJEV_DIR>/logs` |
| state dir | `ELJEV_DIR` | — | `<repo>/.eljev` |
| hook timeout | `ELJEV_HOOK_TIMEOUT_MS` | — | 500 |
| hook min words | `ELJEV_HOOK_MIN_WORDS` | — | 4 |
| engine timeout | `ELJEV_TIMEOUT_MS` | — | 2500 |
| redaction | `ELJEV_REDACT` | — | 1 |
| daemon port | `ELJEV_PORT` | — | 8787 |
| passthrough backend | `ELJEV_SYSTEMONE_URL` | — | unset |

Legacy: `ELJEV_NO_LOG=1` still disables logging. The legacy hook-enable variable is no longer a runtime control.

---

## 10. Module layout

| Path | Purpose |
|---|---|
| `eljev/config.py` | Config resolution, state-dir paths, runtime settings |
| `eljev/daemon.py` | Loopback HTTP daemon and endpoints |
| `eljev/cli.py` | CLI entrypoints and lifecycle commands |
| `eljev/verdict.py` | Shared gate logic |
| `eljev/engines/cohere.py` | Cohere client, token management, retries |
| `eljev/engines/systemone.py` | Shape A engine integration and passthrough hooks |
| `hooks/eljev_pre_turn.py` | Copilot `userPromptTransformed` hook |
| `mcp/server.py` | MCP tool surface |
| `eval/` | Calibration and coverage/risk workflow |
