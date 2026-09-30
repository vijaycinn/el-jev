# el-jev MCP server

`mcp/server.py` exposes `el-jev` decisions as MCP tools over stdio JSON-RPC.
It proxies to the local daemon (`127.0.0.1:8787`) and returns typed `eljev.decision/1` records.

## Configure Copilot CLI

Use [`mcp/eljev-mcp-config.example.json`](eljev-mcp-config.example.json) as the template.
It runs:

- command: `python`
- args: `["<path-to-el-jev>/mcp/server.py"]`

Start daemon separately before use:

```powershell
python -m eljev daemon start
```

```bash
python -m eljev daemon start
```

## Exposed tools

| Tool | Purpose |
|---|---|
| `eljev_health` | Daemon status and engine readiness |
| `eljev_screen` | Shape B rerank against criterion |
| `eljev_decide` | Shape A typed decision (`choice`, `noul`, `score`) |

## Behavior notes

- Input caps are validated in daemon and MCP layers.
- Default policy is advisory (`always_abstain_v0`), so non-trivial outputs usually return `exit_code: 2`.
- `selected` (`exit_code: 0`) requires calibrated mode plus valid calibration for that kind.
- `daemon_unavailable` indicates transport failure to the local daemon, not a model verdict.

## Test

```powershell
python -m unittest discover -s mcp -p "test_*.py"
```

```bash
python -m unittest discover -s mcp -p "test_*.py"
```

