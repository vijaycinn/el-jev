# el-jev — Interface Contract v1

**This file is the coordination point. Every component codes against it. Do not change it
unilaterally — if a component needs a change, say so in your report.**

Repo root: `<path-to-el-jev>`
Python: 3.13 (Microsoft Store build). **Standard library only on the fast path.**
Target GitHub repo: `vijaycinn/el-jev`

---

## 0. Design invariants (non-negotiable)

1. **No per-decision process launch on the fast path.** Measured: interpreter start is ~275 ms and
   `jev.py --help` is ~700 ms on this host. The engine is a warm resident daemon.
2. **Do not import `http.client` in the client fast path.** It pulls in the whole `email` package
   (366 ms cumulative). The daemon may import it; the client must not.
3. **v0 abstains.** Calibration is not fitted, so every non-trivial decision returns **exit 2**.
   This is deliberate, not a stub. See §5.
4. **Fail closed on malformed engine output; fail open on transport failure.**
5. **Never log or print a token, key, or Authorization header.**

---

## 1. Decision record — `eljev.decision/1`

Emitted by the daemon, returned to the client, and appended to the decision log.

```jsonc
{
  "schema": "eljev.decision/1",
  "decision_id": "uuid4",
  "ts": "2026-09-24T18:00:00.000Z",     // UTC, ISO-8601, milliseconds
  "shape": "screen",                     // "screen" | "decide" | "pregate"
  "criterion": "which item is most urgent",
  "n_candidates": 50,

  "tier_path": ["pregate", "cohere"],    // ordered tiers actually executed
  "engine": "cohere-rerank-v4.0-pro",    // null when no model ran
  "engine_version": "1",

  "choice": "c3",                        // candidate id, or null
  "choice_index": 3,                     // index into the ORIGINAL candidate order
  "status": "needs_review",              // see §2
  "exit_code": 2,                        // see §3

  "raw_top_score": 0.92933786,
  "raw_runner_up": 0.92888767,
  "margin_raw": 0.00045019,
  "calibrated_probability": null,        // null until calibration is fitted
  "margin_calibrated": null,
  "calibration_version": "none",
  "coverage_policy": "always_abstain_v0",

  "results": [                            // ranked desc, capped at top_n
    { "id": "c3", "index": 3, "relevance_score": 0.92933786 }
  ],

  "elapsed_ms": { "total": 187.4, "pregate": 0.1, "engine": 183.0, "overhead": 4.3 },

  "escalation_reason": null,             // "margin_below_tau" | "long_candidates" | "n_gt_local_cap" | null
  "error_kind": null,                    // see §4
  "notes": []                            // human-readable, never secrets
}
```

**Rules**

- `choice_index` always indexes the **original** candidate array. No tier may reorder the input.
- `results` is sorted by `relevance_score` descending.
- Ties (`raw_top_score == raw_runner_up` exactly) **must** abstain — see §5. Measured live:
  `0.64027600` appeared 4× in one real response.
- Every numeric field is a JSON number or `null`. Never `NaN` / `Infinity`.

---

## 2. `status` values

| status | Meaning | exit |
|---|---|---|
| `selected` | Above threshold, safe to act on | 0 |
| `trivial` | Local pre-gate short-circuited; no model ran; nothing to act on | 2 |
| `needs_review` | Ranked, but policy says do not auto-act | 2 |
| `abstain_tie` | Top two scores are exactly equal | 2 |
| `invalid_response` | Engine returned a malformed body — **fail closed** | 2 |
| `engine_error` | Transport/auth/timeout/429 — **fail open to caller** | 2 |

---

## 3. Exit codes

| Code | Meaning | When |
|---|---|---|
| `0` | selected | **v0: never returned** unless `ELJEV_COVERAGE_POLICY=calibrated` and calibration is fitted |
| `1` | hard error | input validation only — bad JSON, cap breach, duplicate ids, empty text |
| `2` | needs review / fail open | everything else, including all engine failures |

**A provider failure must never produce exit 1.** Validate input first, then wrap only the engine
call in the fail-open handler.

---

## 4. `error_kind` taxonomy

`auth` · `timeout` · `http` · `dns` · `tls` · `connection` · `malformed` · `rate_limited` ·
`daemon_unavailable` · `daemon_starting` · `daemon_version_mismatch` · `null`

---

## 5. v0 coverage policy — `always_abstain_v0`

**The default policy returns exit 2 for every non-trivial decision.** The ranking and all
diagnostics are still returned; the host simply does not get an auto-act signal.

Why: the live score distribution has a measured top-1/top-2 margin of **0.00045** with exact ties.
No threshold is defensible until it is fitted on labelled data (see `eval/`). Shipping a gate now
would fabricate confidence.

Policy is selected by `ELJEV_COVERAGE_POLICY`:

- `always_abstain_v0` (**default**) — never exit 0.
- `calibrated` — requires `eval/calibration.json` to exist and be valid; applies the fitted
  temperature and thresholds. Exit 0 becomes possible.

Ties abstain under **both** policies.

### 5.1 Calibration convention (binding — both the engine and the fitter obey this)

**Transform.** Pointwise temperature-scaled sigmoid, applied independently to the top-1 and top-2
raw scores:

```
p(s) = sigmoid( logit(clip(s)) / T )        clip to [1e-12, 1-1e-12]
```

**Dual gate.** Exit 0 requires **both** conditions:

```
p(top1) >= threshold                 AND
p(top1) - p(top2) >= margin_threshold
```

A single-score gate is not acceptable. Measured live, the top five scores were
`0.92933786 … 0.92752105` — an absolute-score gate cannot distinguish them, so the margin term is
what carries the information. When there is only one candidate, `p(top2)` is treated as `0.0` and
the decision routes through the existence check rather than auto-accepting.

**`eval/calibration.json` required keys** (extra keys are ignored):

| Key | Type | Meaning |
|---|---|---|
| `temperature` | number > 0, finite | `T` above |
| `threshold` | number in `[0,1]` | floor on `p(top1)` |
| `margin_threshold` | number in `[0,1]` | floor on `p(top1) − p(top2)` |
| `calibration_version` | non-empty string | recorded on every decision |

If the file is missing, unparseable, or any required key fails validation → **fall back to abstain**,
record a note, and set `calibration_version: "none"`. Never crash; never silently auto-accept.

Both thresholds are fitted **only** on the development split.

---

## 6. Input caps (enforced before anything else, on every entry path)

| Rule | Limit | Violation |
|---|---|---|
| candidate count | 1–250 | exit 1 |
| chars per candidate | ≤ 2000 | exit 1 (**reject**, do not truncate) |
| total chars | ≤ 100,000 | exit 1 |
| duplicate candidate ids | not allowed | exit 1 |
| empty/whitespace candidate text | not allowed | exit 1 |
| `criterion` | non-empty string | exit 1 |

---

## 7. Daemon HTTP API — `http://127.0.0.1:8787`

Bind to `127.0.0.1` only. Never `0.0.0.0`.

### `GET /health`
```json
{ "status": "ok", "version": "0.1.0", "schema": "eljev.decision/1",
  "engines": { "cohere": "ready", "local": "absent" },
  "calibration_version": "none", "coverage_policy": "always_abstain_v0", "uptime_s": 12.3 }
```
`status` ∈ `ok` | `starting` | `degraded`.

### `POST /v1/screen` — Shape B (open rerank)
Request:
```json
{ "criterion": "which item is most urgent to act on today",
  "candidates": [ { "id": "c1", "text": "..." } ],
  "top_n": 10, "tier": "auto" }
```
`candidates` may also be a plain array of strings; ids are then auto-assigned `c0..cN`.
`tier` ∈ `auto` | `cohere` | `local` | `pregate_only`.
Response: an `eljev.decision/1` object. **HTTP status is always 200** when the daemon itself is
healthy — the verdict's `exit_code` carries the outcome.

### `POST /v1/decide` — Shape A (short typed decision)
```json
{ "state": "...", "question": { "id": "q1", "kind": "choice",
  "options": [ { "id": "reply", "description": "..." } ] } }
```
`kind` ∈ `choice` | `noul`. **v0: return `501` with `error_kind: "not_implemented"` unless a local
`/v1/systemone` backend is configured** via `ELJEV_SYSTEMONE_URL`.

### `GET /v1/log?limit=N`
Returns the last N decision records (diagnostics only).

---

## 8. Cohere engine — VERIFIED LIVE 2026-09-24

```
POST https://<your-resource>.services.ai.azure.com/providers/cohere/v2/rerank
Authorization: Bearer <Entra token>
Content-Type: application/json
```

- **The provider prefix is required.** `/v1/rerank`, `/v2/rerank`, `/models/rerank` all **404**.
- **No `api-version` query parameter.**
- **API-key auth is impossible** — the resource sets `disableLocalAuth: true` and returns 403
  `AuthenticationTypeDisabled`. Entra bearer only.
- Entra scope: `https://ai.azure.com/.default`. Obtain via
  `az account get-access-token --scope https://ai.azure.com/.default -o tsv`.
  Use `shell=False`, an argv list, and an explicit timeout. **Cache the token in-process** (~55 min)
  and exclude minting from the inference timeout.
- Request body: `{"model": "<deployment>", "query": ..., "documents": [...], "top_n": N,
  "return_documents": false}`
- Response: `{"results":[{"index":int,"relevance_score":float}],"id":...,"meta":{"billed_units":{"search_units":1}}}`
- Deployment name: `Cohere-rerank-v4.0-pro` (env `ELJEV_COHERE_DEPLOYMENT`).
- Quota: **150 req/60s, 150,000 tokens/60s.** Long candidates are quota-bound: 50×2,000 chars is
  ~25,000 tokens → ~6 calls/min.

**Connection reuse is mandatory** — measured 245 ms mean reused vs 764 ms fresh per call.
Keep one `HTTPSConnection` alive in the daemon and reconnect on failure.

**Retry only** `408`/`409`/`429`/`5xx`, jittered backoff, one total deadline. **Never** retry
connection resets, TLS errors, or parse failures — a re-send double-bills a paid call. Honor
`Retry-After`.

### Measured latency baseline (for tests and the README)

| Candidates | cold | p50 | p95 |
|---|---:|---:|---:|
| 10 | 606 | 165 | 669 |
| 50 | 518 | **183** | 409 |
| 250 | 1445 | 334 | 507 |
| 50 long (~2k chars) | 1190 | 624 | 1192 |

---

## 9. Environment variables

| Var | Default | Purpose |
|---|---|---|
| `ELJEV_HOST` | `127.0.0.1` | daemon bind |
| `ELJEV_PORT` | `8787` | daemon port |
| `ELJEV_COHERE_ENDPOINT` | *(required for Shape B)* | e.g. `https://<res>.services.ai.azure.com` |
| `ELJEV_COHERE_DEPLOYMENT` | `Cohere-rerank-v4.0-pro` | deployment name |
| `ELJEV_SYSTEMONE_URL` | unset | local Shape A backend, e.g. `http://127.0.0.1:8080` |
| `ELJEV_COVERAGE_POLICY` | `always_abstain_v0` | `always_abstain_v0` \| `calibrated` |
| `ELJEV_TIMEOUT_MS` | `2500` | engine deadline |
| `ELJEV_LOG_DIR` | `~/.eljev/logs` | decision log location — **outside the repo** |
| `ELJEV_REDACT` | `1` | redact candidate text in the log |

---

## 10. Module layout and ownership

| Path | Owner agent | Purpose |
|---|---|---|
| `eljev/types.py` | A1 | dataclasses + JSON (de)serialisation for `eljev.decision/1` |
| `eljev/validate.py` | A1 | §6 caps |
| `eljev/pregate.py` | A1 | trivial short-circuit |
| `eljev/verdict.py` | A1 | score → verdict, ties, policy |
| `eljev/engines/cohere.py` | A1 | §8 client |
| `eljev/engines/systemone.py` | A1 | Shape A passthrough |
| `eljev/daemon.py` | A1 | §7 server |
| `eljev/client.py` | A2 | fast client (no `http.client`) |
| `eljev/cli.py` | A2 | `eljev` CLI, exit codes |
| `mcp/` | A3 | MCP server |
| `hooks/` | A4 | Copilot harness wiring |
| `skill/el-jev/` | A5 | `SKILL.md` + references |
| `eval/` | A6 | corpus, labelling, calibration, coverage/risk |
| `README.md`, `docs/` | A7 | greenfield bootstrap |
| `tests/` | A1/A2 | unit + contract |

**Cross-component rule:** if you need something another agent owns, code against this contract and
assume it exists. Do not create files outside your ownership column.
