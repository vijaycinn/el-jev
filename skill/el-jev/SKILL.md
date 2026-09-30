---
name: el-jev
description: "Use el-jev whenever a user needs a bounded, typed, auditable decision from candidates already in hand: choose one of a short list, route a message, triage work, or rerank 1-250 open candidates against a criterion. Trigger for requests to screen, pick, rank, prioritize, decide, or explain a decision through the el-jev daemon, CLI, MCP, hook, or skill. Teach Shape A for 5-30 short options and Shape B for 1-250 candidates capped at 2,000 characters each and 100,000 total. Use this skill to shape fuzzy asks into deterministic candidate-selection asks, validate caps before calling, and report the v0 needs_review verdict honestly. Do not use el-jev to discover candidates with an LLM, write prose, do arithmetic, grant permissions, execute actions, or replace host authorization. Treat every v0 non-trivial result as abstention: ranking is evidence only, never approval. Start the daemon first and diagnose daemon_unavailable rather than inventing a result."
user-invocable: true
---

# el-jev

Use el-jev as a typed decision sidecar. It returns a bounded, auditable
`eljev.decision/1` record. It does not replace the host that authorizes,
executes, or completes work.

## Honest posture

The conversational skill is not the latency path. Agent-discretion invocation
added a measured 10.4 seconds median per task against a 465 ms model. The
hook/in-process path is the path that can realize the warm-daemon speed
measurement. The skill is useful when a human asks for a decision because it
shapes the task and exposes a typed, capped, auditable verdict.

Run the daemon before calling it:

```powershell
eljev daemon start
```

`daemon_unavailable` means the client could not reach the local daemon. It is
not a model answer and it is not input validation. Report the failure, keep
host control, and start or repair the daemon before retrying.

## Choose the decision shape

| Shape | Use it for | Input | v0 backend |
|---|---|---|---|
| Shape A | Short typed decisions | State plus 5-30 short options, or a boolean/noul question | Configured local `/v1/systemone`; otherwise not implemented |
| Shape B | Open reranking | One criterion plus 1-250 candidates, each at most 2,000 characters and 100,000 total | Azure Cohere rerank over a kept-alive connection |

Shape A is for a small known label set: route, triage, classify, or decide
whether to escalate. Shape B is for an open list: rank work items against a
criterion such as "which item is most urgent to act on today."

The caps are hard validation rules:

- Candidate count is 1-250.
- Each candidate is at most 2,000 characters.
- Total candidate text is at most 100,000 characters.
- Candidate IDs must be unique.
- Candidate text and the criterion must be non-empty.

Violations are rejected with exit code 1. They are never truncated, silently
repaired, or sent to a model.

## v0 abstention is deliberate

The default coverage policy is `always_abstain_v0`. Every non-trivial decision
returns:

- `status: needs_review`
- `exit_code: 2`
- the ranking, scores, tier path, timings, and diagnostics attached

The model must not read the ranking as an approved decision. `choice` is the
top-ranked candidate when one exists, not permission to act. Ties return
`abstain_tie`, also with exit code 2. Provider failures return `engine_error`
with exit code 2. Malformed provider output returns `invalid_response` with
exit code 2.

Calibration is not fitted in v0. Do not enable
`ELJEV_COVERAGE_POLICY=calibrated` until the evaluation workflow has fitted and
locked calibration on the user's own labelled data.

## Shape the task before calling

An el-jev-shaped task has candidates already in hand, or candidates obtainable
with one cheap deterministic pull. A database query, fixed API request, or
already-loaded list is acceptable. If an LLM or subagent must discover,
summarize, or assemble the candidates, the task is not el-jev-shaped yet.
Resolve discovery first, then call el-jev on the resulting bounded list.

| Fuzzy ask | el-jev-shaped ask |
|---|---|
| "What should I work on today?" | "From these 50 open work items, which is most urgent to act on today?" |
| "Find the best customer to contact." | "From this deterministic account export, rank the 25 accounts against `renewal risk this quarter`." |
| "Decide how to handle this email." | "Given this message and the fixed options `reply`, `delegate`, `FYI`, `urgent`, choose one." |
| "Look through our systems and pick a deal." | "From this already-fetched opportunity list, rank up to 250 records against `next action likely to unblock close`." |
| "Use an agent to gather candidates and then rank them." | "Have the host gather candidates first; then pass the bounded candidate array to Shape B." |

Do not use this skill for prose generation, arithmetic, candidate discovery,
permission grants, authorization, execution, or completion. Relevance is not
urgency, and a ranking is not a policy decision.

## Common path

1. Confirm the candidates and criterion are already available.
2. Select Shape A or Shape B.
3. Validate every cap before making a request.
4. Ask the user or host to run `eljev daemon start`.
5. Call the daemon through the host's configured client or MCP front door.
6. Return the decision record and explain `status`, `exit_code`, and
   `error_kind`.
7. Leave authorization, execution, and completion to host code.

Read these references when needed:

- `references/decision-record.md` for the full `eljev.decision/1` schema.
- `references/task-shaping.md` for candidate preparation and shape selection.
- `references/troubleshooting.md` for daemon, Azure auth, route, quota, and
  pidfile failures.

## Do not overclaim

The live evidence showed a top-1/top-2 raw margin of `0.00045019` and exact
ties, including `0.64027600` four times in one response. These observations
justify abstention; they do not prove that a future calibrated policy will
achieve useful coverage. Say when a claim is measured, unknown, or awaiting
the user's labelled evaluation corpus.
