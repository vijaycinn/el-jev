# el-jev Copilot CLI hook

`eljev_pre_turn.py` is the deterministic, fail-open hook path for el-jev. It
uses a warm local daemon over a small raw-socket HTTP client. It does not import
`http.client`, does not use `argparse`, and never prints diagnostics to stdout.
The default timeout is **250 ms** (`ELJEV_HOOK_TIMEOUT_MS`).

## Copilot CLI mechanism

Copilot CLI has a native hook that fits the pre-turn requirement:

- `userPromptTransformed` fires after prompt transformation and immediately
  before the model-facing content is emitted.
- A command hook may return `modifiedTransformedPrompt`.
- `userPromptSubmitted` is not suitable for this job: command-configured
  `modifiedPrompt` output is explicitly dropped.

This is documented in the official references:

- [Using hooks with GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks)
- [Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference)

Hooks are loaded from `.github/hooks/*.json` in a repository or
`%USERPROFILE%\.copilot\hooks\*.json` for user-level hooks. The installer does
not edit either existing MCP configuration or existing hook configuration. It
only prints the exact blocks and, when explicitly requested, can add a new
user-level hook file.

## Opt-in behavior

The hook is **off by default**. Set:

```powershell
$env:ELJEV_HOOK_ENABLED = "1"
```

The hook does nothing when the variable is absent or false. Transport failures,
timeouts, malformed responses, and invalid input all return `{}` and exit
successfully, so a user's turn continues unchanged. Diagnostics go to
`$env:ELJEV_LOG_DIR` or `~\.eljev\logs\hook.jsonl`; candidate text, tokens, and
authorization data are never logged.

## Native Copilot payload

Configure the hook on `userPromptTransformed`. Copilot supplies a payload like:

```json
{
  "sessionId": "session-id",
  "timestamp": 1769340000000,
  "cwd": "C:\\workspace\\project",
  "prompt": "user prompt",
  "transformedPrompt": "model-facing prompt"
}
```

The native event has no typed-decision fields. A wrapper therefore embeds this
marker in `transformedPrompt` when it already has deterministic candidates:

```text
<!--eljev.request:{"task_id":"task-17","criterion":"most urgent","candidates":[{"id":"c1","text":"..."}],"top_n":1,"tier":"auto"}-->
```

The hook removes the marker, calls `POST /v1/screen`, and appends a compact
decision context to the model-facing prompt. Without the marker, it returns
`{}`. Shape A is also supported by supplying `"path":"/v1/decide"`,
`"state"`, and `"question"` in the marker object.

The daemon target is `http://127.0.0.1:8787` by default. Override it with
`ELJEV_HOOK_DAEMON_URL`, or use `ELJEV_HOST` and `ELJEV_PORT`.

## Generic wrapper contract

A harness or host wrapper can call the same script with a JSON object on stdin.
This avoids inventing a second hook API:

```json
{
  "schema": "eljev.hook/1",
  "event": "pre_turn",
  "task_id": "task-17",
  "criterion": "most urgent",
  "candidates": [
    {"id": "c1", "text": "item one"},
    {"id": "c2", "text": "item two"}
  ],
  "top_n": 1,
  "tier": "auto"
}
```

The single stdout line on success is:

```json
{
  "schema": "eljev.hook_result/1",
  "task_id": "task-17",
  "decision": {"schema": "eljev.decision/1"}
}
```

For native `userPromptTransformed`, the single stdout line instead uses
Copilot's required `{"modifiedTransformedPrompt":"..."}` shape. Empty output is
not used: `{}` is emitted so the hook parser always receives one valid JSON
object.

## Hook configuration

The installer prints, but does not write, a configuration block like this:

```json
{
  "version": 1,
  "hooks": {
    "userPromptTransformed": [
      {
        "type": "command",
        "exec": "python",
        "args": ["<path-to-el-jev>/hooks/eljev_pre_turn.py"],
        "timeoutSec": 0.25
      }
    ]
  }
}
```

The script remains opt-in after installation because
`ELJEV_HOOK_ENABLED` defaults to off. Review the path and add the block to a
new user-level hook file or repository hook file as appropriate.

## S1 oracle-ceiling JSONL

`scripts/oracle_ceiling.py` accepts canonical `eljev.oracle/1` records. The
checkout currently has no `eval/` corpus schema to reuse, so this schema keeps
the contract's `eljev.decision/1` fields inside an explicit replay task:

```json
{
  "schema": "eljev.oracle/1",
  "task_id": "task-17",
  "decision_id": "optional-decision-id",
  "task": {
    "criterion": "most urgent",
    "candidates": [
      {"id": "c1", "text": "routine item"},
      {"id": "c2", "text": "urgent item"}
    ],
    "top_n": 1,
    "tier": "auto"
  },
  "oracle_verdict": {
    "schema": "eljev.decision/1",
    "choice": "c2",
    "choice_index": 1,
    "status": "selected",
    "exit_code": 0
  },
  "expected_choice": "c2",
  "trigger": "userPromptTransformed"
}
```

The harness also accepts an `eljev.decision/1` record when it carries the
replay `task` (or top-level `criterion` and `candidates`) plus
`oracle_verdict` or `expected_choice`. It does not invent missing candidate
text from a ranked result.

Run it with a persistent adapter:

```powershell
python scripts\oracle_ceiling.py `
  --input decisions.jsonl `
  --runner oracle_runner.py `
  --report oracle-report.json
```

The adapter receives one `eljev.oracle.request/1` JSON object per line for
each arm. It must execute the same task for `baseline`, and inject the supplied
`oracle_verdict` through the real `trigger` for `oracle` without a model call.
It returns one `eljev.oracle.result/1` object per line:

```json
{
  "schema": "eljev.oracle.result/1",
  "outcome_correct": true,
  "task_time_ms": 465.0,
  "turns": 2,
  "input_tokens": 120,
  "output_tokens": 40
}
```

`oracle-report.json` preserves paired per-arm wall time, turns, tokens, and
correctness. The gate requires at least 10 pairs, no oracle correctness
regression, and a material improvement in wall time, turns, or tokens.

## Oracle test override

The S1 oracle harness can exercise the same hook trigger without model
latency by setting both variables for a test process only:

```powershell
$env:ELJEV_ORACLE_MODE = "1"
$env:ELJEV_ORACLE_VERDICT_JSON = '{"schema":"eljev.decision/1","choice":"c2","choice_index":1,"status":"selected","exit_code":0}'
```

Never set these variables in a normal Copilot session. They are ignored unless
`ELJEV_ORACLE_MODE=1` is explicit.
