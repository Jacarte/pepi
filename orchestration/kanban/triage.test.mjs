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

test("triage requires a non-empty task", async () => {
  await assert.rejects(
    () => executeTriage({ task: "   " }, async () => {
      throw new Error("should not launch");
    }),
    /args\.task is required/,
  );
});

test("triage delegates exactly once to fresh oracle with structured output", async () => {
  const calls = [];
  const expected = {
    tier: "T2",
    confidence: "medium",
    needsScout: true,
    reason: "The request sounds coordinated but repository scope is not established.",
    riskSignals: [],
  };

  const result = await executeTriage(
    { task: "Add refresh token rotation" },
    async (key, spec) => {
      calls.push({ key, spec });
      return { structuredOutput: expected };
    },
  );

  assert.deepEqual(result, expected);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, "triage");
  assert.equal(calls[0].spec.agent, "oracle");
  assert.equal(calls[0].spec.context, "fresh");

  assert.deepEqual(
    calls[0].spec.outputSchema.properties.tier.enum,
    ["T0", "T1", "T1R", "T2", "T3"],
  );
  assert.deepEqual(
    calls[0].spec.outputSchema.required,
    ["tier", "confidence", "needsScout", "reason", "riskSignals"],
  );
});

test("triage prompt is request-only and routes repository uncertainty to scout", async () => {
  let prompt = "";

  await executeTriage(
    { task: "Rename the authentication config field" },
    async (_key, spec) => {
      prompt = spec.task;
      return {
        structuredOutput: {
          tier: "T1",
          confidence: "medium",
          needsScout: true,
          reason: "Repository impact is unknown.",
          riskSignals: [],
        },
      };
    },
  );

  assert.match(prompt, /Do NOT inspect the repository, filesystem, shell/i);
  assert.match(prompt, /needsScout=true instead of searching/i);
  assert.match(prompt, /uncertainty about code location.*NOT.*reason to promote/i);
  assert.match(prompt, /Rename the authentication config field/);
});

test("triage preserves T1R classification", async () => {
  const result = await executeTriage(
    { task: "Fix the failing go test ./internal/session -run TestCleanup" },
    async () => ({
      structuredOutput: {
        tier: "T1R",
        confidence: "high",
        needsScout: false,
        reason: "The request explicitly asks to repair an existing failing test.",
        riskSignals: [],
      },
    }),
  );

  assert.equal(result.tier, "T1R");
  assert.equal(result.needsScout, false);
});

test("triage fails when oracle returns no structured output", async () => {
  await assert.rejects(
    () => executeTriage(
      { task: "Implement a feature" },
      async () => ({ output: "unstructured" }),
    ),
    /triage returned no structured output/,
  );
});
