import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowSource = fs.readFileSync(path.resolve(__dirname, "../../workflows/prepare-kanban.ts"), "utf8");
const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const ajv = new Ajv({ allErrors: true, strict: true }); addFormats(ajv); const validate = ajv.compile(schema);

async function execute(args, responses, initial = null) {
  let board = initial ? structuredClone(initial) : null;
  const calls = [];
  const state = { async get() { return structuredClone(board); }, async set(_k, value) { board = structuredClone(value); } };
  const workflow = new AsyncFunction("args", "state", "runs", workflowSource);
  let index = 0;
  const result = await workflow(args, state, {
    run: async (key, spec) => {
      calls.push({ key, spec });
      const value = responses[index++];
      if (typeof value === "function") return value(key, spec);
      return value;
    },
  });
  return { result, board, calls };
}

const t2 = { tier: "T2", confidence: "high", needsScout: false, reason: "Coordinated change.", riskSignals: [] };

test("creates waiting-approval board for confirmed T2 plan", async () => {
  const plan = {
    summary: "Two tasks.",
    tasks: [
      { id: "api", title: "API", description: "Implement API.", dependsOn: [], paths: ["internal/api"], acceptance: ["tests pass"], modifying: true },
      { id: "tests", title: "Tests", description: "Add tests.", dependsOn: ["api"], paths: ["test/api"], acceptance: ["integration tests pass"], modifying: true },
    ],
  };
  const { result, board, calls } = await execute(
    { task: "Implement API feature", maxWorkers: 4 },
    [
      { ok: true, structuredOutput: t2 },
      { ok: true, structuredOutput: plan },
    ],
  );
  assert.equal(result.status, "waiting-approval");
  assert.equal(result.taskCount, 2);
  assert.equal(calls[0].key, "prepare-triage");
  assert.equal(calls[1].key, "prepare-plan");
  assert.equal(board.workflow.state, "waiting_approval");
  assert.equal(board.scheduler.maxWorkers, 4);
  assert.equal(board.tasks[0].status, "todo");
  assert.equal(board.tasks[1].status, "blocked");
  assert.equal(validate(board), true, JSON.stringify(validate.errors, null, 2));
});

test("uses scout then confirmation when initial triage requests repository evidence", async () => {
  const initial = { tier: "T1", confidence: "medium", needsScout: true, reason: "Scope unknown.", riskSignals: [] };
  const confirmed = { ...t2, reason: "Repository evidence shows coordinated scope." };
  const plan = { summary: "One task.", tasks: [{ id: "change", title: "Change", description: "Change it.", dependsOn: [], paths: ["src"], acceptance: ["works"], modifying: true }] };
  const { result, calls } = await execute(
    { task: "Change auth" },
    [
      { ok: true, structuredOutput: initial },
      { ok: true, output: "Touches auth service and shared contract." },
      { ok: true, structuredOutput: confirmed },
      { ok: true, structuredOutput: plan },
    ],
  );
  assert.equal(result.scoutUsed, true);
  assert.deepEqual(calls.map((call) => call.key), ["prepare-triage", "prepare-scout", "prepare-confirm", "prepare-plan"]);
});

test("simple tiers route without creating Kanban", async () => {
  const simple = { tier: "T1", confidence: "high", needsScout: false, reason: "Small edit.", riskSignals: [] };
  const { result, board, calls } = await execute({ task: "Rename local variable" }, [{ ok: true, structuredOutput: simple }]);
  assert.equal(result.status, "simple-route");
  assert.equal(result.kanbanCreated, false);
  assert.equal(board, null);
  assert.equal(calls.length, 1);
});

test("rejects cyclic planner DAG", async () => {
  const cyclic = {
    summary: "bad",
    tasks: [
      { id: "a", title: "A", description: "A", dependsOn: ["b"], paths: ["a"], acceptance: ["a"], modifying: true },
      { id: "b", title: "B", description: "B", dependsOn: ["a"], paths: ["b"], acceptance: ["b"], modifying: true },
    ],
  };
  await assert.rejects(
    () => execute({ task: "Complex" }, [{ ok: true, structuredOutput: t2 }, { ok: true, structuredOutput: cyclic }]),
    /contains a cycle/,
  );
});
