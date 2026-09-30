# Decision record reference

`el-jev` returns `eljev.decision/1` for all decision shapes.
v0.2.0 keeps the same schema string and adds Shape A fields.

## Base record (Shape B)

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
  "choice": "bug-3",
  "choice_index": 2,
  "status": "needs_review",
  "exit_code": 2,
  "raw_top_score": 0.93,
  "raw_runner_up": 0.91,
  "margin_raw": 0.02,
  "calibrated_probability": null,
  "margin_calibrated": null,
  "coverage_policy": "always_abstain_v0",
  "results": [
    { "id": "bug-3", "index": 2, "relevance_score": 0.93 }
  ],
  "error_kind": null,
  "notes": []
}
```

## Shape A additive fields

| Field | Meaning |
|---|---|
| `question_id` | Request question id |
| `kind` | `choice`, `noul`, or `score` |
| `confidence` | Top calibrated probability |
| `probabilities` | Option probability map |
| `margin` | Top minus runner-up probability |
| `noul` | Boolean probability map for noul |
| `score` | Expected 1-based score for score |

Example (`choice`):

```json
{
  "schema": "eljev.decision/1",
  "shape": "decide",
  "question_id": "route-task",
  "kind": "choice",
  "choice": "review_audit",
  "confidence": 0.698,
  "margin": 0.581,
  "probabilities": {
    "review_audit": 0.698,
    "investigation_search": 0.117,
    "code_modification": 0.086
  },
  "status": "needs_review",
  "exit_code": 2
}
```

## Status rules

| Status | Exit | Meaning |
|---|---:|---|
| `selected` | 0 | Calibrated gate pass |
| `needs_review` | 2 | Advisory |
| `abstain_tie` | 2 | Exact tie |
| `trivial` | 2 | Pregate short-circuit |
| `engine_error` | 2 | Remote failure |
| `invalid_response` | 2 | Malformed output |
| `invalid_input` | 1 | Validation failure |

Under default `always_abstain_v0`, non-trivial results remain advisory.

