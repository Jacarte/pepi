// Eval-only observer. Valid JavaScript too for contract tests without Pi.
import { appendFileSync, mkdirSync, copyFileSync, existsSync } from "node:fs";
import { join, basename } from "node:path";
import { randomUUID } from "node:crypto";

export default function (pi) {
  const output = process.env.PEPI_EVAL_OUTPUT;
  if (!output) return;
  const directory = join(output, "telemetry");
  const sessions = join(output, "child-sessions");
  mkdirSync(directory, { recursive: true });
  mkdirSync(sessions, { recursive: true });
  let sequence = 0;
  let calls = 0;
  let errors = 0;
  let toolMs = 0;
  const started = new Map();
  const byTool = {};

  function request(method) {
    return new Promise((resolve) => {
      const requestId = randomUUID();
      let finished = false;
      const finish = (reply) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        if (typeof off === "function") off();
        resolve(reply);
      };
      const off = pi.events.on(`subagents:rpc:v1:reply:${requestId}`, finish);
      const timer = setTimeout(() => finish({ success: false, error: { message: "RPC timeout" } }), 5000);
      try {
        pi.events.emit("subagents:rpc:v1:request", { version: 1, requestId, method, params: {} });
      } catch (error) {
        finish({ success: false, error: { message: String(error) } });
      }
    });
  }

  async function snapshot(phase, ctx) {
    try {
      const sessionId = ctx.sessionManager.getSessionId();
      const cost = await request("cost");
      const status = await request("status");
      const copied = [];
      const copyErrors = [];
      if (cost.success && cost.data?.version === 1) {
        for (const child of cost.data.children ?? []) {
          const source = child.sessionFile;
          if (typeof source !== "string" || !source.endsWith(".jsonl")) continue;
          const name = `${String(child.runId ?? "child").replace(/[^a-zA-Z0-9_.-]/g, "_")}-${basename(source)}`;
          try {
            if (!existsSync(source)) throw new Error("child session file is missing");
            copyFileSync(source, join(sessions, name));
            copied.push(name);
          } catch (error) {
            copyErrors.push(String(error));
          }
        }
      }
      const record = {
        schema_version: 1, session_id: sessionId, sequence: ++sequence,
        timestamp_ms: Date.now(), phase,
        accounting: cost.success && cost.data?.version === 1 ? cost.data : null,
        accounting_error: cost.success ? null : cost.error,
        active_children: status.success ? (status.data?.fleet?.totalActive ?? null) : null,
        status_error: status.success ? null : status.error,
        tools: { calls, errors, elapsed_ms_sum: toolMs, by_tool: byTool, unfinished: started.size },
        copied_child_sessions: copied, child_session_copy_errors: copyErrors,
      };
      const safeId = String(sessionId).replace(/[^a-zA-Z0-9_.-]/g, "_");
      appendFileSync(join(directory, `${safeId}.jsonl`), JSON.stringify(record) + "\n");
    } catch (error) {
      console.error(`pepi-eval telemetry: ${String(error)}`);
    }
  }

  pi.on("tool_call", (event) => {
    calls++;
    byTool[event.toolName] = (byTool[event.toolName] ?? 0) + 1;
    started.set(event.toolCallId, performance.now());
  });
  pi.on("tool_result", (event) => {
    if (event.isError) errors++;
    const begin = started.get(event.toolCallId);
    if (begin !== undefined) toolMs += performance.now() - begin;
    started.delete(event.toolCallId);
  });
  pi.on("agent_settled", (_event, ctx) => snapshot("settled", ctx));
  pi.on("session_shutdown", (_event, ctx) => snapshot("shutdown", ctx));
}
