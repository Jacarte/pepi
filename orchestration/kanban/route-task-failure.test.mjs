import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/route-task-failure.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"),
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const validate = ajv.compile(schema);

function boardWith({
  phase = "verification",
  verification = "fail",
  review = "pending",
  attempts = 1,
  modifying = true,
} = {}) {
  return {
    schemaVersion: 1,
    revision: 8,
    workflow: {
      goal: "Implement refresh token rotation",
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
        id: "session-rotation",
        title: "Implement session rotation",
        description: "Rotate refresh tokens safely.",
        status: "working",
        phase,
        dependsOn: [],
        paths: ["internal/session"],
        acceptance: ["Focused tests pass"],
        modifying,
        attempts,
        assignment: {
          workerKey: `worker-session-rotation-${attempts}`,
          runId: `run-${attempts}`,
          attempt: attempts,
          startedAt: "2026-09-17T12:00:00.000Z",
        },
        blocker: null,
        result: {
          summary: "Implementation and independent evidence summary.",
          verification,
          review,
          runId: `run-${attempts}`,
          outputReference: modifying
            ? `/tmp/pi-handoff/session-rotation-${attempts}.json`
            : null,
        },
      },
      {
        id: "session-tests",
        title: "Add integration tests",
        description: "Cover rotation behavior.",
        status: "blocked",
        phase: "queued",
        dependsOn: ["session-rotation"],
        paths: ["internal/session"],
        acceptance: ["Integration tests pass"],
        modifying: true,
        attempts: 0,
        assignment: null,
        blocker: {
          kind: "dependency",
          reason: "Waiting for session-rotation",
          taskIds: ["session-rotation"],
        },
        result: null,
      },
    ],
    createdAt: "2026-09-17T11:00:00.000Z",
    updatedAt: "2026-09-17T12:30:00.000Z",
  };
}

async function execute(board, args = { expectedRevision: 8, taskId: "session-rotation" }) {
  let stored = structuredClone(board);
  const workflow = new AsyncFunction("args", "state", workflowSource);
  const result = await workflow(args, {
    get: async (key) => {
      assert.equal(key, "kanban");
      return structuredClone(stored);
    },
    set: async (key, value) => {
      assert.equal(key, "kanban");
      stored = structuredClone(value);
    },
  });

  return { result, stored };
}

function assertValid(board) {
  assert.equal(validate(board), true, JSON.stringify(validate.errors));
}

test("verification fail prepares bounded modifying fix attempt", async () => {
  const input = boardWith({ attempts: 1, verification: "fail" });
  const before = structuredClone(input);
  const { result, stored } = await execute(input);

  assert.deepEqual(input, before);
  assert.equal(result.status, "fix-prepared");
  assert.equal(result.source, "verification-fail");
  assert.equal(result.attempt, 2);
  assert.equal(result.maxAttempts, 3);
  assert.equal(result.workerKey, "fix-session-rotation-2");
  assert.equal(result.revision, 9);

  const task = stored.tasks[0];
  assert.equal(task.status, "working");
  assert.equal(task.phase, "fix");
  assert.equal(task.attempts, 2);
  assert.equal(task.assignment.workerKey, "fix-session-rotation-2");
  assert.equal(task.assignment.runId, null);
  assert.equal(task.assignment.attempt, 2);
  assert.equal(task.result.verification, "pending");
  assert.equal(task.result.review, "pending");
  assert.equal(task.result.runId, null);
  assert.equal(
    task.result.outputReference,
    "/tmp/pi-handoff/session-rotation-1.json",
  );
  assert.match(task.result.summary, /automatic fix attempt 2\/3/i);
  assert.match(task.result.summary, /run-1/);
  assert.equal(stored.revision, 9);
  assertValid(stored);
});

test("review block prepares final allowed fix attempt", async () => {
  const input = boardWith({
    phase: "review",
    verification: "pass",
    review: "blocked",
    attempts: 2,
  });

  const { result, stored } = await execute(input);

  assert.equal(result.status, "fix-prepared");
  assert.equal(result.source, "review-blocked");
  assert.equal(result.attempt, 3);
  assert.equal(stored.tasks[0].phase, "fix");
  assert.equal(stored.tasks[0].attempts, 3);
  assert.equal(stored.tasks[0].assignment.workerKey, "fix-session-rotation-3");
  assertValid(stored);
});

test("verification blocked becomes infrastructure blocker without retry", async () => {
  const input = boardWith({ verification: "blocked", attempts: 1 });
  const { result, stored } = await execute(input);

  assert.equal(result.status, "task-blocked");
  assert.equal(result.blockerKind, "infrastructure");
  assert.equal(result.source, "verification-blocked");

  const task = stored.tasks[0];
  assert.equal(task.status, "blocked");
  assert.equal(task.phase, "queued");
  assert.equal(task.assignment, null);
  assert.equal(task.blocker.kind, "infrastructure");
  assert.equal(task.result.verification, "blocked");
  assert.match(task.blocker.reason, /verification evidence or tooling/i);
  assertValid(stored);
});

test("exhausted modifying failure becomes technical blocker", async () => {
  const input = boardWith({ verification: "fail", attempts: 3 });
  const { result, stored } = await execute(input);

  assert.equal(result.status, "task-blocked");
  assert.equal(result.blockerKind, "technical");

  const task = stored.tasks[0];
  assert.equal(task.status, "blocked");
  assert.equal(task.assignment, null);
  assert.equal(task.blocker.kind, "technical");
  assert.match(task.blocker.reason, /3\/3 attempts/i);
  assertValid(stored);
});

test("non-modifying verification fail blocks instead of inventing a fix", async () => {
  const input = boardWith({ modifying: false, verification: "fail", attempts: 1 });
  const { result, stored } = await execute(input);

  assert.equal(result.status, "task-blocked");
  assert.equal(result.blockerKind, "technical");
  assert.match(stored.tasks[0].blocker.reason, /non-modifying task/i);
  assertValid(stored);
});

test("does not change workflow state when one task becomes blocked", async () => {
  const input = boardWith({ verification: "blocked" });
  const { stored } = await execute(input);

  assert.equal(stored.workflow.state, "executing");
  assert.equal(stored.tasks[1].status, "blocked");
  assert.equal(stored.tasks[1].blocker.kind, "dependency");
});

test("rejects review routing unless verification already passed", async () => {
  const input = boardWith({
    phase: "review",
    verification: "fail",
    review: "blocked",
  });

  await assert.rejects(
    () => execute(input),
    /review routing requires verification=pass/,
  );
});

test("rejects non-failure task outcomes", async () => {
  const input = boardWith({ verification: "pending" });

  await assert.rejects(
    () => execute(input),
    /verification must be fail or blocked/,
  );
});

test("rejects stale revision", async () => {
  await assert.rejects(
    () => execute(boardWith(), { expectedRevision: 7, taskId: "session-rotation" }),
    /stale kanban revision/,
  );
});

test("requires task id and positive revision", async () => {
  await assert.rejects(
    () => execute(boardWith(), { expectedRevision: 0, taskId: "session-rotation" }),
    /expectedRevision must be a positive integer/,
  );

  await assert.rejects(
    () => execute(boardWith(), { expectedRevision: 8, taskId: "   " }),
    /args\.taskId is required/,
  );
});
