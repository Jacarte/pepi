import assert from "node:assert/strict";
import { stripTypeScriptTypes } from "node:module";
import { readFileSync } from "node:fs";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { test } from "node:test";

const root = fileURLToPath(new URL("../", import.meta.url));
const source = readFileSync(`${root}extensions/pi-typesafe-router.ts`, "utf8");
const targets = JSON.parse(readFileSync(`${root}models-router.json`, "utf8"));

// Evaluate the real extension with isolated environment, classifier and logging.
// No provider requests, global environment changes or writes to router.log.
function setup(child = false, scores = { human: 0, hard: 0.8, trivial: 0, routine: 0 }) {
  let input: any;
  let fetches = 0;
  const selected: any[] = [];
  const js = stripTypeScriptTypes(source)
    .replace('import fs from "fs";', "const fs = fakeFs;")
    .replace("export default function", "function register");
  const register = vm.runInNewContext(`${js}\nregister;`, {
    __dirname: `${root}extensions`,
    fakeFs: { ...fs, createWriteStream: () => ({ write() {} }) },
    process: { env: { PI_SUBAGENT_CHILD: child ? "1" : undefined, TYPESAFE_API_KEY: "offline-test" } },
    AbortSignal,
    console,
    fetch: async () => {
      fetches++;
      return { ok: true, json: async () => ({ answers: Object.fromEntries(
        Object.entries(scores).map(([name, noul]) => [name, { noul }]),
      ) }) };
    },
  });
  register({
    on: (name: string, handler: any) => { assert.equal(name, "input"); input = handler; },
    setModel: async (model: any) => { selected.push(model); return true; },
  });
  const ctx = { isIdle: () => true, hasUI: false, modelRegistry: {
    find: (provider: string, id: string) => ({ provider, id }),
  } };
  return { input, selected, ctx, fetches: () => fetches };
}

test("background child registers no routing hook and never dispatches", () => {
  const child = setup(true);
  assert.equal(child.input, undefined);
  assert.equal(child.fetches(), 0);
  assert.deepEqual(child.selected, []);
});

test("parent still classifies and selects configured frontier", async () => {
  const parent = setup();
  assert.ok(parent.input);
  const result = await parent.input({ text: "diagnose a difficult failure" }, parent.ctx);
  assert.equal(result.action, "continue");
  assert.equal(parent.fetches(), 1);
  assert.deepEqual(parent.selected, [targets.FRONTIER]);
});

test("busy parent does not reroute", async () => {
  const parent = setup();
  const result = await parent.input({ text: "queued task" }, { ...parent.ctx, isIdle: () => false });
  assert.equal(result.action, "continue");
  assert.equal(parent.fetches(), 0);
  assert.deepEqual(parent.selected, []);
});

test("parent image and slash input retain default bypass", async () => {
  for (const event of [{ text: "/visual-explainer" }, { text: "describe", images: [{}] }]) {
    const parent = setup();
    await parent.input(event, parent.ctx);
    assert.equal(parent.fetches(), 0);
    assert.deepEqual(parent.selected, [targets.CHEAP]);
  }
});

test("settings restore physical defaults and roles without child-only router", () => {
  const settings = JSON.parse(readFileSync(`${root}settings.json`, "utf8"));
  assert.equal(settings.defaultModel, "gpt-6-luna");
  assert.equal(settings.subagents.defaultModel, "gpt-6.1-sol");
  const expected = { oracle: "gpt-6-astra", delegate: "gpt-6-luna", researcher: "gpt-6.1-sol",
    worker: "gpt-6-luna", scout: "gpt-6-luna", reviewer: "gpt-6.1-sol" };
  for (const [role, model] of Object.entries(expected)) {
    assert.equal(settings.subagents.agentOverrides[role].model, model);
  }
  assert.equal(settings.subagents.defaultSubagentOnlyExtensions, undefined);
  assert.ok(!JSON.stringify(settings).includes("typesafe-auto"));
  assert.ok(!source.includes("registerVirtualModel"));
});
