# el-jev Architecture & Decisioning Inner Wiring

`el-jev` is a high-speed, typed decision sidecar and pre-turn hook for **GitHub Copilot CLI**. It provides Kahneman "System 1" thinking (calibrated, instinctive decisioning in <200ms) to eliminate slow, multi-turn LLM reasoning loops.

---

## Interactive Archify Schematics

`el-jev` provides two standalone, interactive architecture schematics generated using the **Archify** engine:

1. **System Topology & Cloud Integration:** [`docs/architecture.html`](architecture.html)  
   *Illustrates the pre-turn hook, resident loopback daemon, Entra ID token provider, and Azure AI Foundry integration.*
2. **Decisioning & Dual-Gate Inner Wiring:** [`docs/decisioning-flow.html`](decisioning-flow.html)  
   *Visualizes the step-by-step path: Statement Intake → Shape Selector → Cohere Inference → Softmax → Dual Gate → Action Outcome.*

---

## 1. High-Level Architecture Topology

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

## 2. Statement-to-Decision Mapping

A statement is **not** evaluated across all shapes at once. Instead, `el-jev` maps the statement to the specific typed decision primitive required by the problem.

### How One Statement Maps Across Primitives

**Input Statement:**
> *"The production payment database is throwing 500 connection pool timeouts."*

| Decision Shape | Question Asked Over Statement | Candidate Input Format | Model Output & Verdict |
|---|---|---|---|
| **Noul** (Binary Gate) | *"Is this a critical production incident?"* | `True` vs `False` | $P(\text{True}) = 0.96$<br>**Decision: `true`** |
| **Choice** (Route / Triage) | *"Which remediation action should execute?"* | `{failover: "DB failover", restart: "App restart", log: "Backlog ticket"}` | `{failover: 0.94, restart: 0.05, log: 0.01}`<br>**Decision: `failover`** |
| **Score** (Ordinal Rating) | *"Rate the severity level"* | `["Low", "Medium", "High", "Critical Outage"]` | Expected Score: **`3.92`**<br>**Level: `Critical Outage`** |
| **Screening** (Open Rerank) | *"Which runbook resolves this error?"* | Array of 1–250 runbook documents | **Ranked list desc:**<br>1. `runbook-db-failover.md` (0.95)<br>2. `runbook-restart.md` (0.42) |

### Automatic Routing in the Copilot CLI Pre-Turn Hook

When a prompt enters `hooks/eljev_pre_turn.py`:
1. **Verification / Safety Questions** (`"Should I run migrations on prod?"` / `"Is this PR safe to commit?"`):
   - Mapped automatically to **Noul** to evaluate the binary affirmation/safety gate.
2. **Action / Task Requests** (`"Review this security code"` / `"Deploy staging build"` / `"Find user service"`):
   - Mapped automatically to **Choice** to classify intent into: `code_modification`, `review_audit`, `investigation_search`, `execution_testing`, or `advisory_explanation`.
3. **Explicit Candidate Sets**:
   - Mapped to **Screening** (Shape B) to rank candidate documents against a criterion.
4. **Trivial Greetings** (`"hi"`, `"ok"`, `"thanks"`):
   - Short-circuited in <1 ms without calling any model.

---

## 3. The Inner Wiring of Decisioning: Good Enough vs. Proceed to LLM

To determine whether a decision is **"Good Enough"** to act on immediately or must **escalate to full LLM generative reasoning**, `el-jev` enforces a statistical **Dual Gate**.

```text
Prompt -> Query & Candidates -> Cohere Rerank -> Softmax (T=0.05) -> Dual Gate -> Exit 0 (Act) | Exit 2 (Proceed to LLM)
```

### 1. Temperature-Scaled Softmax
Azure AI Foundry Cohere Rerank produces raw relevance scores $s_i \in [0, 1]$. Because cross-encoder scores can bunch together on realistic candidate sets, `el-jev` applies temperature-scaled softmax:

$$z_i = \frac{s_i}{T} \quad (T = 0.05)$$

$$P_i = \frac{e^{z_i - \max(z)}}{\sum_j e^{z_j - \max(z)}}$$

This maps raw scores into true probabilities $\sum P_i = 1.0$ that clearly distinguish confident winners from ambiguous clusters.

### 2. The Dual Gate Condition
Given top probability $P_1$ and runner-up probability $P_2$:

$$\text{Dual Gate Passes} \iff P_1 \ge \tau \quad \text{AND} \quad (P_1 - P_2) \ge \Delta$$

- **Probability Floor ($\tau \ge 0.70$):** Guarantees the top option has high statistical weight.
- **Margin Clearance ($\Delta \ge 0.15$):** Guarantees the winner decisively beats the second-best candidate.

### 3. Concrete Decision Evaluation

| Top Scores ($P_1, P_2, P_3$) | Margin ($P_1 - P_2$) | Dual Gate | Exit Code & Status | Copilot LLM Action |
|---|---|---|---|---|
| **`0.92, 0.06, 0.02`** | **`+0.86`** | **PASS** | **`Exit 0 (selected)`** | **Act immediately.** Bypass slow LLM multi-turn thinking. |
| **`0.42, 0.38, 0.20`** | **`+0.04`** | **FAIL** | **`Exit 2 (needs_review)`** | **Proceed to LLM.** Ambiguous; LLM must reason through context. |
| **`0.50, 0.50, 0.00`** | **`0.00`** | **FAIL** | **`Exit 2 (abstain_tie)`** | **Proceed to LLM.** Exact tie; auto-abstain. |
| **Timeout / Error** | `n/a` | **FAIL** | **`Exit 2 (engine_error)`** | **Fail-Open.** LLM executes normally with zero interruption. |

---

## 4. Keyless Authentication & Managed Identity

`el-jev` never stores or transmits API keys. It uses Microsoft Entra ID OAuth 2.0 bearer tokens:

- **Token Scope:** `https://ai.azure.com/.default`
- **RBAC Role:** `Cognitive Services User` at the AI Services resource scope.
- **Token Minting:** In-process cached token provider (`az account get-access-token` on dev workstations or Managed Identity on Azure VMs).
- **Network Security:** Direct HTTPS TLS connection with keep-alive connection pooling (~175 ms warm round trip).
