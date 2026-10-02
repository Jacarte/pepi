import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { EventEmitter } from "node:events";

const source = readFileSync(new URL("../eval-telemetry.ts", import.meta.url), "utf8");
const install = (await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`)).default;

test("observer is inert outside evals", () => {
  const previous = process.env.PEPI_EVAL_OUTPUT;
  delete process.env.PEPI_EVAL_OUTPUT;
  try { install({ on() { throw new Error("must not register"); } }); }
  finally { if (previous !== undefined) process.env.PEPI_EVAL_OUTPUT = previous; }
});

test("snapshot RPC uses current contract and captures tool metrics without prompts", async () => {
  const directory = mkdtempSync(join(tmpdir(), "pepi-eval-test-"));
  const previous = process.env.PEPI_EVAL_OUTPUT;
  process.env.PEPI_EVAL_OUTPUT = directory;
  try {
    const events = new EventEmitter();
    const handlers = new Map();
    const pi = {
      on(name, callback) { handlers.set(name, callback); },
      events: {
        on(name, callback) { events.on(name, callback); return () => events.off(name, callback); },
        emit(name, message) { events.emit(name, message); },
      },
    };
    events.on("subagents:rpc:v1:request", (request) => {
      assert.equal(request.version, 1);
      const data = request.method === "cost"
        ? { version: 1, total: { input: 42 }, children: [], unresolvedAsyncChildren: 0 }
        : { fleet: { totalActive: 0 } };
      events.emit(`subagents:rpc:v1:reply:${request.requestId}`, { success: true, data });
    });
    install(pi);
    handlers.get("tool_call")({ toolCallId: "t", toolName: "bash", input: { command: "SECRET" } });
    handlers.get("tool_result")({ toolCallId: "t", isError: true });
    const ctx = { sessionManager: { getSessionId() { return "parent"; } } };
    await handlers.get("agent_settled")({}, ctx);
    await handlers.get("session_shutdown")({}, ctx);
    const text = readFileSync(join(directory, "telemetry/parent.jsonl"), "utf8");
    const records = text.trim().split("\n").map(JSON.parse);
    assert.equal(records.length, 2);
    assert.equal(records[1].phase, "shutdown");
    assert.equal(records[1].accounting.total.input, 42);
    assert.equal(records[1].tools.calls, 1);
    assert.equal(records[1].tools.errors, 1);
    assert.equal(records[1].tools.unfinished, 0);
    assert.equal(records[1].active_children, 0);
    assert.ok(!text.includes("SECRET"));
    assert.equal(events.eventNames().length, 1);
  } finally {
    if (previous === undefined) delete process.env.PEPI_EVAL_OUTPUT;
    else process.env.PEPI_EVAL_OUTPUT = previous;
    rmSync(directory, { recursive: true, force: true });
  }
});
