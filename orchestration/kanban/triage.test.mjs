import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workflowPath = path.resolve(__dirname, "../../workflows/triage.ts");
const workflowSource = fs.readFileSync(workflowPath, "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

async function executeTriage(args, run) {
  const workflow = new AsyncFunction("args", "runs", workflowSource);
  return workflow(args, { run });
}

function classification(overrides = {}) {
  return {
    tier: "T1",
    confidence: "high",
    needsScout: false,
    reason: "The request is a bounded reversible change.",
    riskSignals: [],
    ...overrides,
  };
}

test("triage requires a non-empty task", async () => {
  await assert.rejects(
    () => executeTriage({ task: "   " }, async () => {
      throw new Error("should not launch");
    }),
    /args\.task is required/,
  );
});

test("triage returns the initial classification directly when scout is unnecessary", async () => {
  const calls = [];
  const initial = classification({
    tier: "T1R",
    reason: "The request explicitly asks to repair an existing failing test.",
  });

  const result = await executeTriage(
    { task: "Fix the failing go test ./internal/session -run TestCleanup" },
    async (key, spec) => {
      calls.push({ key, spec });
      return { structuredOutput: initial };
    },
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, "triage");
  assert.equal(calls[0].spec.agent, "oracle");
  assert.equal(calls[0].spec.context, "fresh");
  assert.deepEqual(result, {
    initial,
    confirmed: initial,
    scoutUsed: false,
  });
});

test("request-only triage still forbids repository inspection", async () => {
  let prompt = "";

  await executeTriage(
    { task: "Rename the authentication config field" },
    async (_key, spec) => {
      prompt = spec.task;
      return {
        structuredOutput: classification({
          confidence: "medium",
          needsScout: false,
          reason: "The request appears bounded from its wording.",
        }),
      };
    },
  );

  assert.match(prompt, /Do NOT inspect the repository, filesystem, shell/i);
  assert.match(prompt, /needsScout=true instead of searching/i);
  assert.match(prompt, /uncertainty about code location.*NOT.*reason to promote/i);
});

test("needsScout triggers bounded scout and fresh confirmation", async () => {
  const calls = [];
  const initial = classification({
    confidence: "medium",
    needsScout: true,
    reason: "Repository impact is not established from the request.",
  });
  const confirmed = classification({
    tier: "T2",
    confidence: "high",
    needsScout: false,
    reason: "The config field is part of a public compatibility contract.",
    riskSignals: ["api-contract"],
  });

  const result = await executeTriage(
    { task: "Rename the authentication config field" },
    async (key, spec) => {
      calls.push({ key, spec });

      if (key === "triage") {
        return { structuredOutput: initial };
      }

      if (key === "triage-scout") {
        return {
          output: "config/auth.ts exports the field through the public config schema.",
        };
      }

      if (key === "triage-confirm") {
        return { structuredOutput: confirmed };
      }

      throw new Error(`unexpected key: ${key}`);
    },
  );

  assert.deepEqual(
    calls.map((call) => call.key),
    ["triage", "triage-scout", "triage-confirm"],
  );

  assert.equal(calls[1].spec.agent, "scout");
  assert.equal(calls[1].spec.context, "fresh");
  assert.match(calls[1].spec.task, /current working directory is the repository root/i);
  assert.match(calls[1].spec.task, /never inspect parent directories/i);
  assert.match(calls[1].spec.task, /never inspect \$HOME or ~/i);
  assert.match(calls[1].spec.task, /never use \.\. to escape/i);
  assert.match(calls[1].spec.task, /do not modify application, test, configuration, documentation, or VCS files/i);

  assert.equal(calls[2].spec.agent, "oracle");
  assert.equal(calls[2].spec.context, "fresh");
  assert.match(calls[2].spec.task, /Use only the evidence below/i);
  assert.match(calls[2].spec.task, /config\/auth\.ts exports the field/i);
  assert.match(calls[2].spec.task, /Promote only when repository evidence demonstrates/i);
  assert.match(calls[2].spec.task, /Demote when repository evidence proves/i);

  assert.deepEqual(result, {
    initial,
    confirmed,
    scoutUsed: true,
  });
});

test("confirmation uses the same structured classification schema", async () => {
  const schemas = [];

  await executeTriage(
    { task: "Change authentication behavior" },
    async (key, spec) => {
      if (spec.outputSchema) {
        schemas.push({ key, schema: spec.outputSchema });
      }

      if (key === "triage") {
        return {
          structuredOutput: classification({
            needsScout: true,
            confidence: "low",
            reason: "Repository evidence is required.",
          }),
        };
      }

      if (key === "triage-scout") {
        return { output: "The change is local to internal/auth/session.go." };
      }

      return {
        structuredOutput: classification({
          tier: "T1",
          confidence: "high",
          needsScout: false,
          reason: "Repository evidence confirms a local implementation boundary.",
        }),
      };
    },
  );

  assert.equal(schemas.length, 2);
  assert.deepEqual(
    schemas[0].schema.properties.tier.enum,
    ["T0", "T1", "T1R", "T2", "T3"],
  );
  assert.deepEqual(schemas[1].schema, schemas[0].schema);
});

test("triage fails when initial oracle returns no structured output", async () => {
  await assert.rejects(
    () => executeTriage(
      { task: "Implement a feature" },
      async () => ({ output: "unstructured" }),
    ),
    /triage returned no structured output/,
  );
});

test("triage fails when scout returns no evidence", async () => {
  await assert.rejects(
    () => executeTriage(
      { task: "Implement a feature" },
      async (key) => {
        if (key === "triage") {
          return {
            structuredOutput: classification({
              needsScout: true,
              confidence: "low",
              reason: "Repository evidence is required.",
            }),
          };
        }

        return { output: "   " };
      },
    ),
    /triage scout returned no evidence/,
  );
});

test("triage fails when confirmation returns no structured output", async () => {
  await assert.rejects(
    () => executeTriage(
      { task: "Implement a feature" },
      async (key) => {
        if (key === "triage") {
          return {
            structuredOutput: classification({
              needsScout: true,
              confidence: "low",
              reason: "Repository evidence is required.",
            }),
          };
        }

        if (key === "triage-scout") {
          return { output: "Repository evidence." };
        }

        return { output: "unstructured" };
      },
    ),
    /triage confirmation returned no structured output/,
  );
});
