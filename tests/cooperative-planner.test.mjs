import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { EventEmitter } from "node:events";
import { POLICY, POLICY_DIGEST, scorePlan, validateResponse, bindingDigest } from "../extensions/cooperative-planner/scorer.mjs";
import { Controller, delegate, rpc, validatePlan, READ_TOOLS, PLAN_SCHEMA, PLANNER_RESULT_SCHEMA } from "../extensions/cooperative-planner/controller.mjs";

const plan = () => ({ summary: "Fix issue", slices: [
  { id: "logic", task: "Fix behavior", files: ["src/a.js"], dependencies: [] },
  { id: "tests", task: "Test contract", files: ["test/a.js"], dependencies: [] },
], integration: "Inspect combined patch", checks: ["npm test"] });
const notParallelizable = { status: "not_parallelizable", reason: "The single-file state transition and its ordering cannot have independent owners." };
const response = (value = 0.8) => ({ answers: Object.fromEntries(Object.keys(POLICY.criteria).map(
  key => [key, { type: "noul", noul: value }])), usage: { input_tokens: 123, output_tokens: 4 } });
const scoring = (data = response(), extras = {}) => scorePlan({ issue: "SECRET ISSUE", plan: plan(), execution: {},
  signal: new AbortController().signal, sessionId: "s", toolCallId: "t", attempt: 1,
  apiKey: "SECRET-KEY", receipt() {}, fetchImpl: async () => new Response(JSON.stringify(data)), ...extras });

test("immutable all-criteria conjunction including exact threshold and every single failure", () => {
  assert.ok(Object.isFrozen(POLICY) && Object.isFrozen(POLICY.criteria));
  assert.equal(validateResponse(response()).accepted, true);
  for (const key of Object.keys(POLICY.criteria)) {
    const data = response(); data.answers[key].noul = 0.79999;
    assert.equal(validateResponse(data).accepted, false);
  }
});

test("reject missing, unknown, malformed, empty and nonfinite results/usage", () => {
  const bad = [null, {}, { answers: {} }, { ...response(), usage: {} }];
  for (const value of [null, true, "0.9", -1, 1.01, Infinity, NaN]) {
    const data = response(); data.answers.task_coverage.noul = value; bad.push(data);
  }
  let data = response(); delete data.answers.task_coverage; bad.push(data);
  data = response(); data.answers.extra = { type: "noul", noul: 1 }; bad.push(data);
  data = response(); data.answers.task_coverage.extra = 1; bad.push(data);
  data = response(); data.usage.input_tokens = true; bad.push(data);
  for (const item of bad) assert.throws(() => validateResponse(item), /invalid_response/);
});

test("Noul answers require exactly the documented discriminator and score, in either key order", () => {
  for (const key of Object.keys(POLICY.criteria)) {
    const valid = response(); valid.answers[key] = { noul: 1, type: "noul" };
    assert.equal(validateResponse(valid).accepted, true);
    const missingType = response(); delete missingType.answers[key].type;
    assert.throws(() => validateResponse(missingType), /invalid_response/);
    const missingScore = response(); delete missingScore.answers[key].noul;
    assert.throws(() => validateResponse(missingScore), /invalid_response/);
    for (const type of ["bool", "Noul", "", null, true, 1, [], {}]) {
      const data = response(); data.answers[key].type = type;
      assert.throws(() => validateResponse(data), /invalid_response/);
    }
  }
});

test("missing or wrong provider answer types fail closed at the controller gate", async () => {
  for (const wrongType of [false, true]) {
    const data = response();
    if (wrongType) data.answers.task_coverage.type = "bool";
    else delete data.answers.task_coverage.type;
    const receipts = [];
    const h = harness({ score: args => scorePlan({ ...args, apiKey: "offline-key",
      fetchImpl: async () => new Response(JSON.stringify(data)), receipt: row => receipts.push(row) }) });
    await assert.rejects(h.controller.plan(h.params, h.ctx, h.signal, "tool"), /Planning stopped/);
    assert.equal(receipts.at(-1).outcome, "invalid_response");
    assert.equal(receipts.at(-1).usage, null);
    assert.equal(h.controller.approval, null);
    assert.deepEqual(h.ceiling.current.allowedAgents, []);
    await assert.rejects(h.controller.dispatch("a".repeat(64), h.ctx, h.signal));
    assert.ok(!h.order.includes("dispatch"));
  }
});

test("HTTP request contains explicit data and immutable questions; receipts contain no payload/secrets", async () => {
  const receipts = [];
  const result = await scoring(response(), { receipt: row => receipts.push(row), fetchImpl: async (url, options) => {
    assert.equal(url, "https://api.typesafe.ai/v1/systemone");
    assert.equal(options.headers.Authorization, "Bearer SECRET-KEY");
    const body = JSON.parse(options.body);
    assert.deepEqual(Object.keys(body.state).sort(), ["execution", "issue", "plan"]);
    assert.deepEqual(body.state.execution, { isolation: "unspecified", worker_count: 2 });
    assert.deepEqual(Object.keys(body.questions), Object.keys(POLICY.criteria));
    return new Response(JSON.stringify({ ...response(), private_metadata: "SECRET RESPONSE" }));
  } });
  assert.equal(result.accepted, true);
  assert.equal(result.cost_usd, null);
  assert.equal(receipts.length, 2);
  assert.equal(receipts[0].receipt_id, receipts[1].receipt_id);
  assert.equal(receipts[0].phase, "started");
  assert.equal(receipts[1].outcome, "accepted");
  assert.deepEqual(receipts[1].scores, Object.fromEntries(Object.keys(POLICY.criteria).map(key => [key, 0.8])));
  assert.ok(!JSON.stringify(receipts).includes("SECRET"));
});

test("resolved isolation reaches Jev without local paths or arbitrary execution fields", async () => {
  for (const isolation of ["none", "worktree"]) {
    const execution = { isolation, worker_count: 2, cwd: "SECRET PATH", deadline: Date.now() + 5000, other: "SECRET EXTRA" };
    let sent;
    const result = await scoring(response(), { execution, fetchImpl: async (_url, options) => {
      sent = JSON.parse(options.body).state.execution;
      return new Response(JSON.stringify(response()));
    } });
    assert.deepEqual(sent, { isolation, worker_count: 2 });
    assert.equal(result.plan_digest, bindingDigest("SECRET ISSUE", plan(), execution));
  }
  for (const execution of [{ isolation: "auto" }, { isolation: "arbitrary" }, { isolation: "none", worker_count: 3 }])
    await assert.rejects(scoring(response(), { execution, fetchImpl: () => assert.fail("must not send unresolved constraints") }), /invalid_execution/);
});

test("controller resolves auto isolation before the actual scorer HTTP request", async () => {
  for (const [code, stdout, isolation] of [[0, "", "worktree"], [0, " M file", "none"], [1, "", "none"]]) {
    let sent;
    const h = harness({ pi: { exec: async () => ({ code, stdout }) }, score: args => scorePlan({ ...args,
      apiKey: "test-key", fetchImpl: async (_url, options) => {
        sent = JSON.parse(options.body).state.execution;
        return new Response(JSON.stringify(response()));
      } }) });
    const result = await h.controller.plan({ ...h.params, isolation: "auto" }, h.ctx, h.signal, "score");
    assert.equal(result.isolation, isolation);
    assert.deepEqual(sent, { isolation, worker_count: 2 });
  }
});

test("network, HTTP, malformed JSON, duplicate keys, oversized response, timeout and cancellation fail closed", async () => {
  for (const fetchImpl of [
    async () => { throw new Error("SECRET BODY"); },
    async () => new Response("SECRET BODY", { status: 500 }),
    async () => new Response("not JSON SECRET"),
    async () => new Response(JSON.stringify(response()).replace('"task_coverage":', '"task_coverage":{"type":"noul","noul":0},"task_coverage":')),
    async () => new Response("x".repeat(32769)),
    async (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("SECRET TIMEOUT")))),
  ]) {
    const receipts = [];
    await assert.rejects(scoring(response(), { fetchImpl, timeoutMs: 10, receipt: row => receipts.push(row) }), /Jev scoring stopped/);
    assert.equal(receipts.length, 2);
    assert.ok(!JSON.stringify(receipts).includes("SECRET"));
    assert.equal(receipts[1].usage, null);
  }
  const abort = new AbortController(); abort.abort();
  await assert.rejects(scoring(response(), { signal: abort.signal, fetchImpl: () => assert.fail("must not request") }));
});

test("digest binds issue, plan, execution and policy and is key-order stable", () => {
  const a = bindingDigest("issue", plan(), { isolation: "none" });
  assert.equal(a, bindingDigest("issue", { checks: plan().checks, integration: plan().integration, slices: plan().slices, summary: plan().summary }, { isolation: "none" }));
  assert.notEqual(a, bindingDigest("issue2", plan(), { isolation: "none" }));
  const changed = plan(); changed.slices[0].task += " change";
  assert.notEqual(a, bindingDigest("issue", changed, { isolation: "none" }));
  assert.notEqual(a, bindingDigest("issue", plan(), { isolation: "worktree" }));
  assert.match(POLICY_DIGEST, /^[a-f0-9]{64}$/);
});

function harness(overrides = {}) {
  const order = [], ctx = { cwd: tmpdir(), sessionManager: { getSessionId: () => "session" } };
  const signal = new AbortController().signal;
  const ceiling = { current: null, update(value) { this.current = value; } };
  const controller = new Controller({ pi: {}, ceiling, sessionId: "session", receipt() {},
    launchPlanner: async (_pi, request) => {
      order.push("plan"); assert.equal(request.agent, "planner");
      assert.deepEqual(ceiling.current.allowedTools, READ_TOOLS);
      return plan();
    },
    score: async args => {
      order.push("score");
      return { ...validateResponse(response()), plan_digest: bindingDigest(args.issue, args.plan, args.execution), policy_digest: POLICY_DIGEST };
    },
    callRpc: async (_pi, method, params) => {
      order.push("dispatch"); assert.equal(method, "spawn");
      assert.deepEqual(ceiling.current.allowedAgents, ["worker"]);
      assert.equal(params.isolation, "none");
      assert.ok(params.timeoutMs > 0);
      assert.ok(params.script.includes("runs.all"));
      return { details: { asyncId: "run-1" } };
    }, ...overrides });
  return { controller, order, ceiling, ctx, signal, params: { issue: "issue", workers: 2, isolation: "none", timeout_ms: 10000 } };
}

test("dispatch order is planner -> actual score -> workers; once only", async () => {
  const { controller, order, ctx, signal, params, ceiling } = harness();
  assert.equal(ceiling.current, null, "construction alone must not engage a ceiling");
  const result = await controller.plan(params, ctx, signal, "tool");
  assert.deepEqual(order, ["plan", "score"]);
  assert.deepEqual(ceiling.current.allowedAgents, []);
  const launched = await controller.dispatch(result.plan_digest, ctx, signal);
  assert.equal(launched.run_id, "run-1");
  assert.deepEqual(order, ["plan", "score", "dispatch"]);
  await assert.rejects(controller.dispatch(result.plan_digest, ctx, signal));
});

test("explicit nonparallel assessment returns a reason without Jev, approval, isolation work or dispatch", async () => {
  const h = harness({
    pi: { exec: () => assert.fail("no isolation setup for ordinary routing") },
    launchPlanner: async (_pi, request) => {
      assert.deepEqual(request.result.schema, PLANNER_RESULT_SCHEMA);
      return notParallelizable;
    },
    score: () => assert.fail("nonparallel assessment must not reach Jev"),
    callRpc: () => assert.fail("fallback must not launch workers"),
  });
  const result = await h.controller.plan({ ...h.params, isolation: "auto" }, h.ctx, h.signal, "tool");
  assert.equal(result.status, "not_parallelizable");
  assert.equal(result.reason, notParallelizable.reason);
  assert.equal(result.dispatch_authorized, false);
  for (const key of ["accepted", "scores", "plan", "plan_digest"]) assert.equal(Object.hasOwn(result, key), false, key);
  assert.equal(h.controller.approval, null);
  assert.equal(h.controller.dispatched, false);
  assert.equal(h.controller.attempt, 0);
  await assert.rejects(h.controller.dispatch("a".repeat(64), h.ctx, h.signal), /Dispatch denied/);
});

test("malformed nonparallel results cannot release the gate and consume the three-iteration limit", async () => {
  const invalid = [
    {}, "not_parallelizable", { status: "not_parallelizable" },
    ...["", "  ", null, 1, "x".repeat(4001), "bad\0reason"].map(reason => ({ ...notParallelizable, reason })),
    { ...notParallelizable, accepted: true }, { ...notParallelizable, plan: plan() },
  ];
  for (const value of invalid) {
    let revisions = 0;
    const h = harness({ launchPlanner: async () => { revisions++; return value; },
      score: () => assert.fail("invalid/fallback results must not be scored") });
    await assert.rejects(h.controller.plan(h.params, h.ctx, h.signal, "tool"), /Planning stopped/);
    assert.equal(revisions, 3);
    assert.equal(h.controller.approval, null);
    assert.deepEqual(h.ceiling.current.allowedAgents, []);
  }
});

test("nonparallel result cannot override cancellation or navigation stop", async () => {
  for (const cancel of [h => h.controller.stop(), (_h, abort) => abort.abort()]) {
    const abort = new AbortController();
    const h = harness({ launchPlanner: async () => { cancel(h, abort); return notParallelizable; },
      score: () => assert.fail("cancelled assessment must not be scored") });
    await assert.rejects(h.controller.plan(h.params, h.ctx, abort.signal, "tool"), /Planning stopped/);
    assert.equal(h.controller.approval, null);
    assert.deepEqual(h.ceiling.current.allowedAgents, []);
  }
});

test("post-score nonparallel results cannot bypass the gate; the loop stops after three iterations", async () => {
  let revisions = 0, scores = 0;
  const h = harness({
    launchPlanner: async (_pi, request) => {
      assert.deepEqual(request.result.schema, revisions === 0 ? PLANNER_RESULT_SCHEMA : PLAN_SCHEMA);
      revisions++;
      return revisions === 1 ? plan() : notParallelizable;
    },
    score: async args => ({ ...validateResponse(response(++scores === 1 ? 0.7 : 0.8)),
      plan_digest: bindingDigest(args.issue, args.plan, args.execution), policy_digest: POLICY_DIGEST }),
  });
  const result = await h.controller.plan(h.params, h.ctx, h.signal, "tool");
  assert.equal(revisions, 3);
  assert.equal(scores, 1);
  assert.equal(result.status, "iteration_limit");
  assert.equal(result.best_score_pct, 70);
  assert.equal(result.dispatch_authorized, false);
  assert.equal(h.controller.approval, null);
  await assert.rejects(h.controller.dispatch("a".repeat(64), h.ctx, h.signal));
  assert.deepEqual(h.ceiling.current.allowedAgents, []);
  assert.ok(!h.order.includes("dispatch"));
});

test("starting another planning call cannot reset the no-fallback boundary after scoring", async () => {
  let revisions = 0, scores = 0;
  const abort = new AbortController();
  const h = harness({
    launchPlanner: async (_pi, request) => {
      revisions++;
      if (revisions === 1) return plan();
      assert.deepEqual(request.result.schema, PLAN_SCHEMA);
      if (revisions === 3) abort.abort();
      return notParallelizable;
    },
    score: async args => ({ ...validateResponse(response(++scores === 1 ? 0.8 : 0.7)),
      plan_digest: bindingDigest(args.issue, args.plan, args.execution), policy_digest: POLICY_DIGEST }),
  });
  await h.controller.plan(h.params, h.ctx, h.signal, "first");
  await h.controller.scoreOnly({ issue: h.params.issue, plan: plan() }, h.ctx, h.signal, "reject");
  await assert.rejects(h.controller.plan(h.params, h.ctx, abort.signal, "retry"), /Planning stopped/);
  assert.equal(revisions, 3);
  assert.equal(scores, 2);
  assert.equal(h.controller.approval, null);
  assert.deepEqual(h.ceiling.current.allowedAgents, []);
});

test("Jev API failure stops planning instead of enabling a later nonparallel fallback", async () => {
  let revisions = 0;
  const h = harness({ launchPlanner: async () => ++revisions === 1 ? plan() : notParallelizable,
    score: async () => { throw new Error("offline outage"); } });
  await assert.rejects(h.controller.plan(h.params, h.ctx, h.signal, "tool"), /Planning stopped/);
  await assert.rejects(h.controller.plan(h.params, h.ctx, h.signal, "retry"), /Controller unavailable/);
  assert.equal(revisions, 1);
  assert.equal(h.controller.approval, null);
  assert.deepEqual(h.ceiling.current.allowedAgents, []);
});

test("best maximin score is retained after three rejections, without approval or dispatch", async () => {
  let revisions = 0, scores = 0;
  const scoreValues = [
    { task_coverage: 0.91, slice_independence: 0.9, ownership_conflicts: 0.799, integration_feasibility: 0.95 },
    { task_coverage: 0.795, slice_independence: 0.84, ownership_conflicts: 0.82, integration_feasibility: 0.99 },
    { task_coverage: 0.7, slice_independence: 0.99, ownership_conflicts: 0.9, integration_feasibility: 0.95 },
  ];
  const candidates = scoreValues.map((_, index) => ({ ...plan(), summary: `candidate-${index + 1}` }));
  const h = harness({
    launchPlanner: async () => candidates[revisions++],
    score: async args => {
      const candidate = Number(args.plan.summary.slice(-1)) - 1;
      const result = validateResponse({
        answers: Object.fromEntries(Object.entries(scoreValues[candidate]).map(([key, noul]) => [key, { type: "noul", noul }])),
        usage: { input_tokens: 123, output_tokens: 4 },
      });
      scores++;
      return { ...result, plan_digest: bindingDigest(args.issue, args.plan, args.execution), policy_digest: POLICY_DIGEST };
    },
  });
  const result = await h.controller.plan(h.params, h.ctx, h.signal, "tool");
  assert.equal(revisions, 3);
  assert.equal(scores, 3);
  assert.equal(result.status, "iteration_limit");
  assert.equal(result.plan.summary, "candidate-1");
  assert.equal(result.best_score_pct, 79.9);
  assert.equal(result.iterations, 3);
  assert.equal(result.dispatch_authorized, false);
  assert.equal(h.controller.approval, null);
  await assert.rejects(h.controller.dispatch("a".repeat(64), h.ctx, h.signal));
  assert.ok(!h.order.includes("dispatch"));
});

test("a valid third iteration is approved and reports the best percentage", async () => {
  let revisions = 0, scores = 0;
  const h = harness({
    launchPlanner: async () => ({ ...plan(), summary: `candidate-${++revisions}` }),
    score: async args => ({ ...validateResponse(response(++scores < 3 ? 0.7 : 0.8)),
      plan_digest: bindingDigest(args.issue, args.plan, args.execution), policy_digest: POLICY_DIGEST }),
  });
  const result = await h.controller.plan(h.params, h.ctx, h.signal, "tool");
  assert.equal(revisions, 3);
  assert.equal(scores, 3);
  assert.equal(result.accepted, true);
  assert.equal(result.plan.summary, "candidate-3");
  assert.equal(result.best_score_pct, 80);
  assert.equal(result.iterations, 3);
  assert.ok(h.controller.approval);
});

test("no approval, stale digest, forged success, scorer errors and revision invalidate authorization", async () => {
  let h = harness();
  await assert.rejects(h.controller.dispatch("a".repeat(64), h.ctx, h.signal));
  assert.deepEqual(h.order, []);
  for (const score of [async () => { throw new Error("outage"); }, async () => ({ accepted: true }),
    async args => ({ ...validateResponse(response(0)), accepted: true, policy_digest: POLICY_DIGEST,
      plan_digest: bindingDigest(args.issue, args.plan, args.execution) })]) {
    h = harness({ score });
    await assert.rejects(h.controller.plan(h.params, h.ctx, h.signal, "tool"));
    assert.deepEqual(h.ceiling.current.allowedAgents, []);
    await assert.rejects(h.controller.dispatch("a".repeat(64), h.ctx, h.signal));
  }
  h = harness();
  const first = await h.controller.plan(h.params, h.ctx, h.signal, "tool");
  await h.controller.scoreOnly({ issue: "revised", plan: plan() }, h.ctx, h.signal, "score");
  await assert.rejects(h.controller.dispatch(first.plan_digest, h.ctx, h.signal));
  assert.ok(!h.order.includes("dispatch"));
});

test("cancel, expired approval, concurrent dispatch and changed cwd never authorize work", async () => {
  for (const modify of [h => { h.controller.approval.execution.deadline = Date.now() - 1; },
    h => { h.ctx.cwd = "/"; }, h => { h.controller.stop(); }, h => { h.controller.busy = true; }]) {
    const h = harness(); const result = await h.controller.plan(h.params, h.ctx, h.signal, "tool"); modify(h);
    await assert.rejects(h.controller.dispatch(result.plan_digest, h.ctx, h.signal));
    assert.ok(!h.order.includes("dispatch"));
  }
});

test("navigation/stop during final verification cannot report completion or release the gate", async () => {
  const h = harness({ pi: { exec: async () => { h.controller.stop(); return { code: 0 }; } } });
  const approved = await h.controller.plan(h.params, h.ctx, h.signal, "tool");
  await h.controller.dispatch(approved.plan_digest, h.ctx, h.signal);
  // Terminal inspection is covered with real package proof in cooperative-checkpoints.test.mjs.
  h.controller.checkpointReady = true;
  await assert.rejects(h.controller.checkpoint("verify", h.ctx, h.signal), /Checkpoint failed closed/);
  assert.equal(h.controller.wave, 0);
  assert.equal(h.controller.approval, null);
  assert.deepEqual(h.ceiling.current.allowedAgents, []);
});

test("ownership rejects overlaps, traversal, Git metadata, symlinks, cycles and serial-only plans", () => {
  const cwd = mkdtempSync(join(tmpdir(), "plan-"));
  try {
    assert.deepEqual(validatePlan(plan(), cwd, 2), [["logic", "tests"]]);
    for (const path of ["src/a.js", "../escape", "/absolute", ".git/config", "src", "SRC/a.js", "src/*.js"]) {
      const value = plan(); value.slices[1].files = [path];
      assert.throws(() => validatePlan(value, cwd, 2));
    }
    symlinkSync(tmpdir(), join(cwd, "src"));
    assert.throws(() => validatePlan(plan(), cwd, 2));
    const cyclic = plan(); cyclic.slices[0].dependencies = ["tests"]; cyclic.slices[1].dependencies = ["logic"];
    assert.throws(() => validatePlan(cyclic, tmpdir(), 2));
    const serial = plan(); serial.slices[1].dependencies = ["logic"];
    assert.throws(() => validatePlan(serial, tmpdir(), 2));
  } finally { rmSync(cwd, { recursive: true, force: true }); }
});

function bus() {
  const emitter = new EventEmitter();
  return { events: { emit: (name, value) => emitter.emit(name, value),
    on(name, fn) { emitter.on(name, fn); return () => emitter.off(name, fn); } } };
}
test("documented delegation correlation and cancellation clean up listeners", async () => {
  const pi = bus(); const abort = new AbortController();
  const request = { requestId: "r", ownerRunId: "o", nodeId: "n" };
  pi.events.on("prompt-template:subagent:request", () => {
    pi.events.emit("prompt-template:subagent:response", { ...request, requestId: "wrong", status: "completed" });
    pi.events.emit("prompt-template:subagent:response", { ...request, status: "completed", result: { kind: "structured", value: plan() } });
  });
  assert.deepEqual(await delegate(pi, request, abort.signal), plan());
  const pi2 = bus(); let cancelled;
  pi2.events.on("prompt-template:subagent:cancel", value => { cancelled = value; });
  const promise = delegate(pi2, request, abort.signal); abort.abort();
  await assert.rejects(promise, /cancelled/); assert.deepEqual(cancelled, request);
});

test("RPC only accepts correlated successful versioned replies", async () => {
  const pi = bus();
  pi.events.on("subagents:rpc:v1:request", request => {
    pi.events.emit(`subagents:rpc:v1:reply:${request.requestId}`, { ...request, success: true, data: { ok: true } });
  });
  assert.deepEqual(await rpc(pi, "spawn", {}), { ok: true });
  await assert.rejects(rpc(bus(), "spawn", {}, 10), /timeout/);
});
