// Settings-driven SDK loading and offline protocol fixtures; no remote models or worker launches.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, copyFileSync, rmSync, symlinkSync, realpathSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { rpc, PLAN_SCHEMA, PLANNER_RESULT_SCHEMA } from "../extensions/cooperative-planner/controller.mjs";
import { POLICY } from "../extensions/cooperative-planner/scorer.mjs";

const root = resolve(import.meta.dirname, "..");
const packageRoot = join(root, "npm/node_modules/pi-subagents");
const sdkRoot = resolve(dirname(realpathSync(execFileSync("which", ["pi"], { encoding: "utf8" }).trim())), "../..");
const { DefaultResourceLoader, createAgentSession, SettingsManager, SessionManager } = await import(pathToFileURL(join(sdkRoot, "dist/index.js")).href);
const { discoverAgents } = await import(pathToFileURL(join(packageRoot, "src/agents/agents.js")).href);
const { createStructuredOutputToolParameters, resolveCompileFromPackageRoot } = await import(pathToFileURL(join(packageRoot, "src/runs/shared/structured-output.js")).href);
const { resolveCurrentSubagentCapabilityCeiling, registerSubagentCapabilityCeiling } = await import(pathToFileURL(join(packageRoot, "src/api/capability-ceiling.js")).href);
const plan = { summary: "Offline fixture", slices: [
  { id: "a", task: "A", files: ["a.txt"], dependencies: [] },
  { id: "b", task: "B", files: ["b.txt"], dependencies: [] },
  { id: "c", task: "C", files: ["c.txt"], dependencies: ["a", "b"] },
], integration: "Parent integrates", checks: ["test -f gate-ready"] };
const notParallelizable = { status: "not_parallelizable", reason: "This single-file state machine must be changed by one owner." };
const params = { issue: "Offline coding task and scout handoff", workers: 3, isolation: "none", timeout_ms: 10000 };
const scores = (value = 0.8) => new Response(JSON.stringify({ answers: Object.fromEntries(Object.keys(POLICY.criteria).map(
  key => [key, { type: "noul", noul: value }])), usage: { input_tokens: 1, output_tokens: 1 } }));

async function fixture(t, { profile = "codex", realPackage = false } = {}) {
  const directory = mkdtempSync(join(tmpdir(), "cooperative-sdk-"));
  const inherited = Object.fromEntries(Object.entries(process.env).filter(([key]) => /^(PI_SUBAGENT|PI_ASYNC_|PI_SESSION_|PI_PACKAGE_DIR|PI_CODING_AGENT_DIR$|PEPI_EVAL_OUTPUT$|TYPESAFE_API_KEY$)/.test(key)));
  for (const key of Object.keys(inherited)) delete process.env[key];
  process.env.PI_CODING_AGENT_DIR = directory;
  process.env.TYPESAFE_API_KEY = "offline-fixture";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => scores();
  let session, pi, ctx;
  t.after(async () => {
    if (session) {
      await session.extensionRunner.emit({ type: "session_shutdown", reason: "quit" });
      session.dispose();
    }
    globalThis.fetch = originalFetch;
    for (const key of Object.keys(process.env)) if (/^(PI_SUBAGENT|PI_ASYNC_|PI_SESSION_|PI_PACKAGE_DIR|PI_CODING_AGENT_DIR$|PEPI_EVAL_OUTPUT$|TYPESAFE_API_KEY$)/.test(key)) delete process.env[key];
    Object.assign(process.env, inherited);
    rmSync(directory, { recursive: true, force: true });
  });
  symlinkSync(join(root, "npm"), join(directory, "npm"));
  for (const file of ["agents/planner.md", "agents/deep-worker.md", ...["extension.ts", "controller.mjs", "scorer.mjs"].map(name => `extensions/cooperative-planner/${name}`)]) {
    mkdirSync(dirname(join(directory, file)), { recursive: true });
    copyFileSync(join(root, file), join(directory, file));
  }
  const profileSettings = JSON.parse(readFileSync(join(root, `profiles/${profile}/settings.json`)));
  assert.deepEqual(profileSettings.extensions, ["./extensions/cooperative-planner/extension.ts"]);
  assert.equal(profileSettings.cooperativePlanner, true);
  // Preserve the real profile extension settings; isolate unrelated services and use the installed package offline.
  writeFileSync(join(directory, "settings.json"), JSON.stringify({ ...profileSettings,
    packages: realPackage ? [packageRoot] : [], subagents: { intercomBridge: { mode: "off" } } }));
  const settingsManager = SettingsManager.create(directory, directory);
  assert.equal(settingsManager.getSettings().cooperativePlanner, true, "Pi 1.1 tolerates the freezer's custom flag");
  const loader = new DefaultResourceLoader({ cwd: directory, agentDir: directory, settingsManager,
    noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    extensionFactories: [api => { pi = api; api.on("session_start", (_event, context) => { ctx = context; }); }],
  }); // No --no-extensions or additionalExtensionPaths: the tracked profile entry must load the tools.
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors, []);
  ({ session } = await createAgentSession({ cwd: directory, agentDir: directory, resourceLoader: loader,
    settingsManager, sessionManager: SessionManager.inMemory(directory) }));
  const errors = [];
  session.subscribe(event => { if (event.type === "extension_error") errors.push(event.error); });
  await session.bindExtensions({});
  assert.deepEqual(errors, []);
  for (const name of ["cooperative_plan", "jev_score_plan", "cooperative_dispatch", "cooperative_checkpoint"]) assert.ok(session.getToolDefinition(name));
  assert.equal(existsSync(join(directory, "logs/jev-receipts")), false, "loading must not initialize scoring/receipts");
  const signal = new AbortController().signal;
  return { directory, pi, session, ctx,
    ceiling: () => resolveCurrentSubagentCapabilityCeiling(ctx.sessionManager.getSessionId()),
    execute: (name, input, abortSignal = signal) => session.getToolDefinition(name).execute(name, input, abortSignal, undefined, ctx).then(result => result.details),
    hook: (name, input = {}) => session.extensionRunner.emitToolCall({ type: "tool_call", toolName: name, toolCallId: "hook", input }),
    event: type => session.extensionRunner.emit({ type }),
  };
}
async function assertOrdinary(h, blocked = false, launchBlocked = blocked) {
  for (const name of ["bash", "write", "edit", "jev_route"]) assert.equal((await h.hook(name))?.block === true, blocked, name);
  for (const agent of ["scout", "worker", "deep-worker", "reviewer"]) assert.equal((await h.hook("subagent", { action: "spawn", agent }))?.block === true, launchBlocked, agent);
}
function protocol(h) {
  const state = { requests: [], launches: [], terminal: false, hold: false, result: plan };
  state.respond = request => h.pi.events.emit("prompt-template:subagent:response", { requestId: request.requestId,
    ownerRunId: request.ownerRunId, nodeId: request.nodeId, status: "completed", result: { kind: "structured", value: state.result } });
  h.pi.events.on("prompt-template:subagent:request", request => {
    assert.equal(request.agent, "planner");
    assert.equal(request.context, "fresh");
    assert.equal(request.result.kind, "structured");
    assert.deepEqual(h.ceiling().allowedAgents, ["planner"]);
    assert.deepEqual(h.ceiling().allowedTools, ["find", "grep", "ls", "read"]);
    state.requests.push(request);
    if (!state.hold) state.respond(request);
  });
  h.pi.events.on("subagents:rpc:v1:request", request => {
    let data;
    if (request.method === "spawn") {
      state.launches.push(request.params);
      assert.deepEqual(h.ceiling().allowedAgents, ["worker"]);
      data = { details: { runId: `wave-${state.launches.length}` } };
    } else {
      assert.equal(request.method, "status");
      const id = request.params.id, keys = state.launches.length === 1 ? ["a", "b"] : ["c"];
      data = state.terminal ? { details: { runId: id,
        workflowChildren: { version: 1, workflowRunId: id, inventoryComplete: true, workflowState: "completed",
          children: keys.map(key => ({ childId: key, runId: `child-${key}`, agent: "worker", state: "completed" })) },
        workflowTerminalProof: { version: 1, kind: "workflow", runId: id, state: "observed", dispatchClosed: true,
          children: keys.map(key => ({ version: 1, runId: `child-${key}`, state: "observed", runnerProcessInstanceId: key, observedAt: 1 })) },
      } } : {};
    }
    h.pi.events.emit(`subagents:rpc:v1:reply:${request.requestId}`, { ...request, success: true, data });
  });
  return state;
}

test("the parent and Codex profile route the planner to Astra", () => {
  for (const path of [join(root, "settings.json"), join(root, "profiles/codex/settings.json")]) {
    const settings = JSON.parse(readFileSync(path, "utf8"));
    assert.equal(settings.subagents.agentOverrides.planner.model, "gpt-6-astra");
  }
});

test("parent tool rendering shows only the best planner percentage", async t => {
  const h = await fixture(t);
  const tool = h.session.getToolDefinition("cooperative_plan");
  const rendered = tool.renderResult({ content: [], details: { best_score_pct: 83 } }, { isPartial: false },
    { fg: (_color, text) => text });
  assert.equal(rendered.render(40)[0].trim(), "Best: 83%");
});

for (const profile of ["codex", "bedrock"]) test(`${profile} defaults load one static planner; idle tools work and subagent launches require planning`, async t => {
  assert.equal(JSON.parse(readFileSync(join(sdkRoot, "package.json"))).version, "1.1.0");
  assert.equal(JSON.parse(readFileSync(join(packageRoot, "package.json"))).version, "0.78.0");
  const h = await fixture(t, { profile, realPackage: true });
  const discovered = discoverAgents(h.directory, "user");
  assert.deepEqual(discovered.agentDiagnostics, []);
  const planners = discovered.agents.filter(agent => agent.name === "planner");
  assert.equal(planners.length, 1);
  const [planner] = planners;
  assert.equal(planner.filePath, join(h.directory, "agents/planner.md"));
  assert.deepEqual(planner.tools, ["read", "grep", "find", "ls"]);
  assert.equal(planner.defaultContext, "fresh");
  assert.equal(planner.allowNestedSubagents, false);
  assert.equal(planner.inheritSkills, false);
  assert.equal(planner.acceptanceRole, "read-only");
  assert.deepEqual(planner.extensions ?? [], []);
  assert.deepEqual(planner.subagentOnlyExtensions ?? [], []);
  await h.execute("subagents_enable", {});
  const capabilities = async () => (await h.execute("subagent", { action: "list", options: { capabilities: true } })).agentCapabilities.agents;
  const idleCapabilities = await capabilities();
  for (const name of ["planner", "scout", "worker", "deep-worker", "reviewer"]) {
    const matches = idleCapabilities.filter(agent => agent.name === name);
    assert.equal(matches.length, 1, name);
    assert.equal(matches[0].executable, true, name);
    assert.deepEqual(matches[0].restrictionSources ?? [], []);
  }
  assert.equal(idleCapabilities.find(agent => agent.name === "planner").source, "user");
  assert.ok(!idleCapabilities.some(agent => agent.name === "cooperative-planner"));
  assert.equal(h.ceiling(), undefined);
  await assertOrdinary(h, false, true);
  assert.equal((await h.hook("subagent", { action: "list" }))?.block, undefined);
  await assert.rejects(h.execute("cooperative_dispatch", { plan_digest: "a".repeat(64) }), /No active/);
  await assert.rejects(h.execute("cooperative_checkpoint", { action: "inspect" }), /No active/);
  await assertOrdinary(h, false, true);
  // Invalid input reaches activation, then stops without launching a real planner/model.
  await assert.rejects(h.execute("cooperative_plan", { ...params, issue: "" }), /Planning stopped/);
  await assertOrdinary(h, true);
  assert.deepEqual(h.ceiling().allowedAgents, []);
  for (const agent of ["planner", "worker", "reviewer"]) await assert.rejects(rpc(h.pi, "spawn", { agent, task: "Must not launch", cwd: h.directory }), /RPC failed/);
  await h.event("session_start");
  assert.equal(h.ceiling(), undefined);
  await assertOrdinary(h, false, true);
  assert.equal((await capabilities()).find(agent => agent.name === "reviewer").executable, true);
});

test("installed structured-output validator accepts either a plan or an explicit nonparallel reason", async () => {
  const Compile = await resolveCompileFromPackageRoot(sdkRoot);
  const validator = Compile(createStructuredOutputToolParameters(PLANNER_RESULT_SCHEMA));
  assert.equal(validator.Check({ value: plan }), true);
  assert.equal(validator.Check({ value: notParallelizable }), true);
  for (const value of ["not_parallelizable", { status: "not_parallelizable" }, { ...notParallelizable, reason: " " },
    { ...notParallelizable, reason: "x".repeat(4001) }, { ...notParallelizable, accepted: true },
    { ...notParallelizable, ...plan }]) assert.equal(validator.Check({ value }), false);
  const scoreValidator = Compile(createStructuredOutputToolParameters(PLAN_SCHEMA));
  assert.equal(scoreValidator.Check({ value: notParallelizable }), false, "standalone scoring still requires a real plan");
});

test("explicit nonparallel result restores ordinary worker/deep-worker launch permissions without Jev or dispatch", async t => {
  const h = await fixture(t), state = protocol(h);
  const other = registerSubagentCapabilityCeiling({ sessionId: h.ctx.sessionManager.getSessionId(), source: "other-policy", ceiling: { denyExtensions: true } });
  t.after(() => other.dispose());
  let scoringCalls = 0;
  globalThis.fetch = async () => { scoringCalls++; throw new Error("must not call Jev"); };
  delete process.env.TYPESAFE_API_KEY;
  state.result = notParallelizable; state.hold = true;
  const pending = h.execute("cooperative_plan", params);
  await assertOrdinary(h, true);
  state.respond(state.requests[0]);
  const result = await pending;
  assert.equal(result.status, "not_parallelizable");
  assert.equal(result.reason, notParallelizable.reason);
  assert.equal(result.dispatch_authorized, false);
  for (const key of ["plan", "plan_digest", "accepted", "scores"]) assert.equal(Object.hasOwn(result, key), false);
  assert.equal(scoringCalls, 0);
  assert.equal(state.launches.length, 0);
  assert.deepEqual(h.ceiling().sources, ["other-policy"]);
  assert.equal(h.ceiling().allowedAgents, undefined, "ordinary worker/deep-worker launch is no longer ceiling-restricted");
  assert.equal(h.ceiling().allowedTools, undefined);
  await assertOrdinary(h);
  await assert.rejects(h.execute("cooperative_dispatch", { plan_digest: "a".repeat(64) }), /No active/);
  await assertOrdinary(h);
  // The ordinary fallback does not poison a later cooperative task in the same session.
  state.result = plan; state.hold = false;
  process.env.TYPESAFE_API_KEY = "offline-fixture";
  globalThis.fetch = async () => scores();
  assert.equal((await h.execute("cooperative_plan", params)).accepted, true);
  await assertOrdinary(h, true);
});

test("criterion rejection cannot unlock ordinary routing through a later nonparallel result", async t => {
  const h = await fixture(t), state = protocol(h);
  const abort = new AbortController();
  let scoringCalls = 0;
  globalThis.fetch = async () => { scoringCalls++; state.hold = true; return scores(0.7); };
  const pending = h.execute("cooperative_plan", params, abort.signal);
  const stopped = assert.rejects(pending, /Planning stopped/);
  for (let i = 0; state.requests.length < 2 && i < 100; i++) await delay(5);
  assert.equal(state.requests.length, 2);
  state.result = notParallelizable;
  state.respond(state.requests[1]);
  for (let i = 0; state.requests.length < 3 && i < 100; i++) await delay(5);
  assert.equal(state.requests.length, 3, "must request another revision, not return a fallback");
  assert.equal(scoringCalls, 1);
  assert.equal(state.launches.length, 0);
  await assertOrdinary(h, true);
  abort.abort(); await stopped;
  await assertOrdinary(h, true);
});

test("standalone scoring and denied idle dispatch never activate the gate, including scoring failures", async t => {
  const h = await fixture(t);
  const result = await h.execute("jev_score_plan", { issue: params.issue, plan });
  assert.equal(result.accepted, true);
  await assert.rejects(h.execute("cooperative_dispatch", { plan_digest: result.plan_digest }), /No active/);
  globalThis.fetch = async () => { throw new Error("offline outage"); };
  await assert.rejects(h.execute("jev_score_plan", { issue: params.issue, plan }), /Scoring stopped/);
  assert.equal(h.ceiling(), undefined);
  await assertOrdinary(h, false, true);
  for (const name of ["session_before_tree", "session_before_fork", "session_before_switch"]) await h.event(name);
  assert.equal(h.ceiling(), undefined);
  await assertOrdinary(h, false, true);
});

test("static planner revisions, parent dispatch, checkpoint barriers and final cleanup restore ordinary roles", async t => {
  const h = await fixture(t), state = protocol(h);
  const other = registerSubagentCapabilityCeiling({ sessionId: h.ctx.sessionManager.getSessionId(), source: "other-policy", ceiling: { denyExtensions: true } });
  t.after(() => other.dispose());
  let scoringCalls = 0;
  globalThis.fetch = async () => { assert.deepEqual(h.ceiling().allowedAgents, []); return scores(++scoringCalls === 1 ? 0.7 : 0.8); };
  state.hold = true;
  const planning = h.execute("cooperative_plan", params);
  assert.deepEqual(h.ceiling().allowedAgents, ["planner"], "gate must engage before first await");
  await assertOrdinary(h, true);
  state.hold = false; state.respond(state.requests[0]);
  const approved = await planning;
  assert.equal(state.requests.length, 2);
  assert.ok(state.requests[1].task.includes('"required_threshold":0.8'));
  assert.ok(state.requests[1].task.includes('"task_coverage":0.7'));
  assert.equal(state.launches.length, 0, "acceptance is not parent dispatch");
  assert.deepEqual(h.ceiling().allowedAgents, []);
  await assertOrdinary(h, true);
  await h.execute("cooperative_dispatch", { plan_digest: approved.plan_digest });
  await assertOrdinary(h, true);
  assert.equal((await h.execute("cooperative_checkpoint", { action: "inspect" })).ready, false);
  await assertOrdinary(h, true);
  state.terminal = true;
  assert.equal((await h.execute("cooperative_checkpoint", { action: "inspect" })).ready, true);
  assert.equal((await h.hook("write"))?.block, undefined, "parent integration is allowed at terminal checkpoint");
  assert.equal((await h.hook("subagent", { action: "spawn", agent: "reviewer" }))?.block, true);
  assert.equal((await h.execute("cooperative_checkpoint", { action: "verify" })).verified, false);
  assert.deepEqual(h.ceiling().allowedAgents, []);
  writeFileSync(join(h.directory, "gate-ready"), "parent integration\n");
  assert.equal((await h.execute("cooperative_checkpoint", { action: "verify" })).complete, false);
  assert.equal((await h.hook("subagent", { action: "spawn", agent: "worker" }))?.block, true);
  await h.execute("cooperative_dispatch", { plan_digest: approved.plan_digest });
  assert.equal(state.launches.length, 2);
  await assertOrdinary(h, true);
  await h.execute("cooperative_checkpoint", { action: "inspect" });
  assert.equal((await h.execute("cooperative_checkpoint", { action: "verify" })).complete, true);
  assert.deepEqual(h.ceiling().sources, ["other-policy"], "release must preserve unrelated capability ceilings");
  await assertOrdinary(h);
  await assert.rejects(h.execute("cooperative_dispatch", { plan_digest: approved.plan_digest }), /No active/);
  await assertOrdinary(h);
  await h.execute("cooperative_plan", params);
  await assertOrdinary(h, true);
  await h.event("session_shutdown");
  assert.deepEqual(h.ceiling().sources, ["other-policy"]);
  await assertOrdinary(h);
});

test("initialization failure and canceled navigation stay closed until session reset", async t => {
  const h = await fixture(t);
  const unsafe = join(h.directory, "unsafe"); symlinkSync(h.directory, unsafe);
  process.env.PEPI_EVAL_OUTPUT = unsafe;
  await assert.rejects(h.execute("cooperative_plan", params), /Unsafe receipt/);
  assert.deepEqual(h.ceiling().allowedAgents, []);
  await assertOrdinary(h, true);
  delete process.env.PEPI_EVAL_OUTPUT;
  await assert.rejects(h.execute("cooperative_plan", params), /No active/);
  await h.event("session_start");
  const state = protocol(h);
  for (const event of ["session_before_tree", "session_before_fork", "session_before_switch"]) {
    const approved = await h.execute("cooperative_plan", params);
    await h.event(event);
    await assertOrdinary(h, true);
    await assert.rejects(h.execute("cooperative_dispatch", { plan_digest: approved.plan_digest }), /Controller unavailable/);
    assert.equal(state.launches.length, 0);
    await h.event("session_start");
    assert.equal(h.ceiling(), undefined);
    await assertOrdinary(h, false, true);
  }
});

test("session replacement cleans up in-flight planning without late locks or disposed-handle errors", async t => {
  const h = await fixture(t), state = protocol(h);
  state.hold = true;
  const stopped = assert.rejects(h.execute("cooperative_plan", params), /Planning stopped/);
  await h.event("session_start");
  await stopped;
  assert.equal(h.ceiling(), undefined);
  await assertOrdinary(h, false, true);
  state.hold = false;
  const approved = await h.execute("cooperative_plan", params);
  assert.equal(approved.accepted, true);
  await assertOrdinary(h, true);
});

test("active cancellation, deadline and score-only invalidation never release approval or restrictions", async t => {
  const h = await fixture(t), state = protocol(h);
  state.hold = true;
  const abort = new AbortController();
  const cancelled = h.execute("cooperative_plan", params, abort.signal);
  abort.abort();
  await assert.rejects(cancelled, /Planning stopped/);
  await assertOrdinary(h, true);
  assert.deepEqual(h.ceiling().allowedAgents, []);
  await h.event("session_start");
  const expired = assert.rejects(h.execute("cooperative_plan", { ...params, timeout_ms: 1000 }), /Planning stopped/);
  await delay(1100); await expired;
  await assertOrdinary(h, true);
  await h.event("session_start");
  state.hold = false;
  const approved = await h.execute("cooperative_plan", params);
  await h.execute("jev_score_plan", { issue: params.issue, plan });
  await assertOrdinary(h, true);
  await assert.rejects(h.execute("cooperative_dispatch", { plan_digest: approved.plan_digest }), /Dispatch denied/);
  assert.equal(state.launches.length, 0);
  await assertOrdinary(h, true);
});
