# el-jev Copilot pre-turn hook

`hooks/eljev_pre_turn.py` is the Copilot `userPromptTransformed` hook path for `el-jev`.
It is fail-open by design: when disabled or unavailable, it returns `{}` quickly and does not block the user turn.

## Install the hook

Use the CLI installer instead of hand-authoring hook JSON.

```powershell
python -m eljev install-hook --scope user
# or:
python -m eljev install-hook --scope repo --repo <path-to-repo>
```

```bash
python -m eljev install-hook --scope user
# or:
python -m eljev install-hook --scope repo --repo <path-to-repo>
```

`--scope user` writes to `<COPILOT_HOME or ~/.copilot>/hooks/eljev.json`.
`--scope repo` writes to `<repo>/.github/hooks/eljev.json`.
Use `--force` to overwrite.

The generated shape is (the installer substitutes the absolute path of the current interpreter and of this checkout's hook):

```json
{
  "version": 1,
  "hooks": {
    "userPromptTransformed": [
      {
        "type": "command",
        "bash": "\"<python>\" \"<path-to-el-jev>/hooks/eljev_pre_turn.py\"",
        "powershell": "& \"<python>\" \"<path-to-el-jev>/hooks/eljev_pre_turn.py\"",
        "timeoutSec": 2,
        "comment": "el-jev pre-turn decision hook (installed by eljev install-hook)"
      }
    ]
  }
}
```

A repo-scoped hook file contains absolute local paths, so keep `.github/hooks/eljev.json` out of source control (this repository's `.gitignore` already does).

## Hook modes

`ELJEV_HOOK_MODE` / config `hook_mode`:

- `intent` (default): classify eligible prompts into a `choice` over five intents.
- `marker`: only process explicit marker payloads (`<!--eljev.request:{...}-->`) or generic `eljev.hook/1`.

Related controls:

| Variable | Default | Purpose |
|---|---:|---|
| `EL_JEV` | `ON` | Global on/off switch |
| `ELJEV_HOOK_MODE` | `intent` | Hook routing mode |
| `ELJEV_HOOK_MIN_WORDS` | 4 | Minimum words for intent routing |
| `ELJEV_HOOK_TIMEOUT_MS` | 500 | Hook-side request budget |
| `ELJEV_DIR` | `<repo>/.eljev` | State directory (`spawn.stamp`, logs) |

## Fail-open and spawn behavior

- If disabled, returns `{}` with no network call.
- If daemon is unavailable, hook triggers one debounced spawn attempt (15s window) and returns `{}` immediately.
- No sleeping occurs on the prompt path.
- Only `selected`, `needs_review`, and `abstain_tie` outcomes inject advisory text.
- Engine errors inject nothing.

Example injected block:

```text
[el-jev advisory]
kind: choice | decision: review_audit | confidence: 0.70 | margin: 0.58
gate: needs_review (exit 2) -> treat as a hint; reason normally
probabilities: review_audit=0.698, investigation_search=0.117, code_modification=0.086
[/el-jev advisory]
```

## Logging

- Hook log path: `<ELJEV_DIR>/logs/hook.jsonl`
- Prompt text is not logged.
- Candidate text in decision logs is redacted by default (`ELJEV_REDACT=1`).

## Oracle mode (testing only)

The hook can run in deterministic oracle mode for experiments:

```powershell
$env:ELJEV_ORACLE_MODE = "1"
$env:ELJEV_ORACLE_VERDICT_JSON = '{"schema":"eljev.decision/1","status":"selected","exit_code":0}'
```

```bash
export ELJEV_ORACLE_MODE=1
export ELJEV_ORACLE_VERDICT_JSON='{"schema":"eljev.decision/1","status":"selected","exit_code":0}'
```

Do not enable oracle mode in normal user sessions.

