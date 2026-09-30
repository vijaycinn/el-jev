# el-jev MCP server

This is a dependency-free Python 3.13 MCP server for GitHub Copilot CLI. It speaks
line-delimited JSON-RPC over stdio and proxies tool calls to the already-running local
daemon at `http://127.0.0.1:8787`.

The server never starts the daemon, loads a model, or calls Cohere. Start the daemon
separately with `eljev daemon start`. If it is absent, the tools return structured
`error_kind: "daemon_unavailable"` content with that instruction.

## Register with GitHub Copilot CLI

Review `eljev-mcp-config.example.json`, then add its `mcpServers.eljev` entry to the
Copilot CLI MCP configuration. The example uses absolute Windows paths and does not
modify any live Copilot configuration.

You can also launch the server directly while testing:

```powershell
python <path-to-el-jev>/mcp/server.py
```

The daemon host and port follow the contract variables `ELJEV_HOST` and `ELJEV_PORT`.
The default is `127.0.0.1:8787`. `ELJEV_TIMEOUT_MS` controls the proxy request timeout.

## Exposed tools

| Tool | Input | Result |
| --- | --- | --- |
| `eljev_screen` | Non-empty criterion and 1–250 `{id,text}` candidates | Ranked `eljev.decision/1` record |
| `eljev_decide` | State plus a `choice` or `noul` question | Daemon decision or explicit backend status |
| `eljev_health` | No arguments | Daemon status, engines, calibration, and coverage policy |

Candidate text is capped at 2,000 characters per item and 100,000 characters total.
Duplicate IDs and empty text are rejected. The limits are declared in the tool schemas
and checked again in the server code; inputs are never truncated.

Every successful decision result exposes `structuredContent` containing the full
`eljev.decision/1` record. The v0 `always_abstain_v0` policy is surfaced explicitly:
non-trivial decisions remain `status: "needs_review"` with `exit_code: 2`, even when a
ranking is returned. The MCP path provides typed, auditable, capped verdicts; it makes no
latency claim.

## Test

From the repository root:

```powershell
python -m unittest discover -s mcp -p "test_*.py"
```
