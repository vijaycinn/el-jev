# Task-shaping guide

el-jev is a decision sidecar. It is useful after the host has a bounded set of
candidate records. It is not a search engine or an agent for discovering what
the candidates should be.

## The one-cheap-pull rule

A task is el-jev-shaped when the candidates are:

1. already in memory or in the current request; or
2. obtainable with one cheap, deterministic pull such as a fixed SQL query,
   one API request, or a known file read.

It is not el-jev-shaped when an LLM, subagent, multi-step search, or human
research process must assemble the candidate set. Make discovery a separate
host step. Then pass only the bounded result into el-jev.

## Shape selection

| Question | Shape A | Shape B |
|---|---|---|
| Candidate form | Known labels or short options | Open records or snippets |
| Typical count | 5-30 | 1-250 |
| Typical request | Route, classify, choose, boolean/noul | Rank or screen against a criterion |
| Backend | Local `/v1/systemone` if configured | Cohere rerank deployment |
| Important limit | Option descriptions must stay short | 2,000 chars each and 100,000 total |

Use Shape A when the host can name the complete answer set before the call.
Use Shape B when the answer set is open and the host has the records.

## Rewrite examples

| Do not ask | Ask instead |
|---|---|
| "What should I do next?" | "Choose one of `reply`, `delegate`, `FYI`, or `urgent` for this message." |
| "Find urgent work." | "Rank these 40 open items against `urgent action needed today`." |
| "Pick the best account from our CRM." | "Rank this deterministic export of 25 accounts against `renewal risk this quarter`." |
| "Search Teams, email, and CRM for a deal." | "The host fetched these 50 opportunities; rank them against `next action likely to unblock close`." |

## Candidate preparation

- Preserve a stable candidate ID.
- Keep the text decision-relevant and redact secrets before the call.
- Preserve original order; the returned `choice_index` uses it.
- Count characters before calling. A violation is rejected, not truncated.
- Do not compare raw scores from separate calls.
- Do not turn a relevance ranking into an urgency or authorization rule.

## Host boundary

The host owns authorization, execution, completion, retries outside the
engine contract, and any user-visible action. el-jev only advises. A
`choice` field can identify the top-ranked item while `exit_code: 2` says the
host must not auto-act under v0.

## Evidence boundary

The measured live distribution had a top-1/top-2 raw margin of `0.00045019`,
and exact ties including `0.64027600` four times. v0 therefore attaches the
ranking but abstains. Calibration must be fitted on the user's own labelled
data before any policy can return exit code 0.
