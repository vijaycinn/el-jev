# el-jev v2 improvement plan

## Need to have

| # | Finding | Severity | Resolution in 0.2.0 | Status |
|---:|---|---|---|---|
| 1 | Heuristic fallback could emit a near-certain "decision" when Cohere failed. | Critical | Fail-closed `engine_error`; heuristic used only when no endpoint and never selected. | Done |
| 2 | Four inconsistent gates existed (`0.60/0.10`, `0.50`, sigmoid variant, docs mismatch). | Critical | Single `apply_gate` in `verdict.py`; thresholds read only from calibration; default policy abstains. | Done |
| 3 | Hook routed almost every prompt, generic auto-noul carried no signal, 1.5s budget was expensive. | Important | Intent `choice` routing only for prompts meeting bounds or explicit markers, generic auto-noul removed, 500ms budget, `marker` mode available. | Done |
| 4 | Lifecycle was brittle: orphan daemons, missing pidfile from spawn path, Windows liveness checks were fragile. | Important | Exclusive bind, daemon-owned JSON pidfile, `/v1/shutdown`, Windows process checks, debounced hook spawn without sleep, stop discovers orphans via `/health` pid and never kills a pid it cannot prove is el-jev (process start time vs recorded start). | Done |

## Good to have

| # | Finding | Severity | Resolution in 0.2.0 | Status |
|---:|---|---|---|---|
| 5 | Hardcoded endpoint/path/user-machine values reduced portability. | Important | Added `config.py`, `eljev configure`, portable scripts, and `install-hook`. | Done |
| 6 | Earlier accuracy statement overstated evidence. | Important | Removed accuracy claim; accuracy remains unknown until labelled evaluation is completed. | Done |
| 7 | macOS/Linux behavior had not been validated in CI. | Important | Platform code paths are guarded (Windows registry access, POSIX detach, signal handling) but CI execution is still pending. | Partial |
| 8 | Missing tests for Shape A Cohere path, hook routing, switch behavior, and fail-open/fail-closed boundaries. | Important | Added tests: `test_config`, `test_gate`, `test_systemone`, `test_cohere_client`, `test_daemon`, `test_hook`, `test_install_hook`, `test_review_regressions`, plus lifecycle tests in `test_cli_exitcodes`. | Done |
| 9 | First decision could pay a 1-2s token minting penalty. | Important | Added background warm-up thread for token + connection priming. | Done |
| 10 | Browser/DNS-rebinding could trigger paid daemon calls. | Critical | Added `Origin`, `Host`, and `Content-Type` request checks. | Done |
| 11 | Managed identity was claimed but not implemented. | Important | Implemented managed identity auth path for IMDS/App Service, including user-assigned support. | Done |
| 12 | Stale keep-alive reuse produced spurious first-call errors. | Important | Added one retry path for idle connection closure. | Done |

### Found by the second review (fixed before release)

| # | Finding | Severity | Resolution in 0.2.0 | Status |
|---:|---|---|---|---|
| 13 | A provider response that omitted candidates was zero-filled, which sharpened the softmax into an exit 0. | Critical | Partial rankings now fail closed as `invalid_response`. | Done |
| 14 | `daemon stop` could terminate an unrelated process through a stale pidfile (pid reuse). | Critical | JSON pidfile with start time; a pidfile pid is only terminated when process creation time proves ownership. | Done |
| 15 | Token refresh and warm-up could block request threads for seconds. | Important | Non-blocking lock acquisition; a still-valid token is served while a refresh runs. | Done |
| 16 | A repo-scoped hook file with absolute local paths was not git-ignored, and `timeoutSec` was a float. | Important | `.github/hooks/eljev.json` is ignored; `timeoutSec` is an integer. | Done |

## Open / next

| Item | Why it remains | Status |
|---|---|---|
| Per-kind calibration fitting for `choice`, `noul`, `score` in `eval/scripts/calibrate.py` | Fitter currently emits `screen` calibration only; per-kind Shape A blocks are manual. | Open |
| macOS/Linux CI execution | Platform code paths are present but not yet exercised in automated runs. | Open |
| Optional OpenTelemetry export of decision records to Application Insights (Foundry tracing) | Useful for fleet observability, but should stay opt-in to preserve local-first logging defaults. | Open |
| Labelled corpus + S1 oracle-ceiling gate before enabling calibrated auto-select | Needed to move from advisory-by-default to defensible selective automation. | Open |
| Async/concurrent engine gate path | Current remote call path is one decision call at a time. | Open |
| Hook process start-up dominates hook cost (~0.65 s PowerShell + ~0.55 s Python per Copilot turn on Windows) | Every turn launches a new shell and Python process; a persistent hook host, a compiled shim, or a direct `exec` hook entry would remove most of it. This is the biggest remaining latency item. | Open |
| First decision after daemon start exceeds the 500 ms hook budget (~0.7 s measured) | Warm-up mints the token and opens TLS, but the first rerank is still slow; one warm-up rerank at start (one billed search unit) would close the gap. | Open |

## Operational hygiene

After troubleshooting, remove extra role assignments that are no longer required and keep only **Cognitive Services User** at resource scope. Regenerate any resource keys that were exposed during debugging sessions; key auth can remain disabled while rotation still improves hygiene.
