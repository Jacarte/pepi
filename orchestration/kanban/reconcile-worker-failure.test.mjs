import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/reconcile-worker-failure.ts"), "utf8");
const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const ajv = new Ajv({ allErrors: true, strict: true }); addFormats(ajv); const validate = ajv.compile(schema);

function board(phase = "implementation") {
  return {
    schemaVersion: 1, revision: 9,
    workflow: { goal: "x", tier: "T2", state: "executing", classification: { initialTier: "T2", confirmedTier: "T2", confidence: "high", reason: "x", riskSignals: [] } },
    scheduler: { maxWorkers: 2 },
    tasks: [{
      id: "a", title: "A", description: "A", status: "working", phase, dependsOn: [], paths: ["src/a"], acceptance: ["works"], modifying: true,
      attempts: 1, assignment: { workerKey: "worker-a-1", runId: null, attempt: 1, startedAt: "2026-09-17T12:00:00.000Z" }, blocker: null,
      result: phase === "fix" ? { summary: "Prior failure", verification: "pending", review: "pending", runId: null, outputReference: "/tmp/a.json" } : null,
    }],
    createdAt: "2026-09-17T12:00:00.000Z", updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

async function execute(initial, args = {}) {
  let value = structuredClone(initial);
  const state = { async get() { return structuredClone(value); }, async set(_k, next) { value = structuredClone(next); } };
  const workflow = new AsyncFunction("args", "state", workflowSource);
  const result = await workflow({ expectedRevision: 9, taskId: "a", ...args }, state);
  return { result, value };
}

test("blocks failed implementation worker as infrastructure", async () => {
  const { result, value } = await execute(board(), { reason: "child process exited" });
  assert.equal(result.status, "worker-failure-blocked");
  assert.equal(value.tasks[0].status, "blocked");
  assert.equal(value.tasks[0].phase, "queued");
  assert.equal(value.tasks[0].assignment, null);
  assert.equal(value.tasks[0].blocker.kind, "infrastructure");
  assert.match(value.tasks[0].blocker.reason, /child process exited/);
  assert.equal(validate(value), true, JSON.stringify(validate.errors, null, 2));
});

test("preserves bounded fix history when fix worker fails", async () => {
  const { value } = await execute(board("fix"));
  assert.match(value.tasks[0].result.summary, /Worker infrastructure failure/);
  assert.equal(validate(value), true, JSON.stringify(validate.errors, null, 2));
});

test("rejects completed assignments", async () => {
  const invalid = board();
  invalid.tasks[0].assignment.runId = "run-a";
  await assert.rejects(() => execute(invalid), /uncompleted worker assignment/);
});
