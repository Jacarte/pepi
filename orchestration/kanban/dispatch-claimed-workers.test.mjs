import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/dispatch-claimed-workers.ts"), "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function task(id, overrides = {}) {
  return {
    id, title: id, description: `Do ${id}.`, status: "todo", phase: "queued",
    dependsOn: [], paths: [`src/${id}.ts`], acceptance: [`${id} works`], modifying: true,
    attempts: 0, assignment: null, blocker: null, result: null,
    ...overrides,
  };
}

function claimed(id, phase = "implementation") {
  return task(id, {
    status: "working", phase, attempts: 1,
    assignment: { workerKey: `${phase === "fix" ? "fix" : "worker"}-${id}-1`, runId: null, attempt: 1, startedAt: "2026-09-17T12:00:00.000Z" },
    result: phase === "fix" ? { summary: "Prior failed.", verification: "pending", review: "pending", runId: null, outputReference: `/tmp/${id}.manifest.json` } : null,
  });
}

function board(tasks, maxWorkers = 2) {
  return {
    schemaVersion: 1, revision: 7,
    workflow: { goal: "x", tier: "T2", state: "executing", classification: { initialTier: "T2", confirmedTier: "T2", confidence: "high", reason: "x", riskSignals: [] } },
    scheduler: { maxWorkers }, tasks,
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

test("uses rolling refill when a worker finishes before its sibling", async () => {
  const starts = [];
  const initial = board([claimed("a"), claimed("b"), task("c")], 2);
  const { result, stored, writes } = await execute(initial, async (key) => {
    starts.push(key);
    const delay = key.includes("b") ? 25 : 2;
    await new Promise((resolve) => setTimeout(resolve, delay));
    return { ok: true, runId: `run-${key}`, output: `done ${key}`, artifactPaths: [`/tmp/${key}.manifest.json`] };
  });

  assert.equal(result.status, "complete");
  assert.equal(result.launched, 3);
  assert.deepEqual(new Set(result.completed), new Set(["a", "b", "c"]));
  assert.equal(starts.length, 3);
  assert.ok(starts.indexOf("worker-c-1") > starts.indexOf("worker-a-1"));
  assert.equal(writes.length, 3, "each successful completion persists before refill");
  assert.equal(stored.revision, 10);
  assert.ok(stored.tasks.every((t) => t.phase === "verification"));
});

test("prepared fix tasks participate in the same worker pool", async () => {
  let prompt = "";
  const { stored } = await execute(board([claimed("a", "fix")], 1), async (_key, spec) => {
    prompt = spec.task;
    return { ok: true, runId: "fix-run", output: "fixed", artifactPaths: ["/tmp/new.manifest.json"] };
  });
  assert.match(prompt, /Previous handoff manifest/);
  assert.match(prompt, /reproduce its captured patch/);
  assert.equal(stored.tasks[0].phase, "verification");
  assert.equal(stored.tasks[0].result.outputReference, "/tmp/new.manifest.json");
});

test("a failed worker remains claimed but a successful sibling can refill remaining capacity", async () => {
  const initial = board([claimed("a"), claimed("b"), task("c")], 2);
  const { result, stored } = await execute(initial, async (key) => {
    if (key.includes("a")) {
      await new Promise((resolve) => setTimeout(resolve, 1));
      return { ok: false };
    }
    await new Promise((resolve) => setTimeout(resolve, 3));
    return { ok: true, runId: `run-${key}`, output: "done", artifactPaths: [`/tmp/${key}.manifest.json`] };
  });
  assert.equal(result.status, "partial");
  assert.deepEqual(result.failed, ["a"]);
  assert.ok(result.completed.includes("b"));
  assert.ok(result.completed.includes("c"));
  assert.equal(stored.tasks.find((t) => t.id === "a").phase, "implementation");
  assert.equal(stored.tasks.find((t) => t.id === "a").assignment.runId, null);
  assert.equal(stored.tasks.find((t) => t.id === "c").phase, "verification");
});

test("returns idle when no unlaunched worker-stage task exists", async () => {
  const verified = claimed("a");
  verified.phase = "verification";
  verified.assignment.runId = "run-a";
  verified.result = { summary: "done", verification: "pending", review: "pending", runId: "run-a", outputReference: "/tmp/a" };
  const { result, writes } = await execute(board([verified]), async () => { throw new Error("not called"); });
  assert.equal(result.status, "idle");
  assert.equal(writes.length, 0);
});

test("rejects a board already above worker capacity", async () => {
  await assert.rejects(
    () => execute(board([claimed("a"), claimed("b")], 1), async () => ({ ok: true })),
    /worker capacity exceeded/,
  );
});
