# el-jev architecture

el-jev is a local decision sidecar. It validates an input, optionally applies
a deterministic pregate, calls one configured engine, creates a versioned
decision record, and returns that record to the caller.

## Components

```text
                    +-------------------------------+
                    | Host authorization/execution  |
                    +---------------+---------------+
                                    |
        +---------------------------+---------------------------+
        |                           |                           |
   hook/in-process                 CLI                    MCP / skill
        |                           |                           |
        +---------------------------v---------------------------+
                    | warm client or local HTTP client |
                    +---------------+-----------------+
                                    |
                         127.0.0.1:8787
                                    |
                    +---------------v-----------------+
                    | resident el-jev daemon          |
                    | validate -> pregate -> tier     |
                    | verdict -> log                 |
                    +--------+------------+----------+
                             |            |
                       Shape A        Shape B
                    /v1/systemone   Cohere rerank
```

### Daemon

The daemon binds to `127.0.0.1` only. It exposes:

- `GET /health`
- `POST /v1/screen` for Shape B
- `POST /v1/decide` for Shape A
- `GET /v1/log?limit=N` for recent diagnostic records

When the daemon itself is healthy, decision endpoints return HTTP 200. The
typed verdict's `exit_code` carries selected, review, or fail-open outcomes.
Input validation still produces the contract's validation result.

### Tiers

1. **Validation** enforces the 1-250 candidate, 2,000-character per-candidate,
   100,000-character total, uniqueness, and non-empty rules.
2. **Pregate** may short-circuit a trivial local decision. A pregate result is
   `status: trivial`, with exit code 2 because there is nothing to auto-act on.
3. **Shape A** passes a typed short decision to a configured local
   `/v1/systemone` service. Without `ELJEV_SYSTEMONE_URL`, v0 returns a
   not-implemented response.
4. **Shape B** sends the criterion and documents to the verified Cohere route:
   `POST {endpoint}/providers/cohere/v2/rerank`.
5. **Verdict** sorts results, preserves original indexes, detects exact ties,
   applies the coverage policy, and emits `eljev.decision/1`.
6. **Log** appends a redacted decision record outside the repository.

No tier may reorder the input before `choice_index` is assigned. A malformed
engine response fails closed as `invalid_response`. A transport, auth,
timeout, or quota failure fails open to the caller as `engine_error`, exit 2.

## Data flow

### Shape B

```text
criterion + candidates
        |
        v
validate caps and redact at entry
        |
        v
pregate or Cohere client
        |
        v
kept-alive HTTPS connection + cached Entra token
        |
        v
ranked indexes and relevance scores
        |
        v
typed verdict + timings + diagnostics + redacted log
```

The client must use the Entra scope `https://ai.azure.com/.default`. It must
not use API keys on a resource with `disableLocalAuth: true`, and it must not
add an `api-version` query parameter to the verified route.

### Shape A

```text
state + typed question
        |
        v
validate short options
        |
        v
configured local /v1/systemone
        |
        v
typed decision record
```

Shape A has no local engine bundled in v0. It is configuration-dependent.

## Which path is fast?

The resident daemon and a hook/in-process caller are the performance design.
A warm daemon round trip measured 1.8 ms, while a cold CLI process was about
700 ms. Connection reuse measured 245 ms mean versus 764 ms with a fresh
connection. A conversational skill invocation added 10.4 seconds median in
one measured agent path, so it must not be presented as the speed path.

The host remains responsible for deciding whether any result can authorize or
execute an action. In v0, every non-trivial result has exit code 2.
