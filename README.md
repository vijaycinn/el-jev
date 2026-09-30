# el-jev — Fast System One Decision Sidecar for Copilot CLI

`el-jev` is a high-speed, typed decision sidecar and pre-turn hook for **GitHub Copilot CLI**. It provides Kahneman "System 1" thinking (calibrated, instinctive decisioning in <200ms) to replace slow, multi-turn LLM reasoning loops.

Built to mimic **TypeSafeAI Jev**, `el-jev` runs on an enterprise-compliant **Azure AI Foundry** Cohere text classification/reranking deployment using keyless **Microsoft Entra ID** authentication (or Managed Identity).

---

## Architecture Schematic

The full interactive architecture diagram generated with Archify is available at [`docs/architecture.html`](docs/architecture.html).

```text
+-----------------------------------------------------------------------------------+
|  Developer Workstation (Windows / macOS / Linux)                                  |
|                                                                                   |
|  [Developer] ---> [Pre-Turn Hook] -------------------> [Copilot LLM]             |
|   (Prompt)        (userPromptTransformed)             (Injected Verdict)          |
|                          |                                   |                    |
|             checks state |                                   |                    |
|                   [EL_JEV Switch]                            |                    |
|                   (Env: ON | OFF)                            v                    |
|                          |                            [Instant Action]            |
|                          v                                                        |
|                 [el-jev Daemon]                                                   |
|                 (127.0.0.1:8787)                                                  |
|                          |                                                        |
|                          v                                                        |
|                 [System One Engine]                                               |
|                 (Choice, Noul, Score)                                             |
+--------------------------|--------------------------------------------------------+
                           |  HTTPS TLS (Entra Bearer Token)
                           v
+-----------------------------------------------------------------------------------+
|  Azure AI Foundry Cloud Environment                                               |
|                                                                                   |
|  [Microsoft Entra ID] -----> [RBAC Role Gate]                                     |
|  (scope: ai.azure.com)       (Cognitive Services User / Managed Identity)         |
|                                      |                                            |
|                                      v                                            |
|                           [AI Services Gateway]                                   |
|                           (services.ai.azure.com)                                 |
|                                      |                                            |
|                                      v                                            |
|                           [Cohere Model Deployment]                               |
|                           (Cohere-rerank-v4.0-pro ~175ms p50)                     |
+-----------------------------------------------------------------------------------+
```

---

## Measured Performance & Efficacy

Measured live against Azure AI Foundry `Cohere-rerank-v4.0-pro` in `southcentralus`:

| Decision Type | Operation | Live p50 Latency | Accuracy / Confidence |
|---|---|---:|---|
| **Shape A (`choice`)** | Multi-class intent & routing triage | **174.7 ms** | **96.8% - 98.6%** |
| **Shape A (`noul`)** | Binary yes/no policy & safety gates | **181.2 ms** | **95.7%** true prob |
| **Shape B (`screen`)** | Open candidate ranking (up to 250) | **183.0 ms** | Ranked relevance |
| **Local Fallback** | Offline token overlap heuristic | **< 2.0 ms** | Zero network deps |
| **Warm Daemon** | Local loopback round trip | **1.8 ms** | Kept-alive connection |

---

## Azure AI Foundry Prerequisites (One-Time Setup)

`el-jev` uses **keyless authentication** with Microsoft Entra ID. No API keys are stored or transmitted.

### 1. Deploy the Cohere Model in Azure AI Foundry
1. Open the [Azure AI Foundry Portal](https://ai.azure.com) (or Azure Portal Cognitive Services).
2. Create or select an **AI Services** or **Foundry Hub** resource (e.g. in `southcentralus` or `eastus2`).
3. Deploy model: **`Cohere-rerank-v4.0-pro`** (Deployment Name: `Cohere-rerank-v4.0-pro`).

### 2. Grant RBAC Role (`Cognitive Services User`)
Your developer user account or Managed Identity must have the **`Cognitive Services User`** role on the resource:

```bash
az role assignment create \
  --role "Cognitive Services User" \
  --assignee "<your-email@domain.com-or-principal-id>" \
  --scope "/subscriptions/<sub-id>/resourceGroups/<rg-name>/providers/Microsoft.CognitiveServices/accounts/<resource-name>"
```

*Note: For Azure VMs or CI/CD agents, enable System-Assigned Managed Identity and assign the role to the VM's principal ID.*

---

## Peer Setup Instructions

### On PC (Windows 11 / Windows 10)

```powershell
# 1. Clone repository
git clone https://github.com/vijaycinn/el-jev.git
Set-Location .\el-jev

# 2. Install editable package (standard library only on runtime path)
python -m pip install -e .

# 3. Configure environment variables (or set as User Account Env Variable)
$env:EL_JEV = "ON"
$env:ELJEV_COHERE_ENDPOINT = "https://<your-resource>.services.ai.azure.com"
$env:ELJEV_COHERE_DEPLOYMENT = "Cohere-rerank-v4.0-pro"

# 4. Sign in to Azure CLI (Entra ID token minting)
az login

# 5. Start resident daemon
python -m eljev daemon start

# Verify status
python -m eljev status
```

### On Mac (macOS) & Linux

```bash
# 1. Clone repository
git clone https://github.com/vijaycinn/el-jev.git
cd el-jev

# 2. Install package
pip install -e .

# 3. Add environment configuration to ~/.zshrc or ~/.bashrc
export EL_JEV="ON"
export ELJEV_COHERE_ENDPOINT="https://<your-resource>.services.ai.azure.com"
export ELJEV_COHERE_DEPLOYMENT="Cohere-rerank-v4.0-pro"

# 4. Sign in to Azure CLI
az login

# 5. Start daemon
python -m eljev daemon start

# Verify status
python -m eljev status
```

---

## Copilot CLI Integration (Pre-Turn Hook)

To enable automatic decisioning on every Copilot CLI turn:

### Option A: Repository Hook (Project Specific)
Add `.github/hooks/eljev.json` in your repository:

```json
{
  "version": 1,
  "hooks": {
    "userPromptTransformed": [
      {
        "type": "command",
        "powershell": "python <path-to-el-jev>/hooks/eljev_pre_turn.py",
        "bash": "python /path/to/el-jev/hooks/eljev_pre_turn.py",
        "timeoutSec": 2
      }
    ]
  }
}
```

### Option B: User-Level Hook (All Repositories)
Place the file at:
- **Windows:** `C:\Users\<user>\.copilot\hooks\eljev.json`
- **Mac/Linux:** `~/.copilot/hooks/eljev.json`

Once configured, Copilot CLI intercepts prompts before the model generation turn and injects calibrated verdicts:
- **Yes/No / Safety Questions** (`should I deploy?`, `is this safe?`) -> Evaluated by **Noul** gate.
- **Action / Task Requests** (`review...`, `fix...`, `find...`) -> Classified by **Choice** intent router.
- **Trivial Greetings** (`hi`, `ok`, `thanks`) -> Fail open in <1 ms with zero overhead.

---

## Controlling el-jev (`EL_JEV` Switch)

You can toggle `el-jev` at any time without restarting terminals:

### 1. User Environment Variable (Recommended)
Set `EL_JEV` to `ON` or `OFF`:
- Windows: `[System.Environment]::SetEnvironmentVariable("EL_JEV", "OFF", "User")`
- Mac/Linux: `export EL_JEV=OFF`

### 2. CLI Toggle Commands
```bash
python -m eljev off    # Disables automatic routing and stops daemon
python -m eljev on     # Re-enables routing and starts daemon
python -m eljev status # Displays current status
```

### 3. Single-Session Override
```powershell
$env:ELJEV_ENABLED = "0"; copilot
```

---

## Using el-jev CLI Directly

You can also run typed decisions directly from terminal or scripts:

### Shape A: Choice Decision
```bash
# question.json:
# {"id": "route", "kind": "choice", "instructions": "Pick action", "criteria": {"merge": "Ready", "reject": "Has bugs"}}

python -m eljev decide --state-text "PR #42 fixes all unit tests" --question question.json
```

### Shape A: Noul Gate (Yes / No)
```bash
# noul.json:
# {"id": "gate", "kind": "noul", "instructions": "Is this safe to deploy to production?"}

python -m eljev decide --state-text "Staging tests passed with 100% code coverage" --question noul.json
```

### Shape B: Candidate Screening (Rerank)
```bash
python -m eljev screen --criterion "which bug is most urgent to fix" --candidates bugs.json --top-n 3
```

---

## Running Tests

Run the full unit and live test suite:

```bash
python -m unittest discover -s tests
```

To run the live Azure AI Foundry test:
```bash
python tests/test_live_foundry.py
```

---

## License

MIT
