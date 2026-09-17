import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/review-claimed-task.ts"), "utf8");
const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const validate = ajv.compile(schema);

function task(overrides = {}) {
  return {
    id: "api", title: "API", description: "Implement API behavior.",
    status: "working", phase: "review", dependsOn: [], paths: ["internal/api"],
    acceptance: ["focused tests pass"], modifying: true, attempts: 1,
    assignment: { workerKey: "worker-api-1", runId: "run-api-1", attempt: 1, startedAt: "2026-09-17T12:00:00.000Z" },
    blocker: null,
    result: { summary: "Verification passed.", verification: "pass", review: "pending", runId: "run-api-1", outputReference: "/tmp/api-handoff.json" },
    ...overrides,
  };
}

function board(tasks = [task()]) {
  return {
    schemaVersion: 1, revision: 12,
    workflow: { goal: "API", tier: "T2", state: "executing", classification: { initialTier: "T2", confirmedTier: "T2", confidence: "high", reason: "x", riskSignals: [] } },
    scheduler: { maxWorkers: 3 }, tasks,
    createdAt: "2026-09-17T11:00:00.000Z", updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

async function execute(initial, reviewResult, options = {}) {
  let value = structuredClone(initial);
  let gets = 0;
  const writes = [];
  const state = {
    async get() {
      gets += 1;
      if (options.onGet) value = await options.onGet({ gets, value: structuredClone(value) });
      return structuredClone(value);
    },
    async set(_key, next) { value = structuredClone(next); writes.push(structuredClone(next)); },
  };
  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  const result = await workflow({ expectedRevision: 12, taskId: "api" }, state, {
    run: async (_key, spec) => {
      assert.equal(spec.agent, "reviewer");
      assert.equal(spec.context, "fresh");
      return { ok: true, structuredOutput: reviewResult };
    },
  });
  return { result, value, writes };
}

function assertValid(value) {
  assert.equal(validate(value), true, JSON.stringify(validate.errors, null, 2));
}

test("accepted modifying review keeps task working in integration", async () => {
  const dependent = task({
    id: "tests", title: "Tests", description: "Add integration tests.", status: "blocked", phase: "queued",
    dependsOn: ["api"], paths: ["test/integration"], acceptance: ["tests pass"], attempts: 0,
    assignment: null, blocker: { kind: "dependency", reason: "Waiting for api", taskIds: ["api"] }, result: null,
  });
  const { result, value } = await execute(board([task(), dependent]), {
    verdict: "OK", findings: [], summary: "No material issues.",
  });
  assert.equal(result.status, "review-accepted-awaiting-integration");
  const api = value.tasks.find((item) => item.id === "api");
  assert.equal(api.status, "working");
  assert.equal(api.phase, "integration");
  assert.equal(api.result.review, "pass");
  assert.ok(api.assignment, "assignment/path lease stays until integration");
  const tests = value.tasks.find((item) => item.id === "tests");
  assert.equal(tests.status, "blocked", "dependents do not unlock before integration");
  assertValid(value);
});

test("accepted non-modifying review completes and unlocks dependents", async () => {
  const source = task({ modifying: false, paths: [], result: { summary: "Verified analysis.", verification: "pass", review: "pending", runId: "run-api-1", outputReference: null } });
  const dependent = task({
    id: "next", title: "Next", description: "Follow-up.", status: "blocked", phase: "queued",
    dependsOn: ["api"], paths: [], acceptance: ["done"], modifying: false, attempts: 0,
    assignment: null, blocker: { kind: "dependency", reason: "Waiting", taskIds: ["api"] }, result: null,
  });
  const { result, value } = await execute(board([source, dependent]), {
    verdict: "OK_WITH_NOTES", findings: [{ severity: "P2", summary: "Minor note." }], summary: "Accepted.",
  });
  assert.equal(result.status, "review-accepted");
  assert.equal(value.tasks[0].status, "done");
  assert.equal(value.tasks[0].phase, "complete");
  assert.equal(value.tasks[0].assignment, null);
  assert.equal(value.tasks[1].status, "todo");
  assertValid(value);
});

test("BLOCK remains in review", async () => {
  const { result, value } = await execute(board(), {
    verdict: "BLOCK", findings: [{ severity: "P1", summary: "Regression." }], summary: "Blocking issue.",
  });
  assert.equal(result.status, "review-blocked");
  assert.equal(value.tasks[0].status, "working");
  assert.equal(value.tasks[0].phase, "review");
  assert.equal(value.tasks[0].result.review, "blocked");
  assertValid(value);
});

test("non-blocking verdict rejects P0/P1 findings", async () => {
  await assert.rejects(
    () => execute(board(), { verdict: "OK_WITH_NOTES", findings: [{ severity: "P1", summary: "Bad." }], summary: "x" }),
    /non-blocking verdict with P0\/P1 findings/,
  );
});

test("rejects stale post-review board change", async () => {
  await assert.rejects(
    () => execute(board(), { verdict: "OK", findings: [], summary: "x" }, {
      onGet({ gets, value }) { if (gets === 2) value.revision = 13; return value; },
    }),
    /stale kanban revision: expected 12, got 13/,
  );
});
