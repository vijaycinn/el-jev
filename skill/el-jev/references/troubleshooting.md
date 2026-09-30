# Skill troubleshooting

## `daemon_unavailable`

The local daemon could not be reached. Start it before calling the skill:

```powershell
eljev daemon start
```

Check health:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
```

Do not convert this into a model answer. The host should retain control and
repair the daemon or configuration.

## `daemon_starting` or stale pidfile

If health reports `starting`, wait briefly and retry health. If a start command
claims an existing process but health never becomes ready, inspect the pidfile
under `.eljev\` and confirm the process is no longer running before removing a
stale pidfile. Never delete a pidfile for a live process.

## 401 or 403 from Azure

Use Entra bearer authentication. The verified deployment has
`disableLocalAuth: true`, so key authentication will not work. The signed-in
identity needs the **Cognitive Services User** role at the Azure AI Foundry
resource scope. Owner or Contributor alone does not grant inference.

Verify the account and token:

```powershell
az account show
az account get-access-token --scope https://ai.azure.com/.default -o tsv
```

Never print or log the token.

## 404 from rerank

Use the provider-prefixed route:

```text
POST {endpoint}/providers/cohere/v2/rerank
```

Do not add an `api-version` query parameter. The unprefixed
`/v1/rerank`, `/v2/rerank`, and `/models/rerank` routes return 404 on the
verified deployment.

## 429 or long-candidate throttling

The deployment quota is 150 requests per 60 seconds and 150,000 tokens per 60
seconds. Fifty candidates at roughly 2,000 characters consume about 25,000
tokens, so the workload is limited to about six calls per minute. Shorten
candidate text, reduce call volume, or wait for `Retry-After`. Do not add
unbounded retries.

## Malformed response

The client must return `status: invalid_response`, `error_kind: malformed`,
and exit code 2. It must not guess a choice and must not expose a stack trace
or secret.
