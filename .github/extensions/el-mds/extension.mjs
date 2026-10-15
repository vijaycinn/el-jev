import { joinSession } from "@github/copilot-sdk/extension";

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = Number(process.env.ELJEV_PORT || "8787");
const DEFAULT_HOOK_TIMEOUT_MS = Number(process.env.ELJEV_HOOK_TIMEOUT_MS || "750");
const DEFAULT_DECIDE_TIMEOUT_MS = Number(process.env.ELJEV_TIMEOUT_MS || "2500");
const MIN_WORDS = Number(process.env.ELJEV_HOOK_MIN_WORDS || "4");
const DENIED_FAST_DIRECT_TOOLS = new Set(["task", "run_dynamic_workflow"]);

function normalizeTimeoutMs(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function wordCount(value) {
  if (typeof value !== "string") {
    return 0;
  }
  return value
    .trim()
    .split(/\s+/)
    .filter(Boolean).length;
}

function createAbortSignal(timeoutMs) {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return { signal: AbortSignal.timeout(timeoutMs), cleanup: () => {} };
  }
  if (typeof AbortController === "undefined") {
    return { signal: undefined, cleanup: () => {} };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return {
    signal: controller.signal,
    cleanup: () => clearTimeout(timer),
  };
}

async function postJson(path, payload, { port, timeoutMs, fetchImpl }) {
  if (typeof fetchImpl !== "function") {
    return null;
  }
  const timeout = normalizeTimeoutMs(timeoutMs, DEFAULT_HOOK_TIMEOUT_MS);
  const url = `http://${DEFAULT_HOST}:${port}${path}`;
  const { signal, cleanup } = createAbortSignal(timeout);
  try {
    const response = await fetchImpl(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal,
    });
    if (!response || !response.ok) {
      return null;
    }
    const decoded = await response.json();
    return decoded && typeof decoded === "object" ? decoded : null;
  } catch {
    return null;
  } finally {
    cleanup();
  }
}

function toNonEmptyString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function safeQuestionShape(question) {
  if (!question || typeof question !== "object") {
    return { questionId: "q1", kind: "choice", criterion: "" };
  }
  const questionId = toNonEmptyString(question.id) || "q1";
  const kind = toNonEmptyString(question.kind || question.type) || "choice";
  const criterion = toNonEmptyString(question.instructions || question.description) || "";
  return { questionId, kind, criterion };
}

function errorDecisionFromQuestion(question) {
  const { questionId, kind, criterion } = safeQuestionShape(question);
  const now = new Date().toISOString();
  return {
    schema: "eljev.decision/1",
    decision_id:
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `el-mds-${Date.now()}`,
    ts: now,
    shape: "decide",
    criterion,
    n_candidates: 0,
    tier_path: [],
    engine: null,
    engine_version: "1",
    choice: null,
    choice_index: null,
    status: "engine_error",
    exit_code: 2,
    raw_top_score: null,
    raw_runner_up: null,
    margin_raw: null,
    calibrated_probability: null,
    margin_calibrated: null,
    calibration_version: "none",
    coverage_policy: "always_abstain_v0",
    results: [],
    elapsed_ms: { total: 0.0, pregate: 0.0, engine: 0.0, overhead: 0.0 },
    escalation_reason: null,
    error_kind: "daemon_unavailable",
    notes: ["Run `eljev daemon start` and retry."],
    question_id: questionId,
    kind,
    confidence: null,
    probabilities: null,
    margin: null,
    noul: null,
    score: null,
  };
}

export async function gateViaDaemon(
  state,
  sessionId,
  { port = DEFAULT_PORT, hookTimeoutMs = DEFAULT_HOOK_TIMEOUT_MS, fetchImpl = globalThis.fetch } = {},
) {
  const normalizedState = toNonEmptyString(state);
  if (!normalizedState) {
    return null;
  }
  const payload = { state: normalizedState };
  const normalizedSessionId = toNonEmptyString(sessionId);
  if (normalizedSessionId) {
    payload.session_id = normalizedSessionId;
  }
  return postJson("/v1/gate", payload, {
    port,
    timeoutMs: hookTimeoutMs,
    fetchImpl,
  });
}

export async function decideViaDaemon(
  state,
  question,
  {
    port = DEFAULT_PORT,
    decideTimeoutMs = DEFAULT_DECIDE_TIMEOUT_MS,
    fetchImpl = globalThis.fetch,
    engine = "md1",
  } = {},
) {
  if (
    !(
      (typeof state === "string" && state.trim()) ||
      (state && typeof state === "object" && !Array.isArray(state)) ||
      Array.isArray(state)
    )
  ) {
    return null;
  }
  if (!question || typeof question !== "object") {
    return null;
  }
  return postJson(
    "/v1/decide",
    {
      state,
      question,
      engine,
    },
    {
      port,
      timeoutMs: decideTimeoutMs,
      fetchImpl,
    },
  );
}

export function shouldDenyTool(gate, toolName) {
  if (!gate || typeof gate !== "object") {
    return false;
  }
  if (gate.enforceable !== true) {
    return false;
  }
  if (gate.tier !== "fast_direct") {
    return false;
  }
  if (gate.status !== "selected" || Number(gate.exit_code) !== 0) {
    return false;
  }
  return DENIED_FAST_DIRECT_TOOLS.has(String(toolName || ""));
}

export function createExtensionHandlers({
  port = DEFAULT_PORT,
  hookTimeoutMs = DEFAULT_HOOK_TIMEOUT_MS,
  decideTimeoutMs = DEFAULT_DECIDE_TIMEOUT_MS,
  fetchImpl = globalThis.fetch,
} = {}) {
  const gateBySession = new Map();
  const resolvedPort = normalizeTimeoutMs(port, DEFAULT_PORT);
  const resolvedHookTimeoutMs = normalizeTimeoutMs(hookTimeoutMs, DEFAULT_HOOK_TIMEOUT_MS);
  const resolvedDecideTimeoutMs = normalizeTimeoutMs(decideTimeoutMs, DEFAULT_DECIDE_TIMEOUT_MS);
  const resolvedFetch = fetchImpl;

  const gateCall = (state, sessionId) =>
    gateViaDaemon(state, sessionId, {
      port: resolvedPort,
      hookTimeoutMs: resolvedHookTimeoutMs,
      fetchImpl: resolvedFetch,
    });

  const decideCall = (state, question) =>
    decideViaDaemon(state, question, {
      port: resolvedPort,
      decideTimeoutMs: resolvedDecideTimeoutMs,
      fetchImpl: resolvedFetch,
    });

  const tools = [
    {
      name: "el_mds_decide",
      description: "Run a fast System-1 decision via the local el-jev daemon.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["state", "question"],
        properties: {
          state: {
            oneOf: [{ type: "string" }, { type: "object" }, { type: "array" }],
            description: "State text or JSON context to evaluate.",
          },
          question: {
            type: "object",
            description: "Typed question object (id, kind, and criteria/options).",
          },
        },
      },
      handler: async (args) => {
        const state = args && typeof args === "object" ? args.state : undefined;
        const question = args && typeof args === "object" ? args.question : undefined;
        const decision = await decideCall(state, question);
        return JSON.stringify(decision || errorDecisionFromQuestion(question));
      },
    },
  ];

  const hooks = {
    onUserPromptSubmitted: async (input, invocation) => {
      const sessionId = toNonEmptyString(invocation?.sessionId);
      if (sessionId) {
        gateBySession.delete(sessionId);
      }
      const prompt = toNonEmptyString(input?.prompt);
      if (!prompt || wordCount(prompt) < Math.max(1, MIN_WORDS)) {
        return undefined;
      }
      const gate = await gateCall(prompt, sessionId);
      if (sessionId && gate) {
        gateBySession.set(sessionId, gate);
      }
      const budgetText = gate && typeof gate === "object" ? toNonEmptyString(gate.budget_text) : null;
      if (!budgetText) {
        return undefined;
      }
      return { additionalContext: budgetText };
    },
    onPreToolUse: async (input, invocation) => {
      const sessionId = toNonEmptyString(invocation?.sessionId);
      if (!sessionId) {
        return undefined;
      }
      const gate = gateBySession.get(sessionId);
      if (!shouldDenyTool(gate, input?.toolName)) {
        return undefined;
      }
      return {
        permissionDecision: "deny",
        permissionDecisionReason:
          "el-mds fast_direct gate selected: use direct local tools instead of sub-agent spawning.",
      };
    },
    onSessionEnd: async (_input, invocation) => {
      const sessionId = toNonEmptyString(invocation?.sessionId);
      if (sessionId) {
        gateBySession.delete(sessionId);
      }
      return undefined;
    },
  };

  return {
    tools,
    hooks,
    gateBySession,
    gateViaDaemon: gateCall,
    decideViaDaemon: decideCall,
  };
}

if (
  process.env.ELJEV_EXTENSION_NO_AUTOJOIN !== "1" &&
  process.env.ELJEV_EXTENSION_TEST !== "1"
) {
  const handlers = createExtensionHandlers();
  await joinSession({
    tools: handlers.tools,
    hooks: handlers.hooks,
  });
}
