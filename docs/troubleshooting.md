# Troubleshooting

## Daemon will not start

Start it from the repository environment:

```powershell
eljev daemon start
```

Then check:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health | ConvertTo-Json
```

Expected health includes `status: "ok"` and `schema: "eljev.decision/1"`.
Confirm that `ELJEV_HOST` is `127.0.0.1` and that `ELJEV_PORT` is the port
you are checking. Do not bind the daemon to `0.0.0.0`.

If health reports `starting`, wait briefly and retry. If the start command
reports an existing process but health never becomes ready, inspect the
pidfile under `.eljev\`. Remove it only after confirming that its process no
longer exists. A live process and its pidfile must not be disrupted.

## `daemon_unavailable`

This is a local transport error: the caller could not reach the daemon. It is
not a model verdict and does not mean that a candidate was rejected. Start or
repair the daemon, then retry. Keep host authorization and execution paused.

## 401 or 403 from Azure

The verified resource uses Entra bearer authentication. It has
`disableLocalAuth: true`, so API-key auth fails with
`AuthenticationTypeDisabled`.

Check the signed-in tenant and token:

```powershell
az account show
az account get-access-token --scope https://ai.azure.com/.default -o tsv
```

The identity needs **Cognitive Services User** at the **Azure AI Foundry
resource** scope. Owner or Contributor alone does not grant inference.
Never log or paste the token.

## 404 from the rerank endpoint

Use exactly:

```text
POST {endpoint}/providers/cohere/v2/rerank
```

There is no `api-version` query parameter. These unprefixed paths return 404
on the verified deployment:

- `/v1/rerank`
- `/v2/rerank`
- `/models/rerank`

Also confirm that `ELJEV_COHERE_ENDPOINT` contains only the host, for example
`https://<resource>.services.ai.azure.com`.

## 429 quota response

The verified deployment has a quota of 150 requests per 60 seconds and
150,000 tokens per 60 seconds. A 50-candidate request with approximately
2,000-character candidates is about 25,000 tokens, or roughly six calls per
minute before token throttling.

Honor `Retry-After`. Retry only 408, 409, 429, and 5xx responses within the
single deadline. Do not retry connection resets, TLS errors, or parse
failures; a resend can double-bill a paid request.

## Stale pidfile

1. Read the pidfile path under `.eljev\`.
2. Check whether that exact process ID is still running.
3. If it is not running, remove only that stale pidfile.
4. Start the daemon and verify `/health`.

Do not kill processes by name and do not remove a pidfile for an active
daemon.

## Malformed provider output

The client must return:

```text
status: invalid_response
error_kind: malformed
exit_code: 2
```

It must fail closed, avoid selecting a candidate, and avoid printing provider
payloads that may contain secrets.
