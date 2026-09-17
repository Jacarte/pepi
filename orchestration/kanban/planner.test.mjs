import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/planner.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

async function executePlanner(args, run) {
  const workflow = new AsyncFunction("args", "runs", workflowSource);
  return workflow(args, { run });
}

function validPlan() {
  return {
    summary: "Update shared auth types, then API and CLI, then integration tests.",
    tasks: [
      {
        id: "shared-types",
        title: "Update shared auth types",
        description: "Introduce the token rotation data model.",
        dependsOn: [],
        paths: ["internal/auth/types.go"],
        acceptance: ["The token rotation model is represented."],
        modifying: true,
      },
      {
        id: "api",
        title: "Implement refresh API",
        description: "Use the shared model in the HTTP refresh endpoint.",
        dependsOn: ["shared-types"],
        paths: ["internal/api"],
        acceptance: ["The refresh endpoint rotates tokens."],
        modifying: true,
      },
      {
        id: "cli",
        title: "Update CLI token handling",
        description: "Teach the CLI to handle rotated refresh tokens.",
        dependsOn: ["shared-types"],
        paths: ["cmd"],
        acceptance: ["The CLI accepts rotated tokens."],
        modifying: true,
      },
      {
        id: "integration-tests",
        title: "Add integration tests",
        description: "Cover the complete refresh-token flow.",
        dependsOn: ["api", "cli"],
        paths: ["tests"],
        acceptance: ["Rotation and old-token rejection are covered."],
        modifying: true,
      },
    ],
  };
}

test("planner requires a non-empty task", async () => {
  await assert.rejects(
    () => executePlanner(
      { task: " ", tier: "T2" },
      async () => {
        throw new Error("should not launch");
      },
    ),
    /args\.task is required/,
  );
});

test("planner accepts only confirmed T2/T3 work", async () => {
  await assert.rejects(
    () => executePlanner(
      { task: "Rename field", tier: "T1" },
      async () => {
        throw new Error("should not launch");
      },
    ),
    /confirmed tier T2 or T3/,
  );
});

test("planner launches one fresh oracle with structured DAG schema", async () => {
  const calls = [];
  const expected = validPlan();

  const result = await executePlanner(
    {
      task: "Implement refresh token rotation",
      tier: "T2",
      riskSignals: ["api-contract"],
    },
    async (key, spec) => {
      calls.push({ key, spec });
      return { structuredOutput: expected };
    },
  );

  assert.deepEqual(result, expected);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, "planner");
  assert.equal(calls[0].spec.agent, "oracle");
  assert.equal(calls[0].spec.context, "fresh");

  const schema = calls[0].spec.outputSchema;
  assert.deepEqual(schema.required, ["summary", "tasks"]);
  assert.equal(schema.properties.tasks.minItems, 1);
  assert.equal(schema.properties.tasks.maxItems, 32);
  assert.deepEqual(
    schema.properties.tasks.items.required,
    [
      "id",
      "title",
      "description",
      "dependsOn",
      "paths",
      "acceptance",
      "modifying",
    ],
  );
});

test("planner prompt limits repository inspection to cwd and forbids mutation", async () => {
  let prompt = "";

  await executePlanner(
    {
      task: "Implement refresh token rotation",
      tier: "T3",
      riskSignals: ["security-boundary"],
    },
    async (_key, spec) => {
      prompt = spec.task;
      return { structuredOutput: validPlan() };
    },
  );

  assert.match(prompt, /current working directory is the repository root/i);
  assert.match(prompt, /inspect only files under cwd/i);
  assert.match(prompt, /never inspect parent directories/i);
  assert.match(prompt, /never use \.\. to escape cwd/i);
  assert.match(prompt, /do not modify files/i);
  assert.match(prompt, /security-boundary/);
});

test("planner rejects duplicate task ids", async () => {
  const plan = validPlan();
  plan.tasks[1].id = "shared-types";

  await assert.rejects(
    () => executePlanner(
      { task: "Complex change", tier: "T2" },
      async () => ({ structuredOutput: plan }),
    ),
    /duplicate task id: shared-types/,
  );
});

test("planner rejects unknown dependencies", async () => {
  const plan = validPlan();
  plan.tasks[1].dependsOn = ["missing-task"];

  await assert.rejects(
    () => executePlanner(
      { task: "Complex change", tier: "T2" },
      async () => ({ structuredOutput: plan }),
    ),
    /depends on unknown task missing-task/,
  );
});

test("planner rejects self-dependencies", async () => {
  const plan = validPlan();
  plan.tasks[1].dependsOn = ["api"];

  await assert.rejects(
    () => executePlanner(
      { task: "Complex change", tier: "T2" },
      async () => ({ structuredOutput: plan }),
    ),
    /task api depends on itself/,
  );
});

test("planner rejects cyclic graphs", async () => {
  const plan = validPlan();
  plan.tasks[0].dependsOn = ["integration-tests"];

  await assert.rejects(
    () => executePlanner(
      { task: "Complex change", tier: "T2" },
      async () => ({ structuredOutput: plan }),
    ),
    /cyclic task dependency graph/,
  );
});

test("planner rejects repeated dependencies even if model output bypasses schema enforcement", async () => {
  const plan = validPlan();
  plan.tasks[3].dependsOn = ["api", "api"];

  await assert.rejects(
    () => executePlanner(
      { task: "Complex change", tier: "T2" },
      async () => ({ structuredOutput: plan }),
    ),
    /repeats dependency api/,
  );
});

test("planner fails when oracle returns no structured output", async () => {
  await assert.rejects(
    () => executePlanner(
      { task: "Complex change", tier: "T2" },
      async () => ({ output: "unstructured" }),
    ),
    /planner returned no structured output/,
  );
});
