import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/run-claimed-worker.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"),
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const validateBoard = ajv.compile(schema);

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function makeBoard({ modifying = true } = {}) {
  return {
    schemaVersion: 1,
    revision: 10,
    workflow: {
      goal: "Implement feature",
      tier: "T2",
      state: "executing",
      classification: {
        initialTier: "T2",
        confirmedTier: "T2",
        confidence: "high",
        reason: "Repository evidence confirms coordinated work.",
        riskSignals: [],
      },
    },
    scheduler: { maxWorkers: 1 },
    tasks: [
      {
        id: "task-a",
        title: "Implement task A",
        description: "Add the focused task-A behavior.",
        status: "working",
        phase: "implementation",
        dependsOn: [],
        paths: ["src/task-a.ts"],
        acceptance: ["focused tests pass"],
        modifying,
        attempts: 1,
        assignment: {
          workerKey: "worker-task-a-1",
          runId: null,
          attempt: 1,
          startedAt: "2026-09-17T12:00:00.000Z",
        },
        blocker: null,
        result: null,
      },
    ],
    createdAt: "2026-09-17T11:00:00.000Z",
    updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

function createState(initial) {
  let value = clone(initial);
  let writes = 0;

  return {
    api: {
      async get(key) {
        assert.equal(key, "kanban");
        return clone(value);
      },
      async set(key, next) {
        assert.equal(key, "kanban");
        writes += 1;
        value = clone(next);
      },
    },
    read() {
      return clone(value);
    },
    replace(next) {
      value = clone(next);
    },
    writes() {
      return writes;
    },
  };
}

async function executeWorker(args, state, run) {
  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  return workflow(args, state, { run });
}

test("launches modifying worker in a managed worktree and persists bounded metadata", async () => {
  const store = createState(makeBoard({ modifying: true }));
  const calls = [];

  const result = await executeWorker(
    { expectedRevision: 10, taskId: "task-a" },
    store.api,
    async (key, spec) => {
      calls.push({ key, spec });
      return {
        ok: true,
        runId: "run-worker-a",
        output: "Implemented task A and ran focused tests.",
        artifactPaths: [".pi/subagents/run-worker-a/handoff.json"],
      };
    },
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, "worker-task-a-1");
  assert.equal(calls[0].spec.agent, "worker");
  assert.equal(calls[0].spec.context, "fresh");
  assert.equal(calls[0].spec.worktree, true);
  assert.match(calls[0].spec.task, /Task ID: task-a/);
  assert.match(calls[0].spec.task, /src\/task-a\.ts/);
  assert.match(calls[0].spec.task, /focused tests pass/);
  assert.match(calls[0].spec.task, /stay inside that repository\/worktree/i);
  assert.match(calls[0].spec.task, /do not push, publish, or move remote-facing refs/i);

  assert.equal(store.writes(), 1);
  const board = store.read();
  assert.equal(validateBoard(board), true, JSON.stringify(validateBoard.errors));
  assert.equal(board.revision, 11);
  assert.equal(board.tasks[0].status, "working");
  assert.equal(board.tasks[0].phase, "verification");
  assert.equal(board.tasks[0].assignment.runId, "run-worker-a");
  assert.deepEqual(board.tasks[0].result, {
    summary: "Implemented task A and ran focused tests.",
    verification: "pending",
    review: "pending",
    runId: "run-worker-a",
    outputReference: ".pi/subagents/run-worker-a/handoff.json",
  });

  assert.equal(result.status, "worker-complete");
  assert.equal(result.revision, 11);
  assert.equal(result.phase, "verification");
  assert.equal(result.runId, "run-worker-a");
});

test("non-modifying tasks do not allocate a worktree and are explicitly read-only", async () => {
  const store = createState(makeBoard({ modifying: false }));
  let launch;

  await executeWorker(
    { expectedRevision: 10, taskId: "task-a" },
    store.api,
    async (key, spec) => {
      launch = { key, spec };
      return {
        ok: true,
        runId: "run-readonly-a",
        output: "Inspected the requested behavior.",
        artifactPaths: [],
      };
    },
  );

  assert.equal(launch.spec.worktree, false);
  assert.match(launch.spec.task, /This is a non-modifying task/);
  assert.match(launch.spec.task, /Do not edit, create, delete, or rewrite repository files/);
  assert.equal(store.read().tasks[0].result.outputReference, null);
});

test("rejects stale revision before launching a worker", async () => {
  const store = createState(makeBoard());
  let launches = 0;

  await assert.rejects(
    () => executeWorker(
      { expectedRevision: 9, taskId: "task-a" },
      store.api,
      async () => {
        launches += 1;
      },
    ),
    /stale kanban revision/,
  );

  assert.equal(launches, 0);
  assert.equal(store.writes(), 0);
});

test("requires an executing workflow and an implementation-phase claimed task", async () => {
  const notExecuting = makeBoard();
  notExecuting.workflow.state = "waiting_approval";
  const storeA = createState(notExecuting);

  await assert.rejects(
    () => executeWorker(
      { expectedRevision: 10, taskId: "task-a" },
      storeA.api,
      async () => ({ ok: true, runId: "unused" }),
    ),
    /workflow must be executing/,
  );

  const wrongPhase = makeBoard();
  wrongPhase.tasks[0].phase = "verification";
  const storeB = createState(wrongPhase);

  await assert.rejects(
    () => executeWorker(
      { expectedRevision: 10, taskId: "task-a" },
      storeB.api,
      async () => ({ ok: true, runId: "unused" }),
    ),
    /phase must be implementation/,
  );
});

test("does not relaunch an assignment that already has a runId", async () => {
  const board = makeBoard();
  board.tasks[0].assignment.runId = "existing-run";
  const store = createState(board);
  let launches = 0;

  await assert.rejects(
    () => executeWorker(
      { expectedRevision: 10, taskId: "task-a" },
      store.api,
      async () => {
        launches += 1;
      },
    ),
    /already been launched/,
  );

  assert.equal(launches, 0);
  assert.equal(store.writes(), 0);
});

test("failed or malformed worker results are not persisted", async () => {
  const failedStore = createState(makeBoard());

  await assert.rejects(
    () => executeWorker(
      { expectedRevision: 10, taskId: "task-a" },
      failedStore.api,
      async () => ({ ok: false, output: "failed" }),
    ),
    /worker worker-task-a-1 failed/,
  );
  assert.equal(failedStore.writes(), 0);
  assert.equal(failedStore.read().revision, 10);

  const missingIdStore = createState(makeBoard());
  await assert.rejects(
    () => executeWorker(
      { expectedRevision: 10, taskId: "task-a" },
      missingIdStore.api,
      async () => ({ ok: true, output: "done" }),
    ),
    /returned no runId/,
  );
  assert.equal(missingIdStore.writes(), 0);
});

test("rechecks revision after the worker finishes before persisting", async () => {
  const store = createState(makeBoard());

  await assert.rejects(
    () => executeWorker(
      { expectedRevision: 10, taskId: "task-a" },
      store.api,
      async () => {
        const concurrent = store.read();
        concurrent.revision = 11;
        concurrent.updatedAt = "2026-09-17T12:05:00.000Z";
        store.replace(concurrent);
        return {
          ok: true,
          runId: "run-worker-a",
          output: "Worker completed after another board update.",
          artifactPaths: ["handoff.json"],
        };
      },
    ),
    /stale kanban revision/,
  );

  assert.equal(store.writes(), 0);
  assert.equal(store.read().revision, 11);
  assert.equal(store.read().tasks[0].assignment.runId, null);
});

test("truncates persisted worker output to the schema bound", async () => {
  const store = createState(makeBoard());
  const longOutput = "x".repeat(2500);

  await executeWorker(
    { expectedRevision: 10, taskId: "task-a" },
    store.api,
    async () => ({
      ok: true,
      runId: "run-worker-a",
      output: longOutput,
      artifactPaths: [],
    }),
  );

  assert.equal(store.read().tasks[0].result.summary.length, 2000);
  assert.equal(validateBoard(store.read()), true, JSON.stringify(validateBoard.errors));
});
