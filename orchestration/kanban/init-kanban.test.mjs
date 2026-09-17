import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import Ajv from "ajv";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/init-kanban.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"),
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const validateSchema = ajv.compile(schema);

function baseArgs() {
  return {
    task: "Implement refresh token rotation",
    triage: {
      initial: {
        tier: "T1",
        confidence: "medium",
        needsScout: true,
        reason: "Repository scope was unknown.",
        riskSignals: [],
      },
      confirmed: {
        tier: "T2",
        confidence: "high",
        needsScout: false,
        reason: "Repository evidence shows a coordinated API change.",
        riskSignals: ["api-contract"],
      },
      scoutUsed: true,
    },
    plan: {
      summary: "Update shared types, then API, then tests.",
      tasks: [
        {
          id: "shared-types",
          title: "Update shared token types",
          description: "Add the new refresh token representation.",
          dependsOn: [],
          paths: ["internal/auth/types.go"],
          acceptance: ["The new token representation exists."],
          modifying: true,
        },
        {
          id: "api",
          title: "Implement refresh API",
          description: "Use the new representation in the API.",
          dependsOn: ["shared-types"],
          paths: ["internal/api"],
          acceptance: ["Refresh rotates tokens."],
          modifying: true,
        },
      ],
    },
  };
}

async function executeInit(args, initialState = new Map()) {
  const writes = [];
  const state = {
    async get(key) {
      return initialState.get(key);
    },
    async set(key, value) {
      writes.push({ key, value });
      initialState.set(key, value);
    },
  };

  const workflow = new AsyncFunction("args", "state", workflowSource);
  const result = await workflow(args, state);
  return { result, writes, state: initialState };
}

test("initializes revision 1 board in waiting_approval", async () => {
  const { result, writes } = await executeInit(baseArgs());

  assert.equal(result.schemaVersion, 1);
  assert.equal(result.revision, 1);
  assert.equal(result.workflow.goal, "Implement refresh token rotation");
  assert.equal(result.workflow.tier, "T2");
  assert.equal(result.workflow.state, "waiting_approval");
  assert.equal(result.scheduler.maxWorkers, 3);

  assert.deepEqual(result.workflow.classification, {
    initialTier: "T1",
    confirmedTier: "T2",
    confidence: "high",
    reason: "Repository evidence shows a coordinated API change.",
    riskSignals: ["api-contract"],
  });

  assert.equal(writes.length, 1);
  assert.equal(writes[0].key, "kanban");
  assert.deepEqual(writes[0].value, result);
  assert.equal(result.createdAt, result.updatedAt);
  assert.ok(!Number.isNaN(Date.parse(result.createdAt)));

  assert.equal(
    validateSchema(result),
    true,
    JSON.stringify(validateSchema.errors),
  );
});

test("root tasks start todo and dependent tasks start dependency-blocked", async () => {
  const { result } = await executeInit(baseArgs());
  const root = result.tasks.find((task) => task.id === "shared-types");
  const dependent = result.tasks.find((task) => task.id === "api");

  assert.equal(root.status, "todo");
  assert.equal(root.phase, "queued");
  assert.equal(root.blocker, null);
  assert.equal(root.assignment, null);
  assert.equal(root.attempts, 0);

  assert.equal(dependent.status, "blocked");
  assert.equal(dependent.phase, "queued");
  assert.deepEqual(dependent.blocker, {
    kind: "dependency",
    reason: "Waiting for dependencies: shared-types",
    taskIds: ["shared-types"],
  });
});

test("uses explicit worker capacity", async () => {
  const args = baseArgs();
  args.maxWorkers = 5;

  const { result } = await executeInit(args);
  assert.equal(result.scheduler.maxWorkers, 5);
});

test("does not overwrite existing Kanban mission state", async () => {
  const state = new Map([["kanban", { revision: 7 }]]);

  await assert.rejects(
    () => executeInit(baseArgs(), state),
    /kanban mission state is already initialized/,
  );
});

test("requires confirmed T2 or T3 classification", async () => {
  const args = baseArgs();
  args.triage.confirmed.tier = "T1";

  await assert.rejects(
    () => executeInit(args),
    /requires confirmed T2 or T3 work/,
  );
});

test("rejects invalid worker capacity", async () => {
  const args = baseArgs();
  args.maxWorkers = 0;

  await assert.rejects(
    () => executeInit(args),
    /maxWorkers must be an integer between 1 and 16/,
  );
});

test("defensively rejects unknown dependencies", async () => {
  const args = baseArgs();
  args.plan.tasks[1].dependsOn = ["missing-task"];

  await assert.rejects(
    () => executeInit(args),
    /depends on unknown task missing-task/,
  );
});

test("defensively rejects cyclic plans", async () => {
  const args = baseArgs();
  args.plan.tasks[0].dependsOn = ["api"];

  await assert.rejects(
    () => executeInit(args),
    /dependency graph contains a cycle/,
  );
});
