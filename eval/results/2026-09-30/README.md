# Eval results — 2026-09-30

Published snapshot of the v0.2.0 evaluation runs on one Windows machine. All prompts are
synthetic and author-labelled; nothing here comes from captured user data. These are
regression and speed signals, not a calibrated selective-risk claim.

| File | Produced by | What it shows |
|---|---|---|
| `functional-eval.json` | `eval/scripts/functional_eval.py` | 11/11 functional checks pass: routing, `noul`, `screen`, gate, hook |
| `llm-vs-eljev-lean.json` | `eval/scripts/llm_vs_eljev.py --arm lean` | 30 prompts; LLMs with MCP servers, built-in MCPs and custom instructions disabled |
| `llm-vs-eljev-full.json` | `eval/scripts/llm_vs_eljev.py --arm full` | 10 prompts; LLMs with the full real Copilot workspace context loaded |

## el-jev vs LLM sub-agents: the five-way intent decision

Both sides answer the question the hook asks on every turn: classify the prompt into one of
`code_modification`, `review_audit`, `investigation_search`, `execution_testing`,
`advisory_explanation`.

| System | Accuracy (lean, 30 prompts) | Lean p50 / p95 | Full-context p50 / p95 (10 prompts) |
|---|---|---|---|
| **el-jev** decision (warm daemon, Cohere rerank v4.0 pro) | 29/30 | **183 ms / 289 ms** | **183 ms / 351 ms** |
| el-jev hook process (Python start + decision) | — | 609 ms / 746 ms | 716 ms / 953 ms |
| general-purpose sub-agent (`gemini-3.8-flash`, high) | 30/30 | 3.64 s / 9.72 s | 5.12 s / 10.48 s |
| task sub-agent (`gpt-5.6-luna`, max) | 30/30 | 1.70 s / 6.92 s | 3.84 s / 5.76 s |

el-jev does not load tool context, so its two columns are simply two separate runs. All systems
also scored 10/10 in the full-context arm.

Speed-up at the median, full context:

| Comparison | vs general-purpose | vs task |
|---|---|---|
| el-jev decision only | ~28× | ~21× |
| el-jev hook as it runs today (~1.5 s per turn with the PowerShell runner) | ~3.4× | ~2.6× |

Worst case: el-jev's slowest decision was 0.39 s. The LLMs' slowest were 12.0 s
(`gemini-3.8-flash`) and 24.2 s (`gpt-5.6-luna`).

### Cost of loading the full tool context

Paired on the 10 prompts present in both arms, loading the real workspace context added a
median **+1.23 s** to `gemini-3.8-flash` (slower on 7/10) and **+1.92 s** to `gpt-5.6-luna`
(slower on 9/10). An LLM routing decision inside a real session pays for the tool definitions
and instructions; el-jev does not.

### Accuracy

The LLMs answered all 30 correctly. el-jev missed one: *"Implement caching for the token
minting call in the client"* went to `advisory_explanation` with low confidence (p = 0.39,
margin 0.15). That low-confidence signal is exactly what a fitted calibration would abstain on.

## How to read this

- **LLM decision time** is the summed `model.call_finished.dispatchDurationMs`. It includes
  network and queueing, but not CLI or MCP start-up; median end-to-end `copilot -p` wall time
  was 46–57 s per call, dominated by session start.
- **el-jev decision time** is the warm daemon `/v1/decide` round trip.
- el-jev saves time only where it **replaces** an LLM decision: a router, a gate, a sub-agent or
  tool choice. As a pre-turn advisory the LLM still runs, so today the hook adds ~1.5 s per turn.
  The hook's process start-up is the main gap between the ~20× and ~3× numbers.

## Caveats

- Each LLM call was a fresh session. A long-lived session caches the system prompt after the
  first turn, which should shrink the full-context penalty.
- The lean and full arms ran at the same time, with up to five concurrent Copilot sessions.
- Small synthetic sample: 30 prompts in the lean arm, 10 in the full arm. The 95% interval on
  el-jev's 29/30 is roughly 83–99%.
- One machine, one region (`southcentralus` for Cohere); treat as indicative, not an SLA.

## Reproduce

```powershell
python -m eljev daemon start
python eval\scripts\functional_eval.py
python eval\scripts\llm_vs_eljev.py --arm lean
python eval\scripts\llm_vs_eljev.py --arm full --cwd <your-workspace> --per-intent 2 --workers 2
```

Reports land in the git-ignored `eval/reports/`. Every LLM call is a billed Copilot request.
