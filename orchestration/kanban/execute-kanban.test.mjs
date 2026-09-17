import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/execute-kanban.ts"), "utf8");
const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const ajv = new Ajv({ allErrors: true, strict: true }); addFormats(ajv); const validate = ajv.compile(schema);

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
    schemaVersion: 1, revision: 2,
    workflow: { goal: "finish", tier: "T2", state: "executing", classification: { initialTier: "T2", confirmedTier: "T2", confidence: "high", reason: "x", riskSignals: [] } },
    scheduler: { maxWorkers: 2 }, tasks,
    createdAt: "2026-09-17T12:00:00.000Z", updatedAt: "2026-09-17T12:00:00.000Z",
  };
}

async function execute(initial, run) {
  let value = structuredClone(initial);
  const writes = [];
  const state = {
    async get() { return structuredClone(value); },
    async set(_key, next) {
      assert.equal(validate(next), true, JSON.stringify(validate.errors, null, 2));
      value = structuredClone(next); writes.push(structuredClone(next));
    },
  };
  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  const result = await workflow({ expectedRevision: 2, maxCycles: 100 }, state, { run });
  return { result, value, writes };
}

function oraclePass() {
  return { ok: true, structuredOutput: { verdict: "PASS", summary: "Fresh checks pass.", commands: ["test"], evidence: ["green"] } };
}
function reviewOk() {
  return { ok: true, structuredOutput: { verdict: "OK", findings: [], summary: "No material issues." } };
}

test("executes dependency chain through worker, verify, review, integration and completion", async () => {
  const root = task("root", { paths: ["src/root"] });
  const child = task("child", {
    modifying: false, paths: [], status: "blocked", dependsOn: ["root"],
    blocker: { kind: "dependency", reason: "Waiting", taskIds: ["root"] },
  });
  const calls = [];
  const { result, value } = await execute(board([root, child]), async (key, spec) => {
    calls.push({ key, spec });
    if (key.startsWith("verify-")) return oraclePass();
    if (key.startsWith("review-")) return reviewOk();
    if (key.startsWith("integrate-")) return { ok: true, runId: `run-${key}`, output: "Applied exactly." };
    return {
      ok: true,
      runId: `run-${key}`,
      output: `completed ${key}`,
      artifactPaths: spec.worktree ? [`/tmp/${key}.handoff.json`] : [],
    };
  });

  assert.equal(result.status, "completed");
  assert.equal(value.workflow.state, "completed");
  assert.ok(value.tasks.every((item) => item.status === "done"));
  assert.equal(value.tasks[0].phase, "complete");
  assert.equal(value.tasks[1].phase, "complete");
  assert.ok(calls.some((call) => call.key === "worker-root-1"));
  assert.ok(calls.some((call) => call.key === "verify-root-1"));
  assert.ok(calls.some((call) => call.key === "review-root-1"));
  assert.ok(calls.some((call) => call.key === "integrate-root-1"));
  assert.ok(calls.some((call) => call.key === "worker-child-1"));
});

test("verification failure prepares one fix and re-enters verification", async () => {
  let verificationCount = 0;
  const { result, value } = await execute(board([task("root")]), async (key, spec) => {
    if (key.startsWith("verify-")) {
      verificationCount += 1;
      if (verificationCount === 1) {
        return { ok: true, structuredOutput: { verdict: "FAIL", summary: "Focused test fails.", commands: ["test"], evidence: ["failure"] } };
      }
      return oraclePass();
    }
    if (key.startsWith("review-")) return reviewOk();
    if (key.startsWith("integrate-")) return { ok: true, runId: "integration-run", output: "Applied." };
    if (key.startsWith("fix-")) {
      assert.match(spec.task, /bounded repair attempt/i);
      return { ok: true, runId: `run-${key}`, output: "Fixed.", artifactPaths: [`/tmp/${key}.handoff.json`] };
    }
    return { ok: true, runId: `run-${key}`, output: "Initial.", artifactPaths: [`/tmp/${key}.handoff.json`] };
  });

  assert.equal(result.status, "completed");
  assert.equal(verificationCount, 2);
  assert.equal(value.tasks[0].attempts, 2);
  assert.equal(value.tasks[0].status, "done");
  assert.equal(result.stats.fixAttemptsPrepared, 1);
});

test("ends blocked when worker infrastructure fails", async () => {
  const { result, value } = await execute(board([task("root")]), async () => ({ ok: false }));
  assert.equal(result.status, "blocked");
  assert.equal(value.workflow.state, "blocked");
  assert.equal(value.tasks[0].blocker.kind, "infrastructure");
});
