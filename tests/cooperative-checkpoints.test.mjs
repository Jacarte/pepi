// Real pi-subagents lifecycle, with deterministic loopback model responses only. No remote model calls.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, realpathSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { pathToFileURL } from "node:url";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { setTimeout as delay } from "node:timers/promises";
import { Controller, terminalWave, rpc } from "../extensions/cooperative-planner/controller.mjs";
import { scorePlan, POLICY } from "../extensions/cooperative-planner/scorer.mjs";

const root = resolve(import.meta.dirname, "..");
const plan = { summary: "Offline lifecycle fixture", slices: [
  { id: "a", task: "Return fixture completion A", files: ["a.txt"], dependencies: [] },
  { id: "b", task: "Return fixture completion B", files: ["b.txt"], dependencies: [] },
  { id: "c", task: "Return fixture completion C after A and B", files: ["c.txt"], dependencies: ["a", "b"] },
], integration: "Parent checkpoint fixture", checks: ["test -f gate-ready"] };

async function waitForCheckpoint(controller, ctx, signal) {
  const deadline = Date.now() + 30000;
  do {
    const result = await controller.checkpoint("inspect", ctx, signal);
    if (result.ready) return result;
    await delay(100);
  } while (Date.now() < deadline);
  const status = await rpc(controller.pi, "status", { id: controller.activeRun });
  assert.fail(`Package-produced successful terminal evidence did not arrive: ${status.text?.slice(0, 2500)}`);
}

test("real package dispatch, terminal proofs and parent checks release exactly the next dependency wave", { timeout: 60000 }, async t => {
  const directory = mkdtempSync(join(tmpdir(), "cooperative-checkpoint-"));
  const inherited = Object.fromEntries(Object.entries(process.env).filter(([key]) => /^(PI_SUBAGENT|PI_ASYNC_|PI_SESSION_|PI_PACKAGE_DIR|PI_CODING_AGENT_DIR$|PEPI_EVAL_OUTPUT$)/.test(key)));
  for (const key of Object.keys(inherited)) delete process.env[key];
  process.env.PI_CODING_AGENT_DIR = directory;
  symlinkSync(join(root, "npm"), join(directory, "npm"));
  let session, controller, ceiling, pi, ctx;
  let modelCalls = 0;
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    assert.equal(request.url, "/v1/chat/completions");
    assert.equal(JSON.parse(body).model, "fixture-model");
    modelCalls++;
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = { id: `fixture-${modelCalls}`, object: "chat.completion.chunk", created: 1, model: "fixture-model" };
    response.write(`data: ${JSON.stringify({ ...chunk, choices: [{ index: 0, delta: { role: "assistant", content: "Offline fixture complete." }, finish_reason: null }] })}\n\n`);
    response.write(`data: ${JSON.stringify({ ...chunk, choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } })}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  await new Promise(resolvePromise => server.listen(0, "127.0.0.1", resolvePromise));
  try {
    const bin = realpathSync(execFileSync("which", ["pi"], { encoding: "utf8" }).trim());
    const sdkRoot = resolve(dirname(bin), "../..");
    // SDK tests run under node, not the Pi CLI; identify the real installed runner host explicitly.
    process.env.PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT = sdkRoot;
    assert.equal(JSON.parse(readFileSync(join(sdkRoot, "package.json"))).version, "1.1.0");
    assert.equal(JSON.parse(readFileSync(join(root, "npm/node_modules/pi-subagents/package.json"))).version, "0.78.0");
    const settings = { defaultProvider: "cooperative-test", defaultModel: "fixture-model", defaultThinkingLevel: "off",
      subagents: { defaultProvider: "cooperative-test", defaultModel: "cooperative-test/fixture-model", defaultThinking: "off", intercomBridge: { mode: "off" } } };
    writeFileSync(join(directory, "settings.json"), JSON.stringify(settings));
    writeFileSync(join(directory, "models.json"), JSON.stringify({ providers: { "cooperative-test": {
      api: "openai-completions", baseUrl: `http://127.0.0.1:${server.address().port}/v1`, apiKey: "offline-fixture",
      models: [{ id: "fixture-model", reasoning: false, input: ["text"], contextWindow: 8192, maxTokens: 1024,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    } } }));
    mkdirSync(join(directory, "agents"));
    writeFileSync(join(directory, "agents/worker.md"), `---
name: worker
description: Offline lifecycle fixture only
tools: read
thinking: off
systemPromptMode: replace
inheritProjectContext: false
inheritGlobalContext: false
inheritSkills: false
defaultProgress: false
acceptance: {"level":"none","reason":"offline lifecycle fixture"}
---
Return the fixed offline fixture response. Do not use tools or edit files.
`);
    const { DefaultResourceLoader, createAgentSession, SettingsManager, SessionManager } = await import(pathToFileURL(join(sdkRoot, "dist/index.js")).href);
    const settingsManager = SettingsManager.inMemory({ ...settings, packages: [] });
    const loader = new DefaultResourceLoader({ cwd: directory, agentDir: directory, settingsManager,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      additionalExtensionPaths: [join(root, "npm/node_modules/pi-subagents/index.js")],
      extensionFactories: [api => { pi = api; api.on("session_start", (_event, context) => { ctx = context; }); }],
    });
    await loader.reload();
    assert.deepEqual(loader.getExtensions().errors, []);
    ({ session } = await createAgentSession({ cwd: directory, agentDir: directory, resourceLoader: loader,
      settingsManager, sessionManager: SessionManager.create(directory, join(directory, "sessions")) }));
    await session.bindExtensions({});
    const require = createRequire(join(directory, "npm/package.json"));
    const { registerSubagentCapabilityCeiling } = await import(pathToFileURL(require.resolve("pi-subagents/capability-ceiling")).href);
    ceiling = registerSubagentCapabilityCeiling({ sessionId: ctx.sessionManager.getSessionId(), source: "checkpoint-test", ceiling: { allowedAgents: [] } });
    const evidence = [], dispatches = [], scoringRequests = [];
    controller = new Controller({ pi, ceiling, sessionId: ctx.sessionManager.getSessionId(), receipt() {},
      launchPlanner: async () => plan,
      score: args => scorePlan({ ...args, apiKey: "offline-scorer", fetchImpl: async (_url, options) => {
        scoringRequests.push(JSON.parse(options.body).state);
        return new Response(JSON.stringify({ answers: Object.fromEntries(Object.keys(POLICY.criteria).map(key => [key, { type: "noul", noul: 0.9 }])),
          usage: { input_tokens: 1, output_tokens: 0 } }));
      } }),
      callRpc: async (api, method, params) => {
        if (method === "spawn") dispatches.push(params);
        const result = await rpc(api, method, params);
        if (method === "status") evidence.push(structuredClone(result));
        return result;
      },
    });
    const signal = new AbortController().signal;
    const approved = await controller.plan({ issue: "Offline lifecycle test", workers: 3, isolation: "auto", timeout_ms: 50000 }, ctx, signal, "score");
    assert.equal(approved.isolation, "none");
    assert.deepEqual(scoringRequests[0].execution, { isolation: "none", worker_count: 3 });
    const first = await controller.dispatch(approved.plan_digest, ctx, signal);
    assert.equal((await controller.checkpoint("inspect", ctx, signal)).ready, false);
    assert.equal(controller.checkpointReady, false);
    await waitForCheckpoint(controller, ctx, signal);
    assert.equal(dispatches.length, 1);
    assert.equal((await controller.checkpoint("verify", ctx, signal)).verified, false);
    assert.equal(controller.wave, 0);
    writeFileSync(join(directory, "gate-ready"), "parent integration fixture\n");
    assert.equal((await controller.checkpoint("verify", ctx, signal)).verified, true);
    assert.equal(controller.wave, 1);
    const canonical = evidence.find(value => terminalWave(value, first.run_id, ["a", "b"]));
    assert.ok(canonical, "must use real package-produced targeted status/proof");
    assert.equal(canonical.details.workflowTerminalProof.dispatchClosed, true);
    assert.equal(canonical.details.workflowTerminalProof.children.length, 2);
    assert.equal(canonical.asyncSnapshot.runs.find(row => row.id === first.run_id)?.steps, undefined,
      "display projection is not a canonical child/proof source");

    await t.test("missing, partial, failed, duplicate and wrong-identity proofs never release the barrier", () => {
      const mutate = change => { const value = structuredClone(canonical); change(value.details); return value; };
      const invalid = [
        {}, { asyncSnapshot: canonical.asyncSnapshot },
        mutate(value => { value.runId = "other"; }),
        mutate(value => { value.workflowChildren.workflowRunId = "other"; }),
        mutate(value => { value.workflowChildren.inventoryComplete = false; }),
        mutate(value => { value.workflowChildren.workflowState = "failed"; }),
        mutate(value => { value.workflowChildren.children[0].state = "failed"; }),
        mutate(value => { value.workflowChildren.children[0].agent = "other"; }),
        mutate(value => { value.workflowChildren.children[0].childId = "other"; }),
        mutate(value => { value.workflowChildren.children[1] = value.workflowChildren.children[0]; }),
        mutate(value => { value.workflowChildren.children[1].runId = value.workflowChildren.children[0].runId; }),
        mutate(value => { value.workflowTerminalProof.state = "unknown"; }),
        mutate(value => { value.workflowTerminalProof.runId = "other"; }),
        mutate(value => { value.workflowTerminalProof.dispatchClosed = false; }),
        mutate(value => { value.workflowTerminalProof.children.pop(); }),
        mutate(value => { value.workflowTerminalProof.children[0].state = "unknown"; }),
        mutate(value => { value.workflowTerminalProof.children[0].runId = "other"; }),
        mutate(value => { value.workflowTerminalProof.children[1] = value.workflowTerminalProof.children[0]; }),
        mutate(value => { delete value.workflowTerminalProof.children[0].runnerProcessInstanceId; }),
      ];
      for (const value of invalid) assert.equal(terminalWave(value, first.run_id, ["a", "b"]), false);
      assert.equal(terminalWave(canonical, first.run_id, ["a", "a"]), false);
    });

    await controller.dispatch(approved.plan_digest, ctx, signal);
    assert.equal(dispatches.length, 2);
    assert.ok(dispatches[1].script.includes('[["c"]]'));
    await waitForCheckpoint(controller, ctx, signal);
    assert.equal((await controller.checkpoint("verify", ctx, signal)).complete, true);
    assert.ok(modelCalls >= 3); // Completion notifications can also wake the parent on the same loopback fixture.
    await assert.rejects(controller.dispatch(approved.plan_digest, ctx, signal));
  } finally {
    if (controller?.activeRun && controller.activeRun !== "launch-pending")
      await rpc(pi, "stop", { id: controller.activeRun }).catch(() => {});
    controller?.stop(); ceiling?.dispose();
    if (session) {
      await session.abort();
      await session.extensionRunner.emit({ type: "session_shutdown", reason: "quit" });
      session.dispose();
    }
    server.closeAllConnections();
    await new Promise(resolvePromise => server.close(resolvePromise));
    delete process.env.PI_CODING_AGENT_DIR;
    delete process.env.PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT;
    Object.assign(process.env, inherited);
    rmSync(directory, { recursive: true, force: true });
  }
});
