# Skill troubleshooting

## `daemon_unavailable` / `daemon_starting`

- `daemon_unavailable`: local daemon is unreachable.
- `daemon_starting`: daemon process exists but has not reached ready state.

Run:

```powershell
python -m eljev daemon status
python -m eljev daemon start
```

```bash
python -m eljev daemon status
python -m eljev daemon start
```

## Port collision (second daemon)

Daemon uses exclusive loopback bind; a second start attempt exits with code 3.
Use `daemon status` and `daemon stop` before restarting.

## Orphan process cleanup

`python -m eljev daemon stop` checks `/health` pid and can stop orphan daemons that no longer match pidfile state.

## `engine_error` + `error_kind: auth`

Use `az login`, confirm account context, and verify **Cognitive Services User** role at resource scope.
Role assignments can take about five minutes to propagate.
If resource sets `disableLocalAuth`, key-based auth will not work.

## 401 "invalid subscription key or wrong API endpoint"

Possible causes with bearer auth:

- missing role assignment
- malformed token extraction (for example, accidental tab-separated output from `az ... -o tsv`)

## 404 route mismatch

Only this route is valid:

```text
POST https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank
```

## Hook injected nothing

Check:

- `EL_JEV` effective source in `python -m eljev status`
- `hook_mode`
- min-word threshold
- `.eljev/logs/hook.jsonl`

## 403 or 415 from daemon

Custom callers must use loopback host, JSON content type for POST, and no `Origin` header.

