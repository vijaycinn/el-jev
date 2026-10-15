import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_PATH = ROOT / ".github" / "extensions" / "el-mds" / "extension.mjs"


def _node_program(test_body: str) -> str:
    extension_path = json.dumps(str(EXTENSION_PATH))
    return f"""
import {{ readFile }} from "node:fs/promises";
import vm from "node:vm";
import {{ pathToFileURL }} from "node:url";

const extensionPath = {extension_path};
const source = await readFile(extensionPath, "utf8");
const extensionModule = new vm.SourceTextModule(source, {{
  identifier: pathToFileURL(extensionPath).href,
}});
const sdkModule = new vm.SourceTextModule("export const joinSession = async () => {{}};", {{
  identifier: "stub:copilot-sdk",
}});
await sdkModule.link(() => {{}});
await sdkModule.evaluate();
await extensionModule.link(async (specifier) => {{
  if (specifier === "@github/copilot-sdk/extension") {{
    return sdkModule;
  }}
  throw new Error(`unexpected import: ${{specifier}}`);
}});
await extensionModule.evaluate();
const ext = extensionModule.namespace;
const assert = (condition, message) => {{
  if (!condition) {{
    throw new Error(message);
  }}
}};

{test_body}
"""


class ExtensionElMdsTests(unittest.TestCase):
    def _run_node(self, body: str, *, extra_env: dict[str, str] | None = None) -> None:
        env = dict(os.environ)
        env["ELJEV_EXTENSION_TEST"] = "1"
        if extra_env:
            env.update(extra_env)
        completed = subprocess.run(
            ["node", "--experimental-vm-modules", "--input-type=module", "-e", _node_program(body)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(
                "node extension test failed\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )

    def test_on_user_prompt_submitted_calls_gate_for_eligible_prompt_and_fails_open(self):
        self._run_node(
            """
const calls = [];
const handlers = ext.createExtensionHandlers({
  port: 8787,
  hookTimeoutMs: 25,
  fetchImpl: async (url, init) => {
    calls.push({ url, body: init.body });
    return {
      ok: true,
      json: async () => ({
        schema: "eljev.gate/1",
        status: "needs_review",
        exit_code: 2,
        tier: "bounded_work",
        enforceable: false,
        budget_text: "[budget]",
      }),
    };
  },
});

const skipped = await handlers.hooks.onUserPromptSubmitted(
  { prompt: "too short" },
  { sessionId: "s-short" },
);
assert(skipped === undefined, "short prompts must be ignored");
assert(calls.length === 0, "short prompt must not call /v1/gate");

const eligible = await handlers.hooks.onUserPromptSubmitted(
  { prompt: "please review this patch now" },
  { sessionId: "s-eligible" },
);
assert(calls.length === 1, "eligible prompt must call /v1/gate exactly once");
assert(calls[0].url.endsWith("/v1/gate"), "gate endpoint path mismatch");
assert(eligible && eligible.additionalContext === "[budget]", "budget_text was not returned");
const requestPayload = JSON.parse(calls[0].body);
assert(requestPayload.state === "please review this patch now", "state text mismatch");
assert(requestPayload.session_id === "s-eligible", "session id mismatch");

const failingHandlers = ext.createExtensionHandlers({
  fetchImpl: async () => {
    throw new Error("timeout");
  },
});
const failOpen = await failingHandlers.hooks.onUserPromptSubmitted(
  { prompt: "please review this patch now" },
  { sessionId: "s-fail-open" },
);
assert(failOpen === undefined, "timeout/error must fail open");
"""
        )

    def test_on_pre_tool_use_denies_only_enforceable_fast_direct_for_task_tools(self):
        self._run_node(
            """
const handlers = ext.createExtensionHandlers({ fetchImpl: async () => null });

handlers.gateBySession.set("advisory", {
  enforceable: false,
  tier: "fast_direct",
  status: "selected",
  exit_code: 0,
});
const advisoryDecision = await handlers.hooks.onPreToolUse(
  { toolName: "task" },
  { sessionId: "advisory" },
);
assert(advisoryDecision === undefined, "advisory mode must not deny tool usage");

handlers.gateBySession.set("enforceable", {
  enforceable: true,
  tier: "fast_direct",
  status: "selected",
  exit_code: 0,
});
const denyTask = await handlers.hooks.onPreToolUse(
  { toolName: "task" },
  { sessionId: "enforceable" },
);
assert(denyTask && denyTask.permissionDecision === "deny", "task must be denied when enforceable");

const denyWorkflow = await handlers.hooks.onPreToolUse(
  { toolName: "run_dynamic_workflow" },
  { sessionId: "enforceable" },
);
assert(
  denyWorkflow && denyWorkflow.permissionDecision === "deny",
  "run_dynamic_workflow must be denied when enforceable",
);

const allowView = await handlers.hooks.onPreToolUse(
  { toolName: "view" },
  { sessionId: "enforceable" },
);
assert(allowView === undefined, "view should remain allowed");

const allowGrep = await handlers.hooks.onPreToolUse(
  { toolName: "grep" },
  { sessionId: "enforceable" },
);
assert(allowGrep === undefined, "grep should remain allowed");

const allowEdit = await handlers.hooks.onPreToolUse(
  { toolName: "edit" },
  { sessionId: "enforceable" },
);
assert(allowEdit === undefined, "edit should remain allowed");
"""
        )

    def test_el_mds_decide_handler_returns_contract_json_string(self):
        self._run_node(
            """
const decideCalls = [];
const handlers = ext.createExtensionHandlers({
  fetchImpl: async (_url, init) => {
    decideCalls.push(JSON.parse(init.body));
    return {
      ok: true,
      json: async () => ({
        schema: "eljev.decision/1",
        shape: "decide",
        status: "needs_review",
        exit_code: 2,
        choice: "alpha",
      }),
    };
  },
});

const decideTool = handlers.tools.find((tool) => tool.name === "el_mds_decide");
assert(decideTool, "el_mds_decide tool not registered");
const result = await decideTool.handler({
  state: "state text",
  question: { id: "q1", kind: "choice", instructions: "route", criteria: { alpha: "a", beta: "b" } },
});
assert(typeof result === "string", "tool handler must return a JSON string");
assert(decideCalls.length === 1, "decide tool must call daemon once");
assert(decideCalls[0].engine === "md1", "decide payload must pin engine=md1");
const parsed = JSON.parse(result);
assert(parsed.schema === "eljev.decision/1", "decision schema mismatch");

const fallbackHandlers = ext.createExtensionHandlers({
  fetchImpl: async () => {
    throw new Error("daemon down");
  },
});
const fallbackTool = fallbackHandlers.tools.find((tool) => tool.name === "el_mds_decide");
const fallback = await fallbackTool.handler({
  state: "state text",
  question: { id: "q2", kind: "choice", instructions: "route", criteria: { alpha: "a", beta: "b" } },
});
assert(typeof fallback === "string", "fallback handler must return a JSON string");
const fallbackParsed = JSON.parse(fallback);
assert(fallbackParsed.schema === "eljev.decision/1", "fallback decision schema mismatch");
"""
        )

    def test_short_prompt_clears_stale_gate_and_allows_task_on_second_turn(self):
        self._run_node(
            """
const handlers = ext.createExtensionHandlers({
  fetchImpl: async (_url, _init) => ({
    ok: true,
    json: async () => ({
      schema: "eljev.gate/1",
      status: "selected",
      exit_code: 0,
      tier: "fast_direct",
      enforceable: true,
      budget_text: "[budget]",
    }),
  }),
});

const firstTurn = await handlers.hooks.onUserPromptSubmitted(
  { prompt: "please implement this parser quickly" },
  { sessionId: "session-clear" },
);
assert(firstTurn && firstTurn.additionalContext === "[budget]", "first turn must carry budget text");
const denied = await handlers.hooks.onPreToolUse(
  { toolName: "task" },
  { sessionId: "session-clear" },
);
assert(denied && denied.permissionDecision === "deny", "enforceable fast_direct should deny task");

const secondTurn = await handlers.hooks.onUserPromptSubmitted(
  { prompt: "tiny ask" },
  { sessionId: "session-clear" },
);
assert(secondTurn === undefined, "short prompt should be ignored");
assert(!handlers.gateBySession.has("session-clear"), "short prompt must clear stale gate");
const allowed = await handlers.hooks.onPreToolUse(
  { toolName: "task" },
  { sessionId: "session-clear" },
);
assert(allowed === undefined, "task must be allowed after stale gate is cleared");

handlers.gateBySession.set("session-clear", {
  enforceable: true,
  tier: "fast_direct",
  status: "selected",
  exit_code: 0,
});
await handlers.hooks.onSessionEnd({}, { sessionId: "session-clear" });
assert(!handlers.gateBySession.has("session-clear"), "onSessionEnd must clear gate state");
"""
        )


if __name__ == "__main__":
    unittest.main()
