---
name: el-jev
description: "Use el-jev when the user needs a bounded, typed decision over known options or candidates already in hand: choose, classify, triage, rerank, or score. Route to Shape A for `choice`, `noul`, or `score`, and Shape B for open screening (1-250 candidates). This skill is for deterministic decision tasks, not candidate discovery, prose generation, or execution authority. It defaults to advisory abstention (`always_abstain_v0`), supports calibrated selection only with valid per-kind calibration, and treats malformed/failed engine responses as non-selecting outcomes. Use `python -m eljev configure` and `python -m eljev install-hook` for setup, and explain intent/marker hook modes when pre-turn integration is relevant."
user-invocable: true
---

# el-jev

Use el-jev when the host already has bounded candidates and needs a typed, auditable decision record.

## Core behavior

- Shape A is built in: Cohere-backed `choice`, `noul`, and `score` (no external `/v1/systemone` required).
- Optional passthrough remains available through `ELJEV_SYSTEMONE_URL`.
- Default policy is `always_abstain_v0`, so non-trivial decisions are advisory (`exit_code: 2`).
- `selected` (`exit_code: 0`) requires calibrated mode and valid calibration for that kind.
- Malformed engine output is fail-closed (`invalid_response`), and remote failures stay advisory (`engine_error`).

## Hook behavior

- `intent` mode: classify eligible prompts as a five-intent `choice`.
- `marker` mode: act only on explicit marker payloads.
- Hook timeout/min-word controls: `ELJEV_HOOK_TIMEOUT_MS`, `ELJEV_HOOK_MIN_WORDS`.
- If daemon is unavailable, hook debounces spawn and returns `{}` quickly.

## Setup path

```powershell
python -m eljev configure --endpoint https://<your-resource>.services.ai.azure.com
python -m eljev daemon start
python -m eljev install-hook --scope user
```

```bash
python -m eljev configure --endpoint https://<your-resource>.services.ai.azure.com
python -m eljev daemon start
python -m eljev install-hook --scope user
```

## Shape guidance

| Shape | Use when |
|---|---|
| `choice` | fixed options, single best label |
| `noul` | binary yes/no style gate |
| `score` | ordered severity/priority levels |
| `screen` | open candidate list ranking |

Thresholds are runtime data from calibration, not fixed constants.

## References

- `references/decision-record.md`
- `references/task-shaping.md`
- `references/troubleshooting.md`

