import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/review-claimed-task.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"),
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const validate = ajv.compile(schema);

function task(overrides = {}) {
  return {
    id: "api",
    title: "Implement API change",
    description: "Implement the approved API behavior.",
    status: "working",
    phase: "review",
    dependsOn: [],
    paths: ["internal/api"],
    acceptance: ["focused tests pass"],
    modifying: true,
    attempts: 1,
    assignment: {
      workerKey: "worker-api-1",
      runId: "run-worker-api-1",
      attempt: 1,
      startedAt: "2026-09-17T12:00:00.000Z",
    },
    blocker: null,
    result: {
      summary: "Verification PASS: focused tests pass.",
      verification: "pass",
      review: "pending",
      runId: "run-worker-api-1",
      outputReference: "/tmp/pi-handoff/api.json",
    },
    ...overrides,
  };
}

function board(overrides = {}) {
  return {
    schemaVersion: 1,
    revision: 12,
    workflow: {
      goal: "Implement API change",
      tier: "T2",
      state: "executing",
      classification: {
        initialTier: "T2",
        confirmedTier: "T2",
        confidence: "high",
        reason: "Coordinated repository change.",
        riskSignals: [],
      },
    },
    scheduler: { maxWorkers: 3 },
    tasks: [task()],
    createdAt: "2026-09-17T11:00:00.000Z",
    updatedAt: "2026-09-17T12:00:00.000Z",
    ...overrides,
  };
}

async function execute(args, initialBoard, run, options = {}) {
  let value = structuredClone(initialBoard);
  let gets = 0;
  const writes = [];

  const state = {
    async get(key) {
      assert.equal(key, "kanban");
      gets += 1;
      if (options.onGet) {
        value = await options.onGet({ gets, value: structuredClone(value) });
      }
      return structuredClone(value);
    },
    async set(key, next) {
      assert.equal(key, "kanban");
      value = structuredClone(next);
      writes.push(structuredClone(next));
    },
  };

  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  const result = await workflow(args, state, { run });
  return { result, value, writes };
}

function reviewResult(verdict, findings = [], summary = "Review complete.") {
  return {
    ok: true,
    structuredOutput: { verdict, findings, summary },
  };
}

function assertValid(value) {
  assert.equal(validate(value), true, JSON.stringify(validate.errors, null, 2));
}

test("OK completes task and unlocks dependent task", async () => {
  const dependent = task({
    id: "integration-tests",
    title: "Add integration tests",
    description: "Cover the API behavior.",
    status: "blocked",
    phase: "queued",
    dependsOn: ["api"],
    paths: ["test/integration"],
    acceptance: ["integration tests pass"],
    attempts: 0,
    assignment: null,
    blocker: {
      kind: "dependency",
      reason: "Waiting for api",
      taskIds: ["api"],
    },
    result: null,
  });
  const initial = board({ tasks: [task(), dependent] });
  const calls = [];

  const { result, value, writes } = await execute(
    { expectedRevision: 12, taskId: "api" },
    initial,
    async (key, spec) => {
      calls.push({ key, spec });
      return reviewResult("OK", [], "No material issues found.");
    },
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, "review-api-1");
  assert.equal(calls[0].spec.agent, "reviewer");
  assert.equal(calls[0].spec.context, "fresh");
  assert.match(calls[0].spec.task, /handoff manifest is: \/tmp\/pi-handoff\/api\.json/);
  assert.match(calls[0].spec.task, /read-only reviewer/i);
  assert.deepEqual(calls[0].spec.outputSchema.properties.verdict.enum, [
    "OK",
    "OK_WITH_NOTES",
    "BLOCK",
  ]);

  assert.equal(result.status, "review-accepted");
  assert.equal(result.verdict, "OK");
  assert.equal(value.revision, 13);
  assert.equal(writes.length, 1);

  const completed = value.tasks.find((item) => item.id === "api");
  assert.equal(completed.status, "done");
  assert.equal(completed.phase, "complete");
  assert.equal(completed.assignment, null);
  assert.equal(completed.result.verification, "pass");
  assert.equal(completed.result.review, "pass");
  assert.match(completed.result.summary, /Review OK: No material issues found/);

  const unlocked = value.tasks.find((item) => item.id === "integration-tests");
  assert.equal(unlocked.status, "todo");
  assert.equal(unlocked.phase, "queued");
  assert.equal(unlocked.blocker, null);
  assertValid(value);
});

test("OK_WITH_NOTES accepts only non-blocking P2 findings", async () => {
  const { result, value } = await execute(
    { expectedRevision: 12, taskId: "api" },
    board(),
    async () =>
      reviewResult(
        "OK_WITH_NOTES",
        [{ severity: "P2", summary: "Consider a clearer local name.", location: "internal/api" }],
        "Correct with one non-blocking note.",
      ),
  );

  assert.equal(result.status, "review-accepted");
  assert.equal(result.findings[0].severity, "P2");
  assert.equal(value.tasks[0].status, "done");
  assert.equal(value.tasks[0].result.review, "pass");
  assertValid(value);
});

test("BLOCK persists review blocker evidence without completing task", async () => {
  const { result, value } = await execute(
    { expectedRevision: 12, taskId: "api" },
    board(),
    async () =>
      reviewResult(
        "BLOCK",
        [{ severity: "P1", summary: "Error path leaks the old contract.", location: "internal/api/error.go" }],
        "A blocking contract regression remains.",
      ),
  );

  assert.equal(result.status, "review-blocked");
  assert.equal(value.revision, 13);
  assert.equal(value.tasks[0].status, "working");
  assert.equal(value.tasks[0].phase, "review");
  assert.equal(value.tasks[0].result.review, "blocked");
  assert.ok(value.tasks[0].assignment);
  assertValid(value);
});

test("non-blocking verdict cannot contain P0/P1 findings", async () => {
  await assert.rejects(
    () =>
      execute(
        { expectedRevision: 12, taskId: "api" },
        board(),
        async () =>
          reviewResult("OK_WITH_NOTES", [
            { severity: "P1", summary: "Material regression." },
          ]),
      ),
    /non-blocking verdict with P0\/P1 findings/,
  );
});

test("review requires verification pass and pending review", async () => {
  const invalid = board({
    tasks: [
      task({
        result: {
          summary: "Verification failed.",
          verification: "fail",
          review: "pending",
          runId: "run-worker-api-1",
          outputReference: "/tmp/pi-handoff/api.json",
        },
      }),
    ],
  });

  await assert.rejects(
    () => execute({ expectedRevision: 12, taskId: "api" }, invalid, async () => reviewResult("OK")),
    /verification must be pass/,
  );
});

test("review rejects stale board changes after reviewer finishes", async () => {
  await assert.rejects(
    () =>
      execute(
        { expectedRevision: 12, taskId: "api" },
        board(),
        async () => reviewResult("OK"),
        {
          onGet({ gets, value }) {
            if (gets === 2) {
              value.revision = 13;
            }
            return value;
          },
        },
      ),
    /stale kanban revision: expected 12, got 13/,
  );
});

test("review requires a positive expected revision", async () => {
  await assert.rejects(
    () => execute({ expectedRevision: 0, taskId: "api" }, board(), async () => reviewResult("OK")),
    /expectedRevision must be a positive integer/,
  );
});
