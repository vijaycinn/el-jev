# Task-shaping guide

`el-jev` works after candidates are already bounded.
It does not discover candidates for you.

## One-cheap-pull rule

Use `el-jev` only when candidates are:

1. already available in the request/context, or
2. retrievable in one deterministic pull (single query, fixed API call, known file read).

If discovery needs an LLM or multi-step search, perform discovery first.

## Shape selection

| Shape | Candidate form | Typical count | Use case |
|---|---|---:|---|
| `choice` | fixed options | 2-250 | routing/classification |
| `noul` | boolean framing | 2 semantic outcomes | yes/no gate |
| `score` | ordered levels | 2-10 | severity/priority scoring |
| `screen` | open records/snippets | 1-250 | reranking |

## Rewrite examples

| Fuzzy ask | Typed ask |
|---|---|
| "What should I do next?" | "Choose one of `reply`, `delegate`, `FYI`, `urgent`." |
| "Find urgent work." | "Rank these 40 items against `urgent action needed today`." |
| "How severe is this incident?" | "Score this incident against levels `sev4`..`sev1`." |
| "Pick the best account." | "Rank this deterministic export of 25 accounts for renewal risk." |

## Candidate preparation

- Keep stable candidate ids.
- Keep text decision-relevant and secret-safe.
- Preserve original order (`choice_index` refers to original array).
- Enforce caps before call (count, per-item chars, total chars).
- Do not compare raw scores across separate calls.

## Gate interpretation

- Default policy is advisory (`always_abstain_v0`).
- In calibrated mode, thresholds are read from calibration data (`threshold`, `margin_threshold`) per kind.
- Do not hardcode numeric thresholds in task logic.
- A ranked/selected candidate is still subject to host authorization rules.

