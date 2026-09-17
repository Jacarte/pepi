import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/schedule-next.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function makeTask(overrides = {}) {
  return {
    id: "root",
    title: "Root task",
    description: "Implement the root task.",
    status: "todo",
    phase: "queued",
    dependsOn: [],
    paths: ["src/root.ts"],
    acceptance: ["Root behavior works."],
    modifying: true,
    attempts: 0,
    assignment: null,
    blocker: null,
    result: null,
    ...overrides,
  };
}

function makeBoard(overrides = {}) {
  return {
    schemaVersion: 1,
    revision: 2,
    workflow: {
      goal: "Implement feature",
      tier: "T2",
      state: "executing",
      classification: {
        initialTier: "T2",
        confirmedTier: "T2",
        confidence: "high",
        reason: "Coordinated feature work.",
        riskSignals: [],
      },
    },
    scheduler: { maxWorkers: 3 },
    tasks: [makeTask()],
    createdAt: "2026-09-17T12:00:00.000Z",
    updatedAt: "2026-09-17T12:01:00.000Z",
    ...overrides,
  };
}

async function executeSchedule(args, initialBoard) {
  const store = { kanban: structuredClone(initialBoard) };
  const writes = [];
  const state = {
    async get(key) {
      return structuredClone(store[key]);
    },
    async set(key, value) {
      writes.push({ key, value: structuredClone(value) });
      store[key] = structuredClone(value);
    },
  };

  const workflow = new AsyncFunction("args", "state", workflowSource);
  const result = await workflow(args, state);
  return { result, writes, stored: store.kanban };
}

test("claims the first ready task and persists exactly one revision", async () => {
  const original = makeBoard();
  const snapshot = structuredClone(original);
  const { result, writes, stored } = await executeSchedule(
    { expectedRevision: 2 },
    original,
  );

  assert.equal(result.status, "claimed");
  assert.equal(result.taskId, "root");
  assert.equal(result.workerKey, "worker-root-1");
  assert.equal(result.attempt, 1);
  assert.equal(result.revision, 3);
  assert.equal(result.task.title, "Root task");

  assert.equal(writes.length, 1);
  assert.equal(stored.revision, 3);
  assert.equal(stored.tasks[0].status, "working");
  assert.equal(stored.tasks[0].phase, "implementation");
  assert.equal(stored.tasks[0].attempts, 1);
  assert.equal(stored.tasks[0].assignment.workerKey, "worker-root-1");
  assert.equal(stored.tasks[0].assignment.runId, null);
  assert.equal(stored.tasks[0].assignment.attempt, 1);
  assert.equal(stored.tasks[0].assignment.startedAt, stored.updatedAt);
  assert.ok(!Number.isNaN(Date.parse(stored.updatedAt)));

  assert.deepEqual(original, snapshot, "scheduler must not mutate caller state");
});

test("skips dependency-blocked work and claims a different ready task", async () => {
  const board = makeBoard({
    tasks: [
      makeTask({
        id: "blocked-child",
        title: "Blocked child",
        status: "blocked",
        dependsOn: ["dependency"],
        blocker: {
          kind: "dependency",
          reason: "Waiting for dependency.",
          taskIds: ["dependency"],
        },
      }),
      makeTask({
        id: "dependency",
        title: "Dependency",
        paths: ["src/dependency.ts"],
      }),
    ],
  });

  const { result, stored } = await executeSchedule(
    { expectedRevision: 2 },
    board,
  );

  assert.equal(result.taskId, "dependency");
  assert.equal(stored.tasks[0].status, "blocked");
  assert.equal(stored.tasks[1].status, "working");
});

test("returns busy without writing when any task is already working", async () => {
  const board = makeBoard({
    tasks: [
      makeTask({
        status: "working",
        phase: "implementation",
        attempts: 1,
        assignment: {
          workerKey: "worker-root-1",
          runId: null,
          attempt: 1,
          startedAt: "2026-09-17T12:01:00.000Z",
        },
      }),
      makeTask({ id: "second", title: "Second task" }),
    ],
  });

  const { result, writes } = await executeSchedule(
    { expectedRevision: 2 },
    board,
  );

  assert.deepEqual(result, {
    status: "busy",
    revision: 2,
    activeTaskIds: ["root"],
  });
  assert.equal(writes.length, 0);
});

test("returns idle when unfinished work exists but nothing is ready", async () => {
  const board = makeBoard({
    tasks: [
      makeTask({
        status: "blocked",
        blocker: {
          kind: "human_decision",
          reason: "Need product decision.",
          taskIds: [],
        },
      }),
    ],
  });

  const { result, writes } = await executeSchedule(
    { expectedRevision: 2 },
    board,
  );

  assert.deepEqual(result, {
    status: "idle",
    reason: "no-ready-task",
    revision: 2,
  });
  assert.equal(writes.length, 0);
});

test("returns idle when all tasks are terminal", async () => {
  const board = makeBoard({
    tasks: [
      makeTask({
        status: "done",
        phase: "complete",
        result: {
          summary: "Done.",
          verification: "pass",
          review: "pass",
          runId: null,
          outputReference: null,
        },
      }),
    ],
  });

  const { result, writes } = await executeSchedule(
    { expectedRevision: 2 },
    board,
  );

  assert.deepEqual(result, {
    status: "idle",
    reason: "no-unfinished-tasks",
    revision: 2,
  });
  assert.equal(writes.length, 0);
});

test("rejects stale revisions", async () => {
  await assert.rejects(
    () => executeSchedule({ expectedRevision: 1 }, makeBoard()),
    /stale kanban revision: expected 1, got 2/,
  );
});

test("requires executing workflow state", async () => {
  const board = makeBoard();
  board.workflow.state = "waiting_approval";

  await assert.rejects(
    () => executeSchedule({ expectedRevision: 2 }, board),
    /kanban workflow must be executing/,
  );
});

test("requires a positive expected revision", async () => {
  await assert.rejects(
    () => executeSchedule({}, makeBoard()),
    /args\.expectedRevision must be a positive integer/,
  );
});
