import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/verify-claimed-task.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"),
);
const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const validateBoard = ajv.compile(schema);

function makeBoard({ modifying = true, verification = "pending", phase = "verification" } = {}) {
  return {
    schemaVersion: 1,
    revision: 21,
    workflow: {
      goal: "Implement refresh token rotation",
      tier: "T2",
      state: "executing",
      classification: {
        initialTier: "T1",
        confirmedTier: "T2",
        confidence: "high",
        reason: "Repository evidence shows coordinated auth changes.",
        riskSignals: [],
      },
    },
    scheduler: { maxWorkers: 3 },
    tasks: [
      {
        id: "rotate-token",
        title: "Rotate refresh tokens",
        description: "Implement one-time refresh token rotation.",
        status: "working",
        phase,
        dependsOn: [],
        paths: ["internal/auth/refresh.go"],
        acceptance: [
          "Old refresh token is rejected after successful rotation.",
          "Focused auth tests pass.",
        ],
        modifying,
        attempts: 1,
        assignment: {
          workerKey: "worker-rotate-token-1",
          runId: "run-worker-123",
          attempt: 1,
          startedAt: "2026-09-17T12:00:00.000Z",
        },
        blocker: null,
        result: {
          summary: "Implemented token rotation and added focused tests.",
          verification,
          review: "pending",
          runId: "run-worker-123",
          outputReference: modifying ? "/pi-artifacts/handoffs/run-worker-123.json" : null,
        },
      },
    ],
    createdAt: "2026-09-17T11:00:00.000Z",
    updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function executeWorkflow(args, initialBoard, run) {
  let stored = clone(initialBoard);
  let setCalls = 0;

  const state = {
    get: async (key) => {
      assert.equal(key, "kanban");
      return clone(stored);
    },
    set: async (key, value) => {
      assert.equal(key, "kanban");
      setCalls += 1;
      stored = clone(value);
    },
  };

  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  const result = await workflow(args, state, { run });

  return {
    result,
    get board() {
      return clone(stored);
    },
    get setCalls() {
      return setCalls;
    },
  };
}

test("PASS verifies captured modifying patch in a fresh isolated verifier worktree", async () => {
  const calls = [];
  const execution = await executeWorkflow(
    { expectedRevision: 21, taskId: "rotate-token" },
    makeBoard({ modifying: true }),
    async (key, spec) => {
      calls.push({ key, spec });
      return {
        ok: true,
        structuredOutput: {
          verdict: "PASS",
          summary: "Focused auth tests pass against the captured worker patch.",
          commands: ["go test ./internal/auth/..."],
          evidence: ["rotation tests passed"],
        },
      };
    },
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, "verify-rotate-token-1");
  assert.equal(calls[0].spec.agent, "oracle");
  assert.equal(calls[0].spec.context, "fresh");
  assert.equal(calls[0].spec.worktree, true);
  assert.match(
    calls[0].spec.task,
    /exact pi-subagents handoff manifest is: \/pi-artifacts\/handoffs\/run-worker-123\.json/,
  );
  assert.match(calls[0].spec.task, /apply that exact patch/i);
  assert.match(calls[0].spec.task, /Do not author, improve, fix/i);
  assert.match(calls[0].spec.task, /Do not repair failures/i);
  assert.deepEqual(
    calls[0].spec.outputSchema.properties.verdict.enum,
    ["PASS", "FAIL", "BLOCKED"],
  );

  assert.equal(execution.result.status, "verification-pass");
  assert.equal(execution.result.verdict, "PASS");
  assert.deepEqual(execution.result.commands, ["go test ./internal/auth/..."]);
  assert.equal(execution.setCalls, 1);

  const board = execution.board;
  const task = board.tasks[0];
  assert.equal(board.revision, 22);
  assert.equal(task.status, "working");
  assert.equal(task.phase, "review");
  assert.equal(task.result.verification, "pass");
  assert.equal(task.result.review, "pending");
  assert.equal(task.assignment.runId, "run-worker-123");
  assert.equal(task.result.outputReference, "/pi-artifacts/handoffs/run-worker-123.json");
  assert.match(task.result.summary, /^Verification PASS:/);
  assert.match(task.result.summary, /Implementation:/);
  assert.equal(validateBoard(board), true, JSON.stringify(validateBoard.errors));
});

test("FAIL on a non-modifying task stays in verification and uses no worktree", async () => {
  const calls = [];
  const execution = await executeWorkflow(
    { expectedRevision: 21, taskId: "rotate-token" },
    makeBoard({ modifying: false }),
    async (key, spec) => {
      calls.push({ key, spec });
      return {
        ok: true,
        structuredOutput: {
          verdict: "FAIL",
          summary: "Repository evidence contradicts the expected behavior.",
          commands: ["go test ./internal/auth/..."],
          evidence: ["TestRotation failed"],
        },
      };
    },
  );

  assert.equal(calls[0].spec.worktree, false);
  assert.match(calls[0].spec.task, /This task is non-modifying/i);
  assert.match(calls[0].spec.task, /without editing, creating, deleting/i);

  const task = execution.board.tasks[0];
  assert.equal(execution.result.status, "verification-fail");
  assert.equal(task.status, "working");
  assert.equal(task.phase, "verification");
  assert.equal(task.result.verification, "fail");
  assert.equal(task.result.review, "pending");
  assert.equal(execution.board.revision, 22);
  assert.equal(validateBoard(execution.board), true, JSON.stringify(validateBoard.errors));
});

test("BLOCKED persists the verdict without inventing a task-state policy", async () => {
  const execution = await executeWorkflow(
    { expectedRevision: 21, taskId: "rotate-token" },
    makeBoard(),
    async () => ({
      ok: true,
      structuredOutput: {
        verdict: "BLOCKED",
        summary: "The captured patch artifact cannot be reconstructed.",
        commands: [],
        evidence: ["handoff patch missing"],
      },
    }),
  );

  const task = execution.board.tasks[0];
  assert.equal(execution.result.status, "verification-blocked");
  assert.equal(task.status, "working");
  assert.equal(task.phase, "verification");
  assert.equal(task.result.verification, "blocked");
  assert.equal(task.result.review, "pending");
});

test("modifying verification requires a durable worker handoff reference", async () => {
  const board = makeBoard({ modifying: true });
  board.tasks[0].result.outputReference = null;
  let launched = false;

  await assert.rejects(
    () =>
      executeWorkflow(
        { expectedRevision: 21, taskId: "rotate-token" },
        board,
        async () => {
          launched = true;
          return {};
        },
      ),
    /modifying worker result has no handoff reference/,
  );

  assert.equal(launched, false);
});

test("verification cannot be launched twice for the same task result", async () => {
  let launched = false;

  await assert.rejects(
    () =>
      executeWorkflow(
        { expectedRevision: 21, taskId: "rotate-token" },
        makeBoard({ verification: "fail" }),
        async () => {
          launched = true;
          return {};
        },
      ),
    /verification must be pending; got fail/,
  );

  assert.equal(launched, false);
});

test("verifier refuses the wrong task phase", async () => {
  await assert.rejects(
    () =>
      executeWorkflow(
        { expectedRevision: 21, taskId: "rotate-token" },
        makeBoard({ phase: "implementation" }),
        async () => ({
          ok: true,
          structuredOutput: { verdict: "PASS", summary: "unexpected" },
        }),
      ),
    /phase must be verification; got implementation/,
  );
});

test("verifier rejects stale revision after a long-running child", async () => {
  const initial = makeBoard();
  let stored = clone(initial);
  let setCalls = 0;
  const state = {
    get: async () => clone(stored),
    set: async (_key, value) => {
      setCalls += 1;
      stored = clone(value);
    },
  };
  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);

  await assert.rejects(
    () =>
      workflow(
        { expectedRevision: 21, taskId: "rotate-token" },
        state,
        {
          run: async () => {
            stored.revision = 22;
            stored.updatedAt = "2026-09-17T12:30:00.000Z";
            return {
              ok: true,
              structuredOutput: {
                verdict: "PASS",
                summary: "Verification completed against stale input.",
              },
            };
          },
        },
      ),
    /stale kanban revision: expected 21, got 22/,
  );

  assert.equal(setCalls, 0);
});

test("missing verifier structured output does not mutate Kanban", async () => {
  let setCalls = 0;
  let stored = makeBoard();
  const state = {
    get: async () => clone(stored),
    set: async (_key, value) => {
      setCalls += 1;
      stored = clone(value);
    },
  };
  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);

  await assert.rejects(
    () =>
      workflow(
        { expectedRevision: 21, taskId: "rotate-token" },
        state,
        { run: async () => ({ ok: true, output: "unstructured" }) },
      ),
    /returned no structured output/,
  );

  assert.equal(setCalls, 0);
  assert.equal(stored.revision, 21);
  assert.equal(stored.tasks[0].result.verification, "pending");
});
