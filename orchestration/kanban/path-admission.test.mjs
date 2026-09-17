import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const scheduleSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/schedule-next.ts"), "utf8");
const dispatchSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/dispatch-claimed-workers.ts"), "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function task(id, overrides = {}) {
  return {
    id, title: id, description: id, status: "todo", phase: "queued",
    dependsOn: [], paths: [`src/${id}.ts`], acceptance: [`${id} works`], modifying: true,
    attempts: 0, assignment: null, blocker: null, result: null,
    ...overrides,
  };
}

function claimed(id, paths) {
  return task(id, {
    status: "working", phase: "implementation", paths, attempts: 1,
    assignment: { workerKey: `worker-${id}-1`, runId: null, attempt: 1, startedAt: "2026-09-17T12:00:00.000Z" },
  });
}

function board(tasks, maxWorkers = 3) {
  return {
    schemaVersion: 1, revision: 2,
    workflow: { goal: "x", tier: "T2", state: "executing", classification: { initialTier: "T2", confirmedTier: "T2", confidence: "high", reason: "x", riskSignals: [] } },
    scheduler: { maxWorkers }, tasks,
    createdAt: "2026-09-17T12:00:00.000Z", updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

async function runSchedule(initial) {
  const store = { kanban: structuredClone(initial) };
  const state = {
    async get() { return structuredClone(store.kanban); },
    async set(_key, value) { store.kanban = structuredClone(value); },
  };
  const workflow = new AsyncFunction("args", "state", scheduleSource);
  return { result: await workflow({ expectedRevision: 2 }, state), stored: store.kanban };
}

test("scheduler defers exact-path and ancestor-path conflicts", async () => {
  const { result, stored } = await runSchedule(board([
    task("a", { paths: ["src/auth"] }),
    task("b", { paths: ["src/auth/token.ts"] }),
    task("c", { paths: ["src/payments.ts"] }),
  ], 3));

  assert.deepEqual(result.claims.map((claim) => claim.taskId), ["a", "c"]);
  assert.deepEqual(result.deferredConflictTaskIds, ["b"]);
  assert.equal(stored.tasks.find((t) => t.id === "b").status, "todo");
});

test("unknown modifying ownership serializes against other modifying work", async () => {
  const { result } = await runSchedule(board([
    task("unknown", { paths: [] }),
    task("known", { paths: ["src/known.ts"] }),
  ], 2));

  assert.deepEqual(result.claims.map((claim) => claim.taskId), ["unknown"]);
  assert.deepEqual(result.deferredConflictTaskIds, ["known"]);
});

test("non-modifying tasks can run beside overlapping modifying ownership", async () => {
  const { result } = await runSchedule(board([
    task("writer", { paths: ["src/auth"] }),
    task("reader", { paths: ["src/auth/token.ts"], modifying: false }),
  ], 2));
  assert.deepEqual(result.claims.map((claim) => claim.taskId), ["writer", "reader"]);
});

test("scheduler reports path-conflict when an active writer owns the only ready path", async () => {
  const { result } = await runSchedule(board([
    claimed("active", ["src/auth"]),
    task("next", { paths: ["src/auth/token.ts"] }),
  ], 2));

  assert.equal(result.status, "idle");
  assert.equal(result.reason, "path-conflict");
  assert.deepEqual(result.deferredConflictTaskIds, ["next"]);
});

test("rolling dispatcher rejects an already-invalid overlapping active set", async () => {
  const initial = board([
    claimed("a", ["src/auth"]),
    claimed("b", ["src/auth/token.ts"]),
  ], 2);
  initial.revision = 7;
  const state = { async get() { return structuredClone(initial); }, async set() {} };
  const workflow = new AsyncFunction("args", "state", "runs", dispatchSource);
  await assert.rejects(
    () => workflow({ expectedRevision: 7 }, state, { run: async () => ({ ok: true }) }),
    /active modifying tasks overlap paths: a and b/,
  );
});
