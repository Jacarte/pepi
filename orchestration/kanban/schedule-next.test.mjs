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

function workingTask(id, phase = "implementation") {
  return makeTask({
    id,
    title: id,
    status: "working",
    phase,
    attempts: 1,
    assignment: {
      workerKey: `worker-${id}-1`,
      runId: phase === "implementation" || phase === "fix" ? null : `run-${id}`,
      attempt: 1,
      startedAt: "2026-09-17T12:01:00.000Z",
    },
    result:
      phase === "verification" || phase === "review"
        ? {
            summary: "Worker completed.",
            verification: phase === "review" ? "pass" : "pending",
            review: "pending",
            runId: `run-${id}`,
            outputReference: `/tmp/${id}.handoff.json`,
          }
        : null,
  });
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

test("claims multiple ready tasks up to maxWorkers in one revision", async () => {
  const original = makeBoard({
    scheduler: { maxWorkers: 2 },
    tasks: [
      makeTask({ id: "a", title: "A", paths: ["src/a.ts"] }),
      makeTask({ id: "b", title: "B", paths: ["src/b.ts"] }),
      makeTask({ id: "c", title: "C", paths: ["src/c.ts"] }),
    ],
  });
  const snapshot = structuredClone(original);

  const { result, writes, stored } = await executeSchedule(
    { expectedRevision: 2 },
    original,
  );

  assert.equal(result.status, "claimed");
  assert.equal(result.revision, 3);
  assert.equal(result.maxWorkers, 2);
  assert.equal(result.availableBeforeClaim, 2);
  assert.deepEqual(
    result.claims.map((claim) => claim.taskId),
    ["a", "b"],
  );
  assert.deepEqual(result.workerActiveTaskIds, ["a", "b"]);

  assert.equal(writes.length, 1);
  assert.equal(stored.revision, 3);
  assert.equal(stored.tasks[0].status, "working");
  assert.equal(stored.tasks[1].status, "working");
  assert.equal(stored.tasks[2].status, "todo");
  assert.equal(stored.tasks[0].assignment.workerKey, "worker-a-1");
  assert.equal(stored.tasks[1].assignment.workerKey, "worker-b-1");
  assert.equal(
    stored.tasks[0].assignment.startedAt,
    stored.tasks[1].assignment.startedAt,
  );
  assert.equal(stored.updatedAt, stored.tasks[0].assignment.startedAt);
  assert.deepEqual(original, snapshot, "scheduler must not mutate caller state");
});

test("uses remaining capacity when one worker is already active", async () => {
  const board = makeBoard({
    scheduler: { maxWorkers: 3 },
    tasks: [
      workingTask("active"),
      makeTask({ id: "a", title: "A" }),
      makeTask({ id: "b", title: "B" }),
      makeTask({ id: "c", title: "C" }),
    ],
  });

  const { result, stored } = await executeSchedule(
    { expectedRevision: 2 },
    board,
  );

  assert.equal(result.status, "claimed");
  assert.equal(result.availableBeforeClaim, 2);
  assert.deepEqual(
    result.claims.map((claim) => claim.taskId),
    ["a", "b"],
  );
  assert.deepEqual(result.workerActiveTaskIds, ["active", "a", "b"]);
  assert.equal(stored.tasks.find((task) => task.id === "c").status, "todo");
});

test("verification and review cards do not consume worker capacity", async () => {
  const board = makeBoard({
    scheduler: { maxWorkers: 2 },
    tasks: [
      workingTask("verify", "verification"),
      workingTask("review", "review"),
      makeTask({ id: "a", title: "A" }),
      makeTask({ id: "b", title: "B" }),
    ],
  });

  const { result } = await executeSchedule({ expectedRevision: 2 }, board);

  assert.equal(result.availableBeforeClaim, 2);
  assert.deepEqual(
    result.claims.map((claim) => claim.taskId),
    ["a", "b"],
  );
  assert.deepEqual(result.workerActiveTaskIds, ["a", "b"]);
});

test("fix phase consumes worker capacity", async () => {
  const board = makeBoard({
    scheduler: { maxWorkers: 2 },
    tasks: [
      workingTask("fixing", "fix"),
      makeTask({ id: "a", title: "A" }),
      makeTask({ id: "b", title: "B" }),
    ],
  });

  const { result } = await executeSchedule({ expectedRevision: 2 }, board);

  assert.equal(result.availableBeforeClaim, 1);
  assert.deepEqual(result.claims.map((claim) => claim.taskId), ["a"]);
  assert.deepEqual(result.workerActiveTaskIds, ["fixing", "a"]);
});

test("returns busy without writing when worker capacity is full", async () => {
  const board = makeBoard({
    scheduler: { maxWorkers: 2 },
    tasks: [workingTask("a"), workingTask("b"), makeTask({ id: "c" })],
  });

  const { result, writes } = await executeSchedule(
    { expectedRevision: 2 },
    board,
  );

  assert.deepEqual(result, {
    status: "busy",
    revision: 2,
    maxWorkers: 2,
    availableCapacity: 0,
    workerActiveTaskIds: ["a", "b"],
  });
  assert.equal(writes.length, 0);
});

test("claims only dependency-ready tasks", async () => {
  const board = makeBoard({
    scheduler: { maxWorkers: 3 },
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
      makeTask({ id: "dependency", title: "Dependency" }),
      makeTask({
        id: "todo-but-not-ready",
        title: "Todo child",
        dependsOn: ["dependency"],
      }),
      makeTask({ id: "independent", title: "Independent" }),
    ],
  });

  const { result, stored } = await executeSchedule(
    { expectedRevision: 2 },
    board,
  );

  assert.deepEqual(
    result.claims.map((claim) => claim.taskId),
    ["dependency", "independent"],
  );
  assert.equal(
    stored.tasks.find((task) => task.id === "todo-but-not-ready").status,
    "todo",
  );
  assert.equal(
    stored.tasks.find((task) => task.id === "blocked-child").status,
    "blocked",
  );
});

test("returns idle without writing when unfinished work exists but nothing is ready", async () => {
  const board = makeBoard({
    scheduler: { maxWorkers: 3 },
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
    maxWorkers: 3,
    availableCapacity: 3,
    workerActiveTaskIds: [],
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

  assert.equal(result.status, "idle");
  assert.equal(result.reason, "no-unfinished-tasks");
  assert.equal(writes.length, 0);
});

test("rejects invalid maxWorkers and over-capacity boards", async () => {
  await assert.rejects(
    () =>
      executeSchedule(
        { expectedRevision: 2 },
        makeBoard({ scheduler: { maxWorkers: 0 } }),
      ),
    /scheduler\.maxWorkers must be an integer from 1 to 16/,
  );

  await assert.rejects(
    () =>
      executeSchedule(
        { expectedRevision: 2 },
        makeBoard({
          scheduler: { maxWorkers: 1 },
          tasks: [workingTask("a"), workingTask("b")],
        }),
      ),
    /active worker count 2 exceeds scheduler\.maxWorkers 1/,
  );
});

test("rejects stale revisions and non-executing workflows", async () => {
  await assert.rejects(
    () => executeSchedule({ expectedRevision: 1 }, makeBoard()),
    /stale kanban revision: expected 1, got 2/,
  );

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
