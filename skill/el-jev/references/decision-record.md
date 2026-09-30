# Decision record reference

el-jev emits and returns `eljev.decision/1` records. The record is an audit
object, not an authorization token.

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
    { "id": "c3", "index": 3, "relevance_score": 0.92933786 }
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

## Field rules

| Field | Rule |
|---|---|
| `schema` | Always `eljev.decision/1`. |
| `decision_id` | UUID4 identifying this decision. |
| `ts` | UTC ISO-8601 timestamp with milliseconds. |
| `shape` | `screen`, `decide`, or `pregate`. |
| `criterion` | The non-empty decision criterion. |
| `n_candidates` | Number of input candidates after validation. |
| `tier_path` | Ordered tiers actually executed. |
| `engine` | Engine name, or `null` when no model ran. |
| `engine_version` | Engine version identifier. |
| `choice` | Candidate ID, or `null`. It is not approval. |
| `choice_index` | Index in the original candidate array, or `null`. |
| `status` | See the status table below. |
| `exit_code` | `0`, `1`, or `2`, as defined below. |
| `raw_top_score` | Highest raw score, or `null`. |
| `raw_runner_up` | Second-highest raw score, or `null`. |
| `margin_raw` | Top score minus runner-up, or `null`. |
| `calibrated_probability` | Fitted probability, or `null` before calibration. |
| `margin_calibrated` | Calibrated margin, or `null` before calibration. |
| `calibration_version` | Calibration identifier, or `none`. |
| `coverage_policy` | `always_abstain_v0` or `calibrated`. |
| `results` | Descending score order, capped at requested `top_n`. |
| `elapsed_ms` | Numeric total, pregate, engine, and overhead timings. |
| `escalation_reason` | `margin_below_tau`, `long_candidates`, `n_gt_local_cap`, or `null`. |
| `error_kind` | Error taxonomy value or `null`. |
| `notes` | Human-readable notes. Never include secrets. |

`choice_index` always refers to the original input order. A tier must not
reorder the input before indexing. Every numeric field is a JSON number or
`null`; never emit `NaN` or `Infinity`.

## Status and exit code

| Status | Meaning | Exit |
|---|---|---:|
| `selected` | Above a fitted threshold and safe for the host's policy | 0 |
| `trivial` | Local pregate short-circuited; no model ran | 2 |
| `needs_review` | Ranked, but policy forbids auto-action | 2 |
| `abstain_tie` | Top two raw scores are exactly equal | 2 |
| `invalid_response` | Engine response was malformed; fail closed | 2 |
| `engine_error` | Transport, auth, timeout, or quota failure; fail open | 2 |

Exit code `1` is reserved for input validation: bad JSON, cap breaches,
duplicate IDs, empty text, or similar caller errors. Provider failures must
never become exit code 1. v0 never returns exit code 0.

## Input caps

- Candidate count: 1-250.
- Characters per candidate: at most 2,000.
- Total candidate characters: at most 100,000.
- Candidate IDs: unique.
- Candidate text: non-empty after whitespace handling.
- Criterion: non-empty string.

Reject violations before engine work. Do not truncate to fit.
