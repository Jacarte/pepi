import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/approve-kanban.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function baseBoard(overrides = {}) {
  return {
    schemaVersion: 1,
    revision: 1,
    workflow: {
      goal: "Implement refresh token rotation",
      tier: "T2",
      state: "waiting_approval",
      classification: {
        initialTier: "T2",
        confirmedTier: "T2",
        confidence: "high",
        reason: "Coordinated implementation work.",
        riskSignals: [],
      },
    },
    scheduler: {
      maxWorkers: 3,
    },
    tasks: [],
    createdAt: "2026-09-17T12:00:00.000Z",
    updatedAt: "2026-09-17T12:00:00.000Z",
    ...overrides,
  };
}

async function executeApproval(args, initialBoard) {
  let stored = initialBoard;
  const writes = [];

  const state = {
    async get(key) {
      assert.equal(key, "kanban");
      return stored;
    },
    async set(key, value) {
      assert.equal(key, "kanban");
      stored = value;
      writes.push(value);
    },
  };

  const workflow = new AsyncFunction("args", "state", workflowSource);
  const result = await workflow(args, state);

  return { result, stored, writes };
}

test("approval transitions waiting_approval to executing and bumps revision", async () => {
  const original = baseBoard();

  const { result, stored, writes } = await executeApproval(
    { expectedRevision: 1 },
    original,
  );

  assert.equal(writes.length, 1);
  assert.equal(result.workflow.state, "executing");
  assert.equal(result.revision, 2);
  assert.equal(stored.workflow.state, "executing");
  assert.equal(stored.revision, 2);
  assert.notEqual(stored.updatedAt, original.updatedAt);

  // The input board is not mutated in place.
  assert.equal(original.workflow.state, "waiting_approval");
  assert.equal(original.revision, 1);
});

test("approval rejects a stale expected revision", async () => {
  await assert.rejects(
    () => executeApproval(
      { expectedRevision: 1 },
      baseBoard({ revision: 2 }),
    ),
    /stale kanban revision: expected 1, current 2/,
  );
});

test("approval rejects uninitialized state", async () => {
  await assert.rejects(
    () => executeApproval({ expectedRevision: 1 }, undefined),
    /kanban state is not initialized/,
  );
});

test("approval rejects any workflow state other than waiting_approval", async () => {
  const board = baseBoard();
  board.workflow.state = "executing";

  await assert.rejects(
    () => executeApproval({ expectedRevision: 1 }, board),
    /requires workflow\.state=waiting_approval; current executing/,
  );
});

test("approval requires a positive integer expectedRevision", async () => {
  await assert.rejects(
    () => executeApproval({}, baseBoard()),
    /args\.expectedRevision must be a positive integer/,
  );

  await assert.rejects(
    () => executeApproval({ expectedRevision: 0 }, baseBoard()),
    /args\.expectedRevision must be a positive integer/,
  );
});
