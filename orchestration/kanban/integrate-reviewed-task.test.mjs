import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/integrate-reviewed-task.ts"), "utf8");
const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const ajv = new Ajv({ allErrors: true, strict: true }); addFormats(ajv); const validate = ajv.compile(schema);

function integrationTask(overrides = {}) {
  return {
    id: "api", title: "API", description: "Implement API.", status: "working", phase: "integration",
    dependsOn: [], paths: ["internal/api"], acceptance: ["works"], modifying: true, attempts: 1,
    assignment: { workerKey: "worker-api-1", runId: "run-api-1", attempt: 1, startedAt: "2026-09-17T12:00:00.000Z" },
    blocker: null,
    result: { summary: "Reviewed.", verification: "pass", review: "pass", runId: "run-api-1", outputReference: "/tmp/api.handoff.json" },
    ...overrides,
  };
}
function board(tasks = [integrationTask()]) {
  return {
    schemaVersion: 1, revision: 20,
    workflow: { goal: "x", tier: "T2", state: "executing", classification: { initialTier: "T2", confirmedTier: "T2", confidence: "high", reason: "x", riskSignals: [] } },
    scheduler: { maxWorkers: 3 }, tasks,
    createdAt: "2026-09-17T12:00:00.000Z", updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

async function execute(initial, run) {
  let value = structuredClone(initial); const writes = [];
  const state = { async get() { return structuredClone(value); }, async set(_k, next) { value = structuredClone(next); writes.push(structuredClone(next)); } };
  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  const result = await workflow({ expectedRevision: 20, taskId: "api" }, state, { run });
  return { result, value, writes };
}
function assertValid(value) { assert.equal(validate(value), true, JSON.stringify(validate.errors, null, 2)); }

test("applies reviewed handoff on source checkout and completes task", async () => {
  const dependent = integrationTask({
    id: "next", title: "Next", description: "Next.", status: "blocked", phase: "queued", dependsOn: ["api"], paths: ["test"],
    attempts: 0, assignment: null, blocker: { kind: "dependency", reason: "Waiting", taskIds: ["api"] }, result: null,
  });
  let seen;
  const { result, value } = await execute(board([integrationTask(), dependent]), async (key, spec) => {
    seen = { key, spec };
    return { ok: true, runId: "integration-run", output: "Patch applied exactly." };
  });
  assert.equal(seen.key, "integrate-api-1");
  assert.equal(seen.spec.agent, "worker");
  assert.equal(seen.spec.worktree, false);
  assert.match(seen.spec.task, /Apply the captured patch exactly/);
  assert.equal(result.status, "integration-pass");
  assert.equal(value.tasks[0].status, "done");
  assert.equal(value.tasks[0].phase, "complete");
  assert.equal(value.tasks[0].assignment, null);
  assert.equal(value.tasks[1].status, "todo");
  assertValid(value);
});

test("integration failure becomes explicit integration blocker", async () => {
  const { result, value } = await execute(board(), async () => ({ ok: false }));
  assert.equal(result.status, "integration-blocked");
  assert.equal(value.tasks[0].status, "blocked");
  assert.equal(value.tasks[0].blocker.kind, "integration");
  assertValid(value);
});

test("requires passing review and integration phase", async () => {
  await assert.rejects(
    () => execute(board([integrationTask({ phase: "review" })]), async () => ({ ok: true, runId: "x" })),
    /must be working\/integration/,
  );
});
