# `eljev.decision/1` decision record

`el-jev` emits `eljev.decision/1` for Shape A (`choice`, `noul`, `score`) and Shape B (`screen`).
The schema string is unchanged in v0.2.0; Shape A fields are additive.

## Base contract example (Shape B)

```json
{
  "schema": "eljev.decision/1",
  "decision_id": "uuid4",
  "ts": "2026-09-30T15:04:01.000Z",
  "shape": "screen",
  "criterion": "most urgent item to fix",
  "n_candidates": 5,
  "tier_path": ["systemone", "cohere"],
  "engine": "cohere-rerank-v4.0-pro",
  "engine_version": "1",
  "choice": "bug-3",
  "choice_index": 2,
  "status": "needs_review",
  "exit_code": 2,
  "raw_top_score": 0.93,
  "raw_runner_up": 0.91,
  "margin_raw": 0.02,
  "calibrated_probability": null,
  "margin_calibrated": null,
  "calibration_version": "none",
  "coverage_policy": "always_abstain_v0",
  "results": [
    { "id": "bug-3", "index": 2, "relevance_score": 0.93 },
    { "id": "bug-2", "index": 1, "relevance_score": 0.91 }
  ],
  "elapsed_ms": {
    "total": 188.2,
    "pregate": 0.1,
    "engine": 183.0,
    "overhead": 5.1
  },
  "escalation_reason": null,
  "error_kind": null,
  "notes": []
}
```

## Shape A additive fields

Shape A adds:

| Field | Meaning |
|---|---|
| `question_id` | Question identifier from typed request |
| `kind` | `choice`, `noul`, or `score` |
| `confidence` | Top probability after calibration transform |
| `probabilities` | Mapping of option id to probability |
| `margin` | Top minus runner-up probability |
| `noul` | `{ "true": p, "false": p }` for noul requests |
| `score` | Expected 1-based score for score requests |

## Shape A example (`choice`)

```json
{
  "schema": "eljev.decision/1",
  "decision_id": "uuid4",
  "ts": "2026-09-30T15:06:12.000Z",
  "shape": "decide",
  "question_id": "route-task",
  "kind": "choice",
  "tier_path": ["systemone", "cohere"],
  "engine": "cohere-rerank-v4.0-pro",
  "choice": "review_audit",
  "choice_index": 1,
  "confidence": 0.698,
  "margin": 0.581,
  "probabilities": {
    "review_audit": 0.698,
    "investigation_search": 0.117,
    "code_modification": 0.086,
    "execution_testing": 0.062,
    "advisory_explanation": 0.037
  },
  "raw_top_score": 0.91,
  "raw_runner_up": 0.84,
  "margin_raw": 0.07,
  "calibrated_probability": 0.698,
  "margin_calibrated": 0.581,
  "status": "needs_review",
  "exit_code": 2,
  "error_kind": null,
  "notes": []
}
```

## Status and exit behavior

| Status | Exit | Meaning |
|---|---:|---|
| `selected` | 0 | Calibrated gate passed |
| `needs_review` | 2 | Advisory outcome |
| `abstain_tie` | 2 | Exact tie |
| `trivial` | 2 | Pregate short-circuit |
| `engine_error` | 2 | Remote failure (advisory) |
| `invalid_response` | 2 | Malformed engine output (fail closed) |
| `invalid_input` | 1 | Contract/input validation failure |

Default policy `always_abstain_v0` keeps non-trivial outcomes advisory until calibrated mode is enabled with valid per-kind calibration.

