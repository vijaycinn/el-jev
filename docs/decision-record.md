# `eljev.decision/1` decision record

This is the complete decision-record reference for the daemon, clients, logs,
and front doors.

## Canonical shape

```json
{
  "schema": "eljev.decision/1",
  "decision_id": "uuid4",
  "ts": "2026-09-24T18:00:00.000Z",
  "shape": "screen",
  "criterion": "which item is most urgent",
  "n_candidates": 50,
  "tier_path": ["pregate", "cohere"],
  "engine": "cohere-rerank-v4.0-pro",
  "engine_version": "1",
  "choice": "c3",
  "choice_index": 3,
  "status": "needs_review",
  "exit_code": 2,
  "raw_top_score": 0.92933786,
  "raw_runner_up": 0.92888767,
  "margin_raw": 0.00045019,
  "calibrated_probability": null,
  "margin_calibrated": null,
  "calibration_version": "none",
  "coverage_policy": "always_abstain_v0",
  "results": [
    {
      "id": "c3",
      "index": 3,
      "relevance_score": 0.92933786
    }
  ],
  "elapsed_ms": {
    "total": 187.4,
    "pregate": 0.1,
    "engine": 183.0,
    "overhead": 4.3
  },
  "escalation_reason": null,
  "error_kind": null,
  "notes": []
}
```

## Field contract

| Field | Type | Required meaning |
|---|---|---|
| `schema` | string | Exact value `eljev.decision/1`. |
| `decision_id` | string | UUID4 for this decision. |
| `ts` | string | UTC ISO-8601 timestamp with milliseconds. |
| `shape` | string | `screen`, `decide`, or `pregate`. |
| `criterion` | string | Non-empty criterion used for the decision. |
| `n_candidates` | integer | Validated input count. |
| `tier_path` | array[string] | Ordered tiers actually executed. |
| `engine` | string/null | Engine name, or null if no model ran. |
| `engine_version` | string | Engine version identifier. |
| `choice` | string/null | Top candidate ID, or null. This is not approval. |
| `choice_index` | integer/null | Original input-array index, or null. |
| `status` | string | One of the status values below. |
| `exit_code` | integer | One of 0, 1, or 2. |
| `raw_top_score` | number/null | Highest raw engine score. |
| `raw_runner_up` | number/null | Second-highest raw engine score. |
| `margin_raw` | number/null | Difference between top and runner-up. |
| `calibrated_probability` | number/null | Null until fitted calibration exists. |
| `margin_calibrated` | number/null | Null until fitted calibration exists. |
| `calibration_version` | string | Calibration identifier, or `none`. |
| `coverage_policy` | string | `always_abstain_v0` or `calibrated`. |
| `results` | array[object] | Descending score order, capped at `top_n`. |
| `elapsed_ms` | object | Numeric `total`, `pregate`, `engine`, and `overhead`. |
| `escalation_reason` | string/null | `margin_below_tau`, `long_candidates`, `n_gt_local_cap`, or null. |
| `error_kind` | string/null | Error taxonomy value or null. |
| `notes` | array[string] | Human-readable notes without secrets. |

Each result object contains:

```json
{
  "id": "c3",
  "index": 3,
  "relevance_score": 0.92933786
}
```

`index` always refers to the original input order. Results are sorted by
`relevance_score` descending. Every numeric field is a JSON number or null;
never emit `NaN` or `Infinity`.

## Status values

| Status | Meaning | Exit |
|---|---|---:|
| `selected` | Above a fitted threshold and safe for host policy | 0 |
| `trivial` | Pregate short-circuited; no model ran | 2 |
| `needs_review` | Ranked, but coverage policy forbids auto-action | 2 |
| `abstain_tie` | Top two scores are exactly equal | 2 |
| `invalid_response` | Engine returned malformed output; fail closed | 2 |
| `engine_error` | Transport, authentication, timeout, or quota failure; fail open | 2 |

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Selected; impossible in v0 unless calibrated policy is fitted and enabled |
| 1 | Input validation failure, including bad JSON, cap breach, duplicate ID, or empty text |
| 2 | Needs review, trivial, tie, malformed engine response, or provider failure |

Validate input before calling the engine. Wrap only the engine call in the
fail-open handler. Provider failures must never become exit code 1.

## Error taxonomy

`auth`, `timeout`, `http`, `dns`, `tls`, `connection`, `malformed`,
`rate_limited`, `daemon_unavailable`, `daemon_starting`,
`daemon_version_mismatch`, or `null`.

## Coverage policy

`always_abstain_v0` is the default and returns exit code 2 for every
non-trivial decision. The ranking and diagnostics are still returned.

`calibrated` requires a valid `eval/calibration.json` produced from the user's
labelled data. Ties abstain under both policies. No raw score threshold is
valid until calibration and a held-out selective-risk evaluation establish
one.
