# Troubleshooting

## `daemon_unavailable` or `daemon_starting`

`daemon_unavailable` means the caller could not reach the local daemon.
`daemon_starting` means daemon process exists but is not yet ready.

```powershell
python -m eljev daemon status
python -m eljev daemon start
```

```bash
python -m eljev daemon status
python -m eljev daemon start
```

## Port already in use

Daemon uses exclusive loopback bind. A second daemon exits with code 3 if the port is occupied.

```powershell
python -m eljev daemon status
python -m eljev daemon stop
```

```bash
python -m eljev daemon status
python -m eljev daemon stop
```

## Orphan daemon process

`eljev daemon stop` also checks `/health` pid and can stop orphaned daemons even when pidfile is stale.
If stop succeeds but status still shows running, retry `daemon stop` once and re-check.

## `engine_error` with `error_kind: auth`

Most common causes:

1. Missing `az login`.
2. Missing **Cognitive Services User** role on resource.
3. Role assignment not propagated yet (~5 minutes).

```powershell
az login
az account show
```

```bash
az login
az account show
```

If resource has `disableLocalAuth` enabled, API keys are never accepted.

## 401 with "invalid subscription key or wrong API endpoint"

This can appear even with bearer-token usage when:

- Role assignment is missing.
- Token is malformed (for example, tab-separated output copied from `az ... -o tsv` with extra fields).

Validate token retrieval and endpoint format carefully.

## 400 "Token tenant ... does not match resource tenant"

The `az` default account belongs to a different tenant than the Foundry resource (common when you are signed in to several accounts). Pin the resource's subscription so el-jev mints tokens with the right account, then restart the daemon:

```text
python -m eljev configure --subscription <subscription-id>
python -m eljev daemon stop
python -m eljev daemon start
```

`--subscription` selects both the signed-in account and its tenant. Passing only a tenant is not enough, because `az` would still use the default user.

## 404 from Cohere route

Use:

```text
POST https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank
```

Do not use `/v1/rerank`, `/v2/rerank`, or `/models/rerank`.

## Hook adds nothing

Check:

1. `EL_JEV` effective value and source (`python -m eljev status`).
2. Hook mode (`intent` vs `marker`).
3. Prompt length and `ELJEV_HOOK_MIN_WORDS`.
4. Hook logs at `.eljev/logs/hook.jsonl`.

## Daemon returns 403 or 415

Custom clients must:

- send `Host: 127.0.0.1[:port]` or `localhost[:port]`
- send `Content-Type: application/json` on POST
- send no `Origin` header

Otherwise daemon rejects the request by design.

