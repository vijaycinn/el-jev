# Changelog

## Unreleased

### Added

- Added `eval/scripts/functional_eval.py`, a live smoke/regression eval: 30-prompt intent routing, noul and 50-candidate screen accuracy, latency percentiles, calibrated/fail-closed gate checks, and hook end-to-end. Exits 0 only when every check passes; the JSON report goes to the git-ignored `eval/reports/`.
- Added `eval/scripts/llm_vs_eljev.py`, which times the same intent decision on el-jev and on Copilot CLI sub-agent LLMs, in a `lean` (tools stripped) or `full` (real workspace context) arm.
- Published the 2026-09-30 functional and LLM-vs-el-jev results in `eval/results/2026-09-30/`, and summarised el-jev vs LLM decision speed in the README.
- Added `eljev configure --hook-timeout-ms` and `config.hook_timeout_ms()`, and raised the default hook timeout from 500 ms to 750 ms to absorb Cohere tail latency.

### Fixed

- Fixed `.gitignore` so `eval/scripts/capture.py` is published; the `**/*capture*` data rule had excluded it, leaving step 1 of the eval harness missing from clones.

## 0.2.0 - 2026-09-30

### Added

- Added `eljev/config.py` and repo-local runtime state management in `.eljev/`.
- Added `configure`, `install-hook`, `uninstall-hook`, `on`, `off`, `status`, and improved daemon lifecycle commands.
- Added built-in Shape A decision path for `choice`, `noul`, and `score` with additive `eljev.decision/1` fields.
- Added daemon `/v1/shutdown` endpoint and richer `/health` payload (`pid`, `warm`, `auth`, engine states).
- Added managed identity auth support (IMDS and App Service identity endpoints).
- Added `configure --subscription` / `ELJEV_AZURE_SUBSCRIPTION` to mint az CLI tokens with the account that owns the Foundry resource, independent of the `az` default account.

### Changed

- Changed config resolution order to environment variable > `config.json` > default.
- Changed default gate behavior to explicit advisory abstention (`always_abstain_v0`).
- Changed hook behavior to `intent`/`marker` modes with 500 ms budget and debounced daemon spawn.
- Changed scripts (`scripts/copilot.ps1`, `scripts/eljev.ps1`, `scripts/install.ps1`) to portable path handling and CLI-driven hook installation.

### Fixed

- Fixed stale keep-alive first-call failures with a targeted retry on idle connection closure.
- Fixed lifecycle reliability with daemon-owned JSON pidfile, exclusive port bind, and orphan detection by `/health` pid.
- Fixed `daemon stop` so it never terminates a recycled pid: pidfile kills require the process creation time to match the daemon's recorded start.
- Fixed Shape A error handling to fail closed on malformed engine output instead of fabricating high confidence from heuristics.
- Fixed a fail-open where a partial provider ranking was zero-filled into a wide softmax margin; a ranking that omits candidates is now `invalid_response`.
- Fixed token refresh and warm-up blocking request threads: a valid cached token is served while a refresh runs, and warm-up skips when a request holds the connection.
- Fixed retry backoff holding the connection lock; server-error retries are capped at four attempts.
- Fixed the first request after an idle period paying reconnect + retry: warm-up (every 30 s) now detects a keep-alive connection the server closed and reconnects in the background.
- Fixed `install-hook` emitting `timeoutSec` as a float.
- Fixed logging and status reporting to show effective setting source and env overrides.

### Security

- Added request hardening for loopback daemon: reject `Origin`, reject non-loopback `Host`, reject non-JSON POST payloads.
- Preserved local logging redaction defaults and no prompt-text hook logs.

### Removed

- Removed generic auto-noul routing from hook intent flow.
- Removed hardcoded endpoint and machine-specific path assumptions from docs/scripts.
- Removed fixed gate-threshold behavior from runtime decisioning; thresholds now come from calibration only.
- Removed silent fallback that previously converted remote failures into high-confidence heuristic decisions.

## 0.1.0 - 2026-09-24

- Documented the v1 contract and `eljev.decision/1` record.
- Added the conversational el-jev skill with Shape A and Shape B task-shaping
  guidance.
- Documented the verified Azure Cohere route and Entra-only authentication.
- Documented deliberate v0 abstention with `always_abstain_v0`.
- Added greenfield Windows 11 bootstrap instructions, architecture,
  troubleshooting, evaluation guidance, and repository hygiene rules.
