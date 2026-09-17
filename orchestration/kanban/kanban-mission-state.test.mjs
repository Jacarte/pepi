import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const workflowSource = fs.readFileSync(
  path.join(__dirname, "..", "..", "workflows", "kanban-state.ts"),
  "utf8",
);

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runWorkflow = new AsyncFunction("args", "state", workflowSource);

function board(revision = 1) {
  return {
    schemaVersion: 1,
    revision,
    workflow: {
      goal: "Test mission state",
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
    scheduler: {
      maxWorkers: 2,
    },
    tasks: [],
    createdAt: "2026-09-17T12:00:00.000Z",
    updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

function fakeState(initial = {}) {
  const values = new Map(Object.entries(initial));
  const writes = [];

  return {
    writes,
    values,
    async get(key) {
      return values.get(key);
    },
    async set(key, value) {
      writes.push({ key, value });
      values.set(key, value);
    },
  };
}

test("get returns not-found before initialization", async () => {
  const state = fakeState();

  const result = await runWorkflow({ action: "get" }, state);

  assert.deepEqual(result, {
    action: "get",
    found: false,
    board: null,
  });
  assert.equal(state.writes.length, 0);
});

test("init stores revision 1 under the kanban mission key", async () => {
  const state = fakeState();
  const value = board(1);

  const result = await runWorkflow(
    { action: "init", board: value },
    state,
  );

  assert.equal(result.action, "init");
  assert.equal(result.revision, 1);
  assert.deepEqual(state.values.get("kanban"), value);
  assert.equal(state.writes.length, 1);
  assert.equal(state.writes[0].key, "kanban");
});

test("init refuses to overwrite existing mission state", async () => {
  const state = fakeState({ kanban: board(1) });

  await assert.rejects(
    runWorkflow({ action: "init", board: board(1) }, state),
    /already initialized/,
  );

  assert.equal(state.writes.length, 0);
});

test("init requires revision 1", async () => {
  const state = fakeState();

  await assert.rejects(
    runWorkflow({ action: "init", board: board(2) }, state),
    /revision must be 1/,
  );

  assert.equal(state.writes.length, 0);
});

test("replace persists exactly the next revision", async () => {
  const current = board(3);
  const replacement = board(4);
  const state = fakeState({ kanban: current });

  const result = await runWorkflow(
    {
      action: "replace",
      expectedRevision: 3,
      board: replacement,
    },
    state,
  );

  assert.equal(result.previousRevision, 3);
  assert.equal(result.revision, 4);
  assert.deepEqual(state.values.get("kanban"), replacement);
  assert.equal(state.writes.length, 1);
});

test("replace rejects a stale expected revision", async () => {
  const state = fakeState({ kanban: board(4) });

  await assert.rejects(
    runWorkflow(
      {
        action: "replace",
        expectedRevision: 3,
        board: board(5),
      },
      state,
    ),
    /Stale Kanban revision: expected 3, current 4/,
  );

  assert.equal(state.writes.length, 0);
});

test("replace cannot skip revisions", async () => {
  const state = fakeState({ kanban: board(4) });

  await assert.rejects(
    runWorkflow(
      {
        action: "replace",
        expectedRevision: 4,
        board: board(6),
      },
      state,
    ),
    /Replacement Kanban revision must be 5/,
  );

  assert.equal(state.writes.length, 0);
});

test("replace requires initialized mission state", async () => {
  const state = fakeState();

  await assert.rejects(
    runWorkflow(
      {
        action: "replace",
        expectedRevision: 1,
        board: board(2),
      },
      state,
    ),
    /not initialized/,
  );

  assert.equal(state.writes.length, 0);
});
