import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/dispatch-claimed-workers.ts"), "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function task(id, phase = "implementation") {
  return {
    id, title: id, description: `Do ${id}.`, status: "working", phase,
    dependsOn: [], paths: [`src/${id}.ts`], acceptance: [`${id} works`], modifying: true,
    attempts: 1, assignment: { workerKey: `${phase === "fix" ? "fix" : "worker"}-${id}-1`, runId: null, attempt: 1, startedAt: "2026-09-17T12:00:00.000Z" },
    blocker: null,
    result: phase === "fix" ? { summary: "Prior failed.", verification: "pending", review: "pending", runId: null, outputReference: `/tmp/${id}.manifest.json` } : null,
  };
}

function board(tasks) {
  return {
    schemaVersion: 1, revision: 7,
    workflow: { goal: "x", tier: "T2", state: "executing", classification: { initialTier: "T2", confirmedTier: "T2", confidence: "high", reason: "x", riskSignals: [] } },
    scheduler: { maxWorkers: 3 }, tasks,
    createdAt: "2026-09-17T12:00:00.000Z", updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

async function execute(initial, run) {
  const store = { kanban: structuredClone(initial) };
  const writes = [];
  const state = {
    async get() { return structuredClone(store.kanban); },
    async set(_key, value) { writes.push(structuredClone(value)); store.kanban = structuredClone(value); },
  };
  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  const result = await workflow({ expectedRevision: 7 }, state, { run });
  return { result, stored: store.kanban, writes };
}

test("launches all claimed implementation workers concurrently and persists one revision", async () => {
  const calls = [];
  const original = board([task("a"), task("b")]);
  const { result, stored, writes } = await execute(original, async (key, spec) => {
    calls.push({ key, spec });
    await new Promise((resolve) => setTimeout(resolve, key.includes("a") ? 5 : 1));
    return { ok: true, runId: `run-${key}`, output: `done ${key}`, artifactPaths: [`/tmp/${key}.manifest.json`] };
  });
  assert.equal(calls.length, 2);
  assert.ok(calls.every((call) => call.spec.agent === "worker" && call.spec.worktree === true));
  assert.equal(result.status, "complete");
  assert.equal(result.completed.length, 2);
  assert.equal(writes.length, 1);
  assert.equal(stored.revision, 8);
  assert.ok(stored.tasks.every((t) => t.phase === "verification" && t.result.verification === "pending"));
});

test("supports prepared fix tasks and preserves isolated worktree launch", async () => {
  let prompt = "";
  const { stored } = await execute(board([task("a", "fix")]), async (_key, spec) => {
    prompt = spec.task;
    return { ok: true, runId: "fix-run", output: "fixed", artifactPaths: ["/tmp/new.manifest.json"] };
  });
  assert.match(prompt, /Previous handoff manifest/);
  assert.match(prompt, /reproduce its captured patch/);
  assert.equal(stored.tasks[0].phase, "verification");
  assert.equal(stored.tasks[0].result.outputReference, "/tmp/new.manifest.json");
});

test("partial failure persists successful siblings only", async () => {
  const { result, stored } = await execute(board([task("a"), task("b")]), async (key) => {
    if (key.includes("b")) return { ok: false };
    return { ok: true, runId: "run-a", output: "done", artifactPaths: ["/tmp/a.manifest.json"] };
  });
  assert.equal(result.status, "partial");
  assert.deepEqual(result.failed, ["b"]);
  assert.equal(stored.tasks[0].phase, "verification");
  assert.equal(stored.tasks[1].phase, "implementation");
  assert.equal(stored.tasks[1].assignment.runId, null);
});

test("returns idle when there are no unlaunched worker-active tasks", async () => {
  const done = task("a");
  done.phase = "verification";
  done.assignment.runId = "run-a";
  done.result = { summary: "done", verification: "pending", review: "pending", runId: "run-a", outputReference: "/tmp/a" };
  const { result, writes } = await execute(board([done]), async () => { throw new Error("not called"); });
  assert.equal(result.status, "idle");
  assert.equal(writes.length, 0);
});
