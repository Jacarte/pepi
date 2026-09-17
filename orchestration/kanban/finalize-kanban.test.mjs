import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/finalize-kanban.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"),
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const validate = ajv.compile(schema);

function baseTask(id, overrides = {}) {
  return {
    id,
    title: id,
    description: `Task ${id}`,
    status: "todo",
    phase: "queued",
    dependsOn: [],
    paths: [],
    acceptance: ["acceptance"],
    modifying: true,
    attempts: 0,
    assignment: null,
    blocker: null,
    result: null,
    ...overrides,
  };
}

function board(tasks, overrides = {}) {
  return {
    schemaVersion: 1,
    revision: 10,
    workflow: {
      goal: "Finish the mission",
      tier: "T2",
      state: "executing",
      classification: {
        initialTier: "T2",
        confirmedTier: "T2",
        confidence: "high",
        reason: "test",
        riskSignals: [],
      },
    },
    scheduler: { maxWorkers: 3 },
    tasks,
    createdAt: "2026-09-17T12:00:00.000Z",
    updatedAt: "2026-09-17T12:00:00.000Z",
    ...overrides,
  };
}

async function executeFinalize(args, initial) {
  let stored = structuredClone(initial);
  const writes = [];
  const state = {
    async get(key) {
      assert.equal(key, "kanban");
      return structuredClone(stored);
    },
    async set(key, value) {
      assert.equal(key, "kanban");
      stored = structuredClone(value);
      writes.push(structuredClone(value));
    },
  };

  const workflow = new AsyncFunction("args", "state", workflowSource);
  const result = await workflow(args, state);
  return { result, stored, writes };
}

function assertValid(value) {
  assert.equal(validate(value), true, JSON.stringify(validate.errors));
}

test("completes workflow when every task is terminal", async () => {
  const initial = board([
    baseTask("a", {
      status: "done",
      phase: "complete",
      result: {
        summary: "done",
        verification: "pass",
        review: "pass",
        runId: "run-a",
        outputReference: null,
      },
    }),
    baseTask("b", { status: "cancelled" }),
  ]);

  const { result, stored, writes } = await executeFinalize(
    { expectedRevision: 10 },
    initial,
  );

  assert.equal(result.status, "completed");
  assert.equal(stored.workflow.state, "completed");
  assert.equal(stored.revision, 11);
  assert.equal(writes.length, 1);
  assert.deepEqual(result.completedTaskIds, ["a"]);
  assert.deepEqual(result.cancelledTaskIds, ["b"]);
  assertValid(stored);
  assert.equal(initial.workflow.state, "executing");
  assert.equal(initial.revision, 10);
});

test("does not finalize while a task is working", async () => {
  const initial = board([
    baseTask("a", {
      status: "working",
      phase: "implementation",
      attempts: 1,
      assignment: {
        workerKey: "worker-a-1",
        runId: null,
        attempt: 1,
        startedAt: "2026-09-17T12:00:00.000Z",
      },
    }),
  ]);

  const { result, stored, writes } = await executeFinalize(
    { expectedRevision: 10 },
    initial,
  );

  assert.deepEqual(result.taskIds, ["a"]);
  assert.equal(result.status, "running");
  assert.equal(result.reason, "working-tasks");
  assert.equal(stored.workflow.state, "executing");
  assert.equal(writes.length, 0);
});

test("does not finalize while a task is ready", async () => {
  const initial = board([baseTask("a")]);
  const { result, writes } = await executeFinalize(
    { expectedRevision: 10 },
    initial,
  );

  assert.equal(result.status, "running");
  assert.equal(result.reason, "ready-tasks");
  assert.deepEqual(result.taskIds, ["a"]);
  assert.equal(writes.length, 0);
});

test("marks workflow blocked when no work can run and explicit blockers remain", async () => {
  const initial = board([
    baseTask("a", {
      status: "blocked",
      blocker: {
        kind: "technical",
        reason: "automatic repair limit exhausted",
        taskIds: [],
      },
    }),
    baseTask("b", {
      status: "blocked",
      dependsOn: ["a"],
      blocker: {
        kind: "dependency",
        reason: "waiting for a",
        taskIds: ["a"],
      },
    }),
  ]);

  const { result, stored, writes } = await executeFinalize(
    { expectedRevision: 10 },
    initial,
  );

  assert.equal(result.status, "blocked");
  assert.equal(stored.workflow.state, "blocked");
  assert.equal(stored.revision, 11);
  assert.equal(writes.length, 1);
  assert.equal(result.blockers.length, 2);
  assert.equal(result.blockers[0].kind, "technical");
  assertValid(stored);
});

test("ready work wins over unrelated blockers", async () => {
  const initial = board([
    baseTask("blocked", {
      status: "blocked",
      blocker: {
        kind: "technical",
        reason: "blocked",
        taskIds: [],
      },
    }),
    baseTask("ready"),
  ]);

  const { result, writes } = await executeFinalize(
    { expectedRevision: 10 },
    initial,
  );

  assert.equal(result.status, "running");
  assert.equal(result.reason, "ready-tasks");
  assert.deepEqual(result.taskIds, ["ready"]);
  assert.equal(writes.length, 0);
});

test("throws on stalled unfinished state without runnable or blocker state", async () => {
  const initial = board([
    baseTask("a", { dependsOn: ["missing"] }),
  ]);

  await assert.rejects(
    () => executeFinalize({ expectedRevision: 10 }, initial),
    /kanban is stalled with unfinished tasks/,
  );
});

test("rejects stale revision", async () => {
  await assert.rejects(
    () => executeFinalize({ expectedRevision: 9 }, board([baseTask("a")])),
    /stale kanban revision/,
  );
});

test("requires executing workflow", async () => {
  const initial = board([baseTask("a")]);
  initial.workflow.state = "blocked";

  await assert.rejects(
    () => executeFinalize({ expectedRevision: 10 }, initial),
    /kanban workflow must be executing/,
  );
});

test("requires positive expectedRevision", async () => {
  await assert.rejects(
    () => executeFinalize({ expectedRevision: 0 }, board([])),
    /args\.expectedRevision must be a positive integer/,
  );
});
