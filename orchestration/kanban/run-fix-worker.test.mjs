import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/run-fix-worker.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"),
);
const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const validateBoard = ajv.compile(schema);

function board(overrides = {}) {
  const value = {
    schemaVersion: 1,
    revision: 8,
    workflow: {
      goal: "Repair refresh token rotation",
      tier: "T2",
      state: "executing",
      classification: {
        initialTier: "T2",
        confirmedTier: "T2",
        confidence: "high",
        reason: "Coordinated auth change",
        riskSignals: [],
      },
    },
    scheduler: { maxWorkers: 3 },
    tasks: [
      {
        id: "refresh-token",
        title: "Repair refresh token rotation",
        description: "Correct the failed refresh token implementation.",
        status: "working",
        phase: "fix",
        dependsOn: [],
        paths: ["internal/auth/refresh.go"],
        acceptance: ["refresh rotation tests pass"],
        modifying: true,
        attempts: 2,
        assignment: {
          workerKey: "fix-refresh-token-2",
          runId: null,
          attempt: 2,
          startedAt: "2026-09-17T13:00:00.000Z",
        },
        blocker: null,
        result: {
          summary: "Verification FAIL: refresh token was not invalidated.",
          verification: "pending",
          review: "pending",
          runId: null,
          outputReference: "/tmp/pi-handoff/refresh-token-attempt-1.json",
        },
      },
    ],
    createdAt: "2026-09-17T12:00:00.000Z",
    updatedAt: "2026-09-17T13:00:00.000Z",
  };

  return Object.assign(value, overrides);
}

async function execute(args, initialBoard, run, getOverride) {
  let stored = initialBoard;
  const sets = [];
  let getCount = 0;

  const state = {
    async get(key) {
      assert.equal(key, "kanban");
      getCount += 1;
      if (getOverride) {
        const overridden = await getOverride(getCount, stored);
        if (overridden !== undefined) return overridden;
      }
      return stored;
    },
    async set(key, value) {
      assert.equal(key, "kanban");
      sets.push(value);
      stored = value;
    },
  };

  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  const result = await workflow(args, state, { run });
  return { result, stored, sets, getCount };
}

test("fix worker reconstructs prior handoff and returns task to verification", async () => {
  const original = board();
  let launch;

  const { result, stored, sets } = await execute(
    { expectedRevision: 8, taskId: "refresh-token" },
    original,
    async (key, spec) => {
      launch = { key, spec };
      return {
        ok: true,
        runId: "run-fix-2",
        output: "Applied focused invalidation fix; focused tests pass.",
        artifactPaths: ["/tmp/pi-handoff/refresh-token-attempt-2.json"],
      };
    },
  );

  assert.equal(launch.key, "fix-refresh-token-2");
  assert.equal(launch.spec.agent, "worker");
  assert.equal(launch.spec.context, "fresh");
  assert.equal(launch.spec.worktree, true);
  assert.match(launch.spec.task, /Previous implementation handoff:/);
  assert.match(
    launch.spec.task,
    /\/tmp\/pi-handoff\/refresh-token-attempt-1\.json/,
  );
  assert.match(launch.spec.task, /apply that exact prior patch/i);
  assert.match(launch.spec.task, /smallest evidence-backed correction/i);

  assert.equal(sets.length, 1);
  assert.equal(stored.revision, 9);
  assert.equal(stored.tasks[0].phase, "verification");
  assert.equal(stored.tasks[0].status, "working");
  assert.equal(stored.tasks[0].attempts, 2);
  assert.equal(stored.tasks[0].assignment.runId, "run-fix-2");
  assert.equal(stored.tasks[0].result.verification, "pending");
  assert.equal(stored.tasks[0].result.review, "pending");
  assert.equal(stored.tasks[0].result.runId, "run-fix-2");
  assert.equal(
    stored.tasks[0].result.outputReference,
    "/tmp/pi-handoff/refresh-token-attempt-2.json",
  );
  assert.match(stored.tasks[0].result.summary, /Fix attempt 2 completed/);
  assert.equal(validateBoard(stored), true, JSON.stringify(validateBoard.errors));

  // Workflow clones before persistence.
  assert.equal(original.revision, 8);
  assert.equal(original.tasks[0].phase, "fix");
  assert.equal(original.tasks[0].assignment.runId, null);

  assert.equal(result.status, "fix-complete");
  assert.equal(result.revision, 9);
  assert.equal(result.phase, "verification");
  assert.equal(result.runId, "run-fix-2");
});

test("fix worker bounds the persisted summary to schema size", async () => {
  const { stored } = await execute(
    { expectedRevision: 8, taskId: "refresh-token" },
    board(),
    async () => ({
      ok: true,
      runId: "run-fix-long",
      output: "x".repeat(10000),
      artifactPaths: ["/tmp/pi-handoff/fix-long.json"],
    }),
  );

  assert.ok(stored.tasks[0].result.summary.length <= 2000);
  assert.equal(validateBoard(stored), true, JSON.stringify(validateBoard.errors));
});

test("fix worker rejects non-modifying and malformed prepared tasks before launch", async () => {
  const cases = [
    {
      name: "non-modifying",
      mutate(task) {
        task.modifying = false;
      },
      pattern: /requires modifying=true/,
    },
    {
      name: "wrong phase",
      mutate(task) {
        task.phase = "verification";
      },
      pattern: /phase must be fix/,
    },
    {
      name: "already launched",
      mutate(task) {
        task.assignment.runId = "old-run";
      },
      pattern: /already been launched/,
    },
    {
      name: "missing handoff",
      mutate(task) {
        task.result.outputReference = null;
      },
      pattern: /no previous handoff reference/,
    },
    {
      name: "bad attempt",
      mutate(task) {
        task.assignment.attempt = 1;
      },
      pattern: /attempt is inconsistent/,
    },
  ];

  for (const entry of cases) {
    const value = board();
    entry.mutate(value.tasks[0]);
    let launched = false;

    await assert.rejects(
      () =>
        execute(
          { expectedRevision: 8, taskId: "refresh-token" },
          value,
          async () => {
            launched = true;
            return {};
          },
        ),
      entry.pattern,
      entry.name,
    );
    assert.equal(launched, false, entry.name);
  }
});

test("fix worker rejects malformed child results without persisting", async () => {
  const cases = [
    {
      name: "failed child",
      child: { ok: false, runId: "run" },
      pattern: /fix worker .* failed/,
    },
    {
      name: "missing runId",
      child: { ok: true, artifactPaths: ["/tmp/handoff.json"] },
      pattern: /returned no runId/,
    },
    {
      name: "missing handoff artifact",
      child: { ok: true, runId: "run", artifactPaths: [] },
      pattern: /returned no handoff artifact/,
    },
  ];

  for (const entry of cases) {
    const initial = board();
    let setCalled = false;
    const state = {
      async get() {
        return initial;
      },
      async set() {
        setCalled = true;
      },
    };
    const workflow = new AsyncFunction("args", "state", "runs", workflowSource);

    await assert.rejects(
      () =>
        workflow(
          { expectedRevision: 8, taskId: "refresh-token" },
          state,
          { run: async () => entry.child },
        ),
      entry.pattern,
      entry.name,
    );
    assert.equal(setCalled, false, entry.name);
  }
});

test("fix worker refuses to overwrite a board changed while child was running", async () => {
  const initial = board();
  const concurrent = structuredClone(initial);
  concurrent.revision = 9;
  concurrent.updatedAt = "2026-09-17T13:05:00.000Z";

  await assert.rejects(
    () =>
      execute(
        { expectedRevision: 8, taskId: "refresh-token" },
        initial,
        async () => ({
          ok: true,
          runId: "run-fix-2",
          output: "fixed",
          artifactPaths: ["/tmp/pi-handoff/new.json"],
        }),
        async (count) => (count === 2 ? concurrent : undefined),
      ),
    /stale kanban revision: expected 8, got 9/,
  );
});

test("fix worker validates args and workflow state", async () => {
  await assert.rejects(
    () => execute({ expectedRevision: 0, taskId: "refresh-token" }, board(), async () => ({})),
    /expectedRevision must be a positive integer/,
  );

  await assert.rejects(
    () => execute({ expectedRevision: 8, taskId: " " }, board(), async () => ({})),
    /taskId is required/,
  );

  const waiting = board();
  waiting.workflow.state = "waiting_approval";
  await assert.rejects(
    () => execute({ expectedRevision: 8, taskId: "refresh-token" }, waiting, async () => ({})),
    /workflow must be executing/,
  );

  await assert.rejects(
    () => execute({ expectedRevision: 7, taskId: "refresh-token" }, board(), async () => ({})),
    /stale kanban revision/,
  );
});
