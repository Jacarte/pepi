import assert from "node:assert/strict";
import test from "node:test";

import {
  beginFix,
  beginReview,
  beginVerification,
  blockTask,
  completeTask,
  requeueTask,
  startTask,
} from "./kanban-transitions.mjs";

function makeTask(overrides = {}) {
  return {
    id: "task-a",
    title: "Task A",
    description: "Implement task A.",
    status: "todo",
    phase: "queued",
    dependsOn: [],
    paths: [],
    acceptance: [],
    modifying: true,
    attempts: 0,
    assignment: null,
    blocker: null,
    result: null,
    ...overrides,
  };
}

function makeBoard(tasks) {
  return {
    schemaVersion: 1,
    revision: 1,
    workflow: {
      goal: "Test workflow",
      tier: "T2",
      state: "executing",
      classification: {
        initialTier: "T2",
        confirmedTier: "T2",
        confidence: "high",
        reason: "Test fixture",
        riskSignals: [],
      },
    },
    scheduler: { maxWorkers: 2 },
    tasks,
    createdAt: "2026-09-17T12:00:00.000Z",
    updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

test("startTask claims a ready task and increments its attempt", () => {
  const board = makeBoard([makeTask()]);

  startTask(board, "task-a", {
    workerKey: "worker-task-a-1",
    runId: "run-1",
    at: "2026-09-17T12:01:00.000Z",
  });

  const task = board.tasks[0];
  assert.equal(task.status, "working");
  assert.equal(task.phase, "implementation");
  assert.equal(task.attempts, 1);
  assert.deepEqual(task.assignment, {
    workerKey: "worker-task-a-1",
    runId: "run-1",
    attempt: 1,
    startedAt: "2026-09-17T12:01:00.000Z",
  });
  assert.equal(board.revision, 2);
  assert.equal(board.updatedAt, "2026-09-17T12:01:00.000Z");
});

test("startTask rejects a task with unfinished dependencies", () => {
  const board = makeBoard([
    makeTask({ id: "task-a" }),
    makeTask({ id: "task-b", dependsOn: ["task-a"] }),
  ]);

  assert.throws(
    () => startTask(board, "task-b", { workerKey: "worker-b" }),
    /unfinished dependencies/,
  );
  assert.equal(board.revision, 1);
});

test("verification and review advance a working task through phases", () => {
  const board = makeBoard([makeTask()]);

  startTask(board, "task-a", {
    workerKey: "worker-a",
    at: "2026-09-17T12:01:00.000Z",
  });
  beginVerification(board, "task-a", {
    at: "2026-09-17T12:02:00.000Z",
  });
  beginReview(board, "task-a", {
    at: "2026-09-17T12:03:00.000Z",
  });

  assert.equal(board.tasks[0].status, "working");
  assert.equal(board.tasks[0].phase, "review");
  assert.equal(board.revision, 4);
  assert.equal(board.updatedAt, "2026-09-17T12:03:00.000Z");
});

test("beginFix moves a failed verification or review back to fix", () => {
  const board = makeBoard([makeTask()]);

  startTask(board, "task-a", { workerKey: "worker-a" });
  beginVerification(board, "task-a");
  beginFix(board, "task-a", { at: "2026-09-17T12:04:00.000Z" });

  assert.equal(board.tasks[0].phase, "fix");
  assert.equal(board.updatedAt, "2026-09-17T12:04:00.000Z");
});

test("blockTask clears the active assignment and records the blocker", () => {
  const board = makeBoard([makeTask()]);

  startTask(board, "task-a", { workerKey: "worker-a" });
  blockTask(
    board,
    "task-a",
    {
      kind: "human_decision",
      reason: "Compatibility behavior needs a user decision.",
      taskIds: [],
    },
    { at: "2026-09-17T12:05:00.000Z" },
  );

  const task = board.tasks[0];
  assert.equal(task.status, "blocked");
  assert.equal(task.phase, "queued");
  assert.equal(task.assignment, null);
  assert.deepEqual(task.blocker, {
    kind: "human_decision",
    reason: "Compatibility behavior needs a user decision.",
    taskIds: [],
  });
  assert.equal(board.revision, 3);
});

test("requeueTask refuses unresolved dependency blockers", () => {
  const board = makeBoard([
    makeTask({ id: "task-a" }),
    makeTask({
      id: "task-b",
      status: "blocked",
      dependsOn: ["task-a"],
      blocker: {
        kind: "dependency",
        reason: "Waiting for task A.",
        taskIds: ["task-a"],
      },
    }),
  ]);

  assert.throws(
    () => requeueTask(board, "task-b"),
    /unfinished dependencies/,
  );
  assert.equal(board.revision, 1);
});

test("requeueTask releases a resolved non-dependency blocker", () => {
  const board = makeBoard([
    makeTask({
      status: "blocked",
      blocker: {
        kind: "technical",
        reason: "Temporary tool failure.",
        taskIds: [],
      },
    }),
  ]);

  requeueTask(board, "task-a", {
    at: "2026-09-17T12:06:00.000Z",
  });

  assert.equal(board.tasks[0].status, "todo");
  assert.equal(board.tasks[0].phase, "queued");
  assert.equal(board.tasks[0].blocker, null);
  assert.equal(board.revision, 2);
});

test("completeTask marks the task done and unlocks its dependents atomically", () => {
  const board = makeBoard([
    makeTask({ id: "task-a" }),
    makeTask({
      id: "task-b",
      status: "blocked",
      dependsOn: ["task-a"],
      blocker: {
        kind: "dependency",
        reason: "Waiting for task A.",
        taskIds: ["task-a"],
      },
    }),
  ]);

  startTask(board, "task-a", { workerKey: "worker-a" });
  beginVerification(board, "task-a");
  beginReview(board, "task-a");

  completeTask(
    board,
    "task-a",
    {
      summary: "Implemented task A.",
      verification: "pass",
      review: "pass",
      runId: "run-a",
      outputReference: "artifact-a",
    },
    { at: "2026-09-17T12:10:00.000Z" },
  );

  const [completed, dependent] = board.tasks;
  assert.equal(completed.status, "done");
  assert.equal(completed.phase, "complete");
  assert.equal(completed.assignment, null);
  assert.deepEqual(completed.result, {
    summary: "Implemented task A.",
    verification: "pass",
    review: "pass",
    runId: "run-a",
    outputReference: "artifact-a",
  });

  assert.equal(dependent.status, "todo");
  assert.equal(dependent.phase, "queued");
  assert.equal(dependent.blocker, null);
  assert.equal(board.revision, 5);
  assert.equal(board.updatedAt, "2026-09-17T12:10:00.000Z");
});

test("completeTask rejects completion without passing verification and review", () => {
  const board = makeBoard([makeTask()]);

  startTask(board, "task-a", { workerKey: "worker-a" });
  beginVerification(board, "task-a");
  beginReview(board, "task-a");

  const revisionBefore = board.revision;

  assert.throws(
    () =>
      completeTask(board, "task-a", {
        summary: "Not ready.",
        verification: "fail",
        review: "pass",
      }),
    /passing verification/,
  );

  assert.equal(board.tasks[0].status, "working");
  assert.equal(board.tasks[0].phase, "review");
  assert.equal(board.revision, revisionBefore);
});
