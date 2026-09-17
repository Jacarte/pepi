import assert from "node:assert/strict";
import test from "node:test";

import {
  activeTasks,
  readyTasks,
  refreshDependencyBlockers,
  validateDag,
} from "./kanban-model.mjs";

function task(id, dependsOn = [], overrides = {}) {
  return {
    id,
    title: id,
    description: id,
    status: dependsOn.length === 0 ? "todo" : "blocked",
    phase: "queued",
    dependsOn,
    paths: [],
    acceptance: [],
    modifying: true,
    attempts: 0,
    assignment: null,
    blocker:
      dependsOn.length === 0
        ? null
        : {
            kind: "dependency",
            reason: "waiting for dependencies",
            taskIds: dependsOn,
          },
    result: null,
    ...overrides,
  };
}

function board(tasks) {
  return { tasks };
}

test("validateDag accepts an acyclic dependency graph", () => {
  const value = board([
    task("a"),
    task("b", ["a"]),
    task("c", ["a"]),
    task("d", ["b", "c"]),
  ]);

  assert.deepEqual(validateDag(value), []);
});

test("validateDag rejects duplicate task ids", () => {
  const errors = validateDag(board([task("a"), task("a")]));
  assert.match(errors.join("\n"), /duplicate task id: a/);
});

test("validateDag rejects unknown dependencies", () => {
  const errors = validateDag(board([task("a", ["missing"])]));
  assert.match(errors.join("\n"), /unknown task missing/);
});

test("validateDag rejects self dependencies", () => {
  const errors = validateDag(board([task("a", ["a"])]));
  assert.match(errors.join("\n"), /depends on itself/);
});

test("validateDag rejects cycles", () => {
  const errors = validateDag(
    board([
      task("a", ["c"]),
      task("b", ["a"]),
      task("c", ["b"]),
    ]),
  );

  assert.match(errors.join("\n"), /contains a cycle/);
});

test("readyTasks returns only dependency-ready todo tasks", () => {
  const value = board([
    task("a", [], { status: "done", phase: "complete" }),
    task("b", ["a"], { status: "todo", blocker: null }),
    task("c", ["b"]),
    task("d", [], {
      status: "working",
      phase: "implementation",
      assignment: {
        workerKey: "worker-d-1",
        attempt: 1,
        startedAt: "2026-09-17T12:00:00.000Z",
      },
    }),
  ]);

  assert.deepEqual(
    readyTasks(value).map((entry) => entry.id),
    ["b"],
  );
});

test("refreshDependencyBlockers unlocks satisfied dependency blockers only", () => {
  const value = board([
    task("a", [], { status: "done", phase: "complete" }),
    task("b", ["a"]),
    task("human", [], {
      status: "blocked",
      blocker: {
        kind: "human_decision",
        reason: "needs a decision",
        taskIds: [],
      },
    }),
  ]);

  refreshDependencyBlockers(value);

  assert.equal(value.tasks[1].status, "todo");
  assert.equal(value.tasks[1].blocker, null);
  assert.equal(value.tasks[2].status, "blocked");
});

test("activeTasks returns working tasks", () => {
  const value = board([
    task("a"),
    task("b", [], { status: "working" }),
    task("c", [], { status: "done", phase: "complete" }),
  ]);

  assert.deepEqual(
    activeTasks(value).map((entry) => entry.id),
    ["b"],
  );
});
