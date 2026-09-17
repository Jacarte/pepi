import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const scheduleSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/schedule-next.ts"), "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function task(id, overrides = {}) {
  return {
    id, title: id, description: `Do ${id}.`, status: "todo", phase: "queued",
    dependsOn: [], paths: [`src/${id}`], acceptance: [`${id} works`], modifying: true,
    attempts: 0, assignment: null, blocker: null, result: null,
    ...overrides,
  };
}

function board(tasks) {
  return {
    schemaVersion: 1, revision: 5,
    workflow: { goal: "x", tier: "T2", state: "executing", classification: { initialTier: "T2", confirmedTier: "T2", confidence: "high", reason: "x", riskSignals: [] } },
    scheduler: { maxWorkers: 2 }, tasks,
    createdAt: "2026-09-17T12:00:00.000Z", updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

async function schedule(initial) {
  let value = structuredClone(initial);
  const writes = [];
  const state = {
    async get() { return structuredClone(value); },
    async set(_key, next) { value = structuredClone(next); writes.push(structuredClone(next)); },
  };
  const workflow = new AsyncFunction("args", "state", scheduleSource);
  const result = await workflow({ expectedRevision: 5 }, state);
  return { result, value, writes };
}

function integrationLease(id, paths) {
  return task(id, {
    status: "working", phase: "integration", paths, attempts: 1,
    assignment: { workerKey: `worker-${id}-1`, runId: `run-${id}`, attempt: 1, startedAt: "2026-09-17T12:00:00.000Z" },
    result: { summary: "Reviewed and accepted.", verification: "pass", review: "pass", runId: `run-${id}`, outputReference: `/tmp/${id}.handoff.json` },
  });
}

test("integration-phase modifying task keeps its path lease", async () => {
  const initial = board([
    integrationLease("accepted", ["src/api"]),
    task("conflict", { paths: ["src/api/handler.ts"] }),
    task("independent", { paths: ["src/other"] }),
  ]);
  const { result, value } = await schedule(initial);
  assert.equal(result.status, "claimed");
  assert.deepEqual(result.claims.map((claim) => claim.taskId), ["independent"]);
  assert.ok(result.deferredConflictTaskIds.includes("conflict"));
  assert.equal(value.tasks.find((item) => item.id === "conflict").status, "todo");
});

test("verification/review modifying tasks also retain path ownership", async () => {
  for (const phase of ["verification", "review"]) {
    const owner = integrationLease("owner", ["pkg/service"]);
    owner.phase = phase;
    if (phase === "verification") owner.result.review = "pending";
    const { result } = await schedule(board([owner, task("conflict", { paths: ["pkg/service/file.go"] })]));
    assert.equal(result.status, "idle");
    assert.equal(result.reason, "path-conflict");
  }
});
