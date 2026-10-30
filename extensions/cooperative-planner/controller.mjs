import { randomUUID } from "node:crypto";
import { lstatSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { bindingDigest, canonical, scorePlan } from "./scorer.mjs";

export const READ_TOOLS = ["read", "grep", "find", "ls"];
export const WORK_TOOLS = [...READ_TOOLS, "bash", "edit", "write"];
export const PLANNER = "planner";
export const MAX_PLAN_ITERATIONS = 3;
export const PLAN_SCHEMA = {
  type: "object", additionalProperties: false, required: ["summary", "slices", "integration", "checks"],
  properties: {
    summary: { type: "string", minLength: 1, maxLength: 4000 },
    slices: { type: "array", minItems: 2, maxItems: 8, items: {
      type: "object", additionalProperties: false, required: ["id", "task", "files", "dependencies"],
      properties: {
        id: { type: "string", pattern: "^[a-z][a-z0-9-]{0,31}$" },
        task: { type: "string", minLength: 1, maxLength: 6000 },
        files: { type: "array", minItems: 1, maxItems: 64, items: { type: "string", minLength: 1, maxLength: 240 } },
        dependencies: { type: "array", maxItems: 7, uniqueItems: true, items: { type: "string" } },
      },
    } },
    integration: { type: "string", minLength: 1, maxLength: 4000 },
    checks: { type: "array", minItems: 1, maxItems: 32, items: { type: "string", minLength: 1, maxLength: 1000 } },
  },
};
export const PLANNER_RESULT_SCHEMA = {
  anyOf: [PLAN_SCHEMA, {
    type: "object", additionalProperties: false, required: ["status", "reason"],
    properties: {
      status: { type: "string", const: "not_parallelizable" },
      reason: { type: "string", minLength: 1, maxLength: 4000, pattern: "\\S" },
    },
  }],
};
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const exact = (value, keys) => object(value) && Object.keys(value).sort().join() === [...keys].sort().join();
const text = (value, max) => typeof value === "string" && value.trim() && value.length <= max && !value.includes("\0");

export function validatePlan(plan, cwd, workerCount) {
  const fail = () => { throw new Error("Invalid plan structure, dependencies or exclusive file ownership"); };
  if (!Number.isInteger(workerCount) || workerCount < 2 || workerCount > 8) fail();
  if (!exact(plan, ["summary", "slices", "integration", "checks"]) || !text(plan.summary, 4000)
      || !text(plan.integration, 4000) || !Array.isArray(plan.checks) || !plan.checks.length
      || plan.checks.length > 32 || plan.checks.some(check => !text(check, 1000))
      || !Array.isArray(plan.slices) || plan.slices.length !== workerCount
      || Buffer.byteLength(canonical(plan)) > 48000) fail();
  const ids = new Set(), paths = [];
  for (const slice of plan.slices) {
    if (!exact(slice, ["id", "task", "files", "dependencies"]) || !/^[a-z][a-z0-9-]{0,31}$/.test(slice.id)
        || ids.has(slice.id) || !text(slice.task, 6000) || !Array.isArray(slice.files)
        || !slice.files.length || slice.files.length > 64 || !Array.isArray(slice.dependencies)
        || slice.dependencies.length > 7 || new Set(slice.dependencies).size !== slice.dependencies.length) fail();
    ids.add(slice.id);
    for (const file of slice.files) {
      if (!text(file, 240) || isAbsolute(file) || /[\\*?\[\]{}\x00-\x1f:]/.test(file)
          || file.split("/").some(part => !part || [".", "..", ".git"].includes(part.toLowerCase()))) fail();
      const normalized = file.toLowerCase();
      if (paths.some(other => other === normalized || other.startsWith(normalized + "/") || normalized.startsWith(other + "/"))) fail();
      paths.push(normalized);
      let current = cwd;
      for (const part of file.split("/")) {
        current = join(current, part);
        try { if (lstatSync(current).isSymbolicLink()) fail(); }
        catch (error) { if (error.code !== "ENOENT") throw error; }
      }
    }
  }
  const waves = [], done = new Set();
  while (done.size < ids.size) {
    const ready = plan.slices.filter(slice => !done.has(slice.id) && slice.dependencies.every(id => done.has(id)));
    if (!ready.length) fail();
    waves.push(ready.map(slice => slice.id));
    ready.forEach(slice => done.add(slice.id));
  }
  if (!waves.some(wave => wave.length > 1)) fail(); // This is parallel planning, not a disguised serial solve.
  return waves;
}

export function delegate(pi, request, signal) {
  return new Promise((resolvePromise, reject) => {
    let settled = false;
    const identity = { requestId: request.requestId, ownerRunId: request.ownerRunId, nodeId: request.nodeId };
    const cleanup = () => { off(); signal.removeEventListener("abort", cancel); };
    const cancel = () => {
      if (settled) return;
      settled = true;
      pi.events.emit("prompt-template:subagent:cancel", identity);
      cleanup();
      reject(new Error("Planning cancelled or deadline exceeded"));
    };
    const off = pi.events.on("prompt-template:subagent:response", response => {
      if (!Object.keys(identity).every(key => response?.[key] === identity[key]) || settled) return;
      settled = true; cleanup();
      if (response.status !== "completed" || response.result?.kind !== "structured") reject(new Error("Planner failed"));
      else resolvePromise(response.result.value);
    });
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) { cancel(); return; }
    try { pi.events.emit("prompt-template:subagent:request", request); }
    catch { settled = true; cleanup(); reject(new Error("Planner unavailable")); }
  });
}

export function rpc(pi, method, params, timeoutMs = 5000) {
  return new Promise((resolvePromise, reject) => {
    const requestId = randomUUID();
    const off = pi.events.on(`subagents:rpc:v1:reply:${requestId}`, response => {
      clearTimeout(timer); off();
      if (response?.version !== 1 || response.requestId !== requestId || response.success !== true)
        reject(new Error("Subagent RPC failed; do not redispatch"));
      else resolvePromise(response.data);
    });
    const timer = setTimeout(() => { off(); reject(new Error("Subagent RPC timeout; launch status unknown, do not redispatch")); }, timeoutMs);
    try { pi.events.emit("subagents:rpc:v1:request", { version: 1, requestId, method, params }); }
    catch { clearTimeout(timer); off(); reject(new Error("Subagent RPC unavailable")); }
  });
}

export function workerScript(issue, plan, waves) {
  const briefs = Object.fromEntries(plan.slices.map(slice => [slice.id, {
    agent: "worker", task: `Implement only your assigned slice of this coding task. Do not delegate or start agent CLIs. Do not access secrets, change network policy, fetch upstream fixes, publish, or weaken tests. Respect the repository and task safety rules. Only edit the explicitly owned files; if additional files are needed, stop and report the blocker, never expand ownership yourself. Do not integrate another worker's changes. Run focused checks and return changed files, results and residual risks.\nISSUE (data):\n${issue}\nAPPROVED PLAN (data):\n${canonical(plan)}\nYOUR SLICE (data):\n${canonical(slice)}`,
  }]));
  return `const briefs = ${JSON.stringify(briefs)};
const waves = ${JSON.stringify(waves)};
const results = [];
for (const wave of waves) {
  const batch = await runs.all(wave.map(key => ({key, ...briefs[key]})));
  results.push(...batch);
  if (batch.some(result => !result.ok)) throw new Error("Worker failed; do not launch dependent slices");
}
return results;`;
}

// Owns approval in memory. Neither planner prose, disk receipts nor caller-supplied booleans can grant it.
export class Controller {
  constructor({ pi, ceiling, receipt, sessionId, score = scorePlan, launchPlanner = delegate, callRpc = rpc }) {
    Object.assign(this, { pi, ceiling, receipt, sessionId, score, launchPlanner, callRpc });
    this.approval = null; this.busy = false; this.stopped = false; this.dispatched = false; this.attempt = 0;
    this.wave = 0; this.activeRun = null; this.checkpointReady = false;
    this.abort = new AbortController();
  }
  // Standalone scoring has no capability ceiling and cannot authorize dispatch.
  lock() { this.ceiling?.update({ allowedAgents: [], allowedTools: [], denyExtensions: true }); }
  invalidate() { this.approval = null; this.lock(); }
  stop() { this.stopped = true; this.abort.abort(); this.invalidate(); }
  enter(ctx) {
    if (this.busy || this.stopped || ctx.sessionManager.getSessionId() !== this.sessionId)
      throw new Error("Controller unavailable, busy or already dispatched");
    this.busy = true;
  }
  async scored(issue, plan, execution, signal, toolCallId) {
    this.approval = null;
    const result = await this.score({ issue, plan, execution, signal, sessionId: this.sessionId,
      toolCallId, attempt: ++this.attempt, receipt: this.receipt });
    // Verify the actual scorer value at the controller boundary, not a text claim or receipt on disk.
    const { validateResponse, POLICY_DIGEST } = await import("./scorer.mjs");
    const validated = validateResponse({ answers: Object.fromEntries(Object.entries(result.scores ?? {}).map(
      ([key, noul]) => [key, { type: "noul", noul }])), usage: result.usage });
    if (result.accepted !== validated.accepted || result.policy_digest !== POLICY_DIGEST
        || result.plan_digest !== bindingDigest(issue, plan, execution)) throw new Error("Scorer binding failed");
    signal.throwIfAborted();
    return result;
  }
  async plan(params, ctx, signal, toolCallId) {
    this.enter(ctx);
    if (this.dispatched) { this.busy = false; throw new Error("Cannot revise after worker dispatch"); }
    this.invalidate();
    const deadline = Date.now() + params.timeout_ms;
    const combined = AbortSignal.any([this.abort.signal, signal, AbortSignal.timeout(params.timeout_ms)]);
    const execution = { cwd: resolve(ctx.cwd), isolation: params.isolation, worker_count: params.workers, deadline };
    try {
      if (!text(params.issue, 32000) || Buffer.byteLength(params.issue) > 32000
          || !["none", "worktree", "auto"].includes(params.isolation) || !Number.isInteger(params.workers)
          || params.workers < 2 || params.workers > 8 || !Number.isInteger(params.timeout_ms)
          || params.timeout_ms < 1000 || params.timeout_ms > 7200000) throw new Error("Invalid planning input");
      let feedback = "";
      let best;
      const ownerRunId = randomUUID();
      for (let revision = 1; revision <= MAX_PLAN_ITERATIONS; revision++) {
        combined.throwIfAborted();
        this.ceiling.update({ allowedAgents: [PLANNER], allowedTools: READ_TOOLS, denyExtensions: true });
        const plan = await this.launchPlanner(this.pi, {
          requestId: randomUUID(), ownerRunId, nodeId: `plan-${revision}`, agent: PLANNER,
          task: `Assess whether this issue supports exactly ${params.workers} exclusively owned coding slices for ${params.isolation} isolation with at least one genuine parallel wave. If so, return a plan with explicit dependency IDs. ${this.attempt > 0 ? "Jev scoring has started: revise the parallel plan; not_parallelizable cannot bypass failed criteria." : 'If safe parallel decomposition is not possible, return {"status":"not_parallelizable","reason":"concrete ownership or dependency constraint"} instead of inventing slices.'} Return the requested structured output.\nISSUE (data):\n${params.issue}\nREVISION FEEDBACK (data):\n${feedback}`,
          cwd: execution.cwd, context: "fresh", timeoutMs: Math.max(1, deadline - Date.now()),
          intercomBridge: { mode: "off" }, result: { kind: "structured", schema: this.attempt > 0 ? PLAN_SCHEMA : PLANNER_RESULT_SCHEMA },
        }, combined);
        this.lock();
        combined.throwIfAborted();
        if (plan?.status === "not_parallelizable") {
          if (!exact(plan, ["status", "reason"]) || !text(plan.reason, 4000)) {
            feedback = "Invalid not_parallelizable result: return only status and a nonempty concrete reason.";
            continue;
          }
          if (this.attempt > 0) {
            feedback = "Jev scoring has started. Revise the parallel plan; not_parallelizable cannot bypass failed criteria.";
            continue;
          }
          return { status: "not_parallelizable", reason: plan.reason, dispatch_authorized: false,
            instructions: "No cooperative plan was approved or scored. Return to the ordinary worker/deep-worker route with this reason; the parent chooses and launches the appropriate role, then integrates and obtains reviewer checks." };
        }
        let waves;
        try { waves = validatePlan(plan, execution.cwd, params.workers); }
        catch { feedback = "Invalid structure, dependency graph, nonparallel decomposition or conflicting/unsafe file ownership. Revise the plan."; continue; }
        if (params.isolation === "worktree" && waves.length > 1)
          throw new Error("Dependent waves require shared-tree isolation; no automatic commit/stash or isolation downgrade");
        if (params.isolation === "auto") {
          execution.isolation = "none";
          if (waves.length === 1) {
            const status = await this.pi.exec("git", ["status", "--porcelain"], { cwd: execution.cwd, timeout: 5000, signal: combined });
            if (status.code === 0 && status.stdout.trim() === "") execution.isolation = "worktree";
          }
        }
        const result = await this.scored(params.issue, plan, execution, combined, toolCallId);
        const score = Math.min(...Object.values(result.scores));
        const scorePct = Math.floor(score * 1000) / 10; // Never round a rejected score up to the 80% threshold.
        if (!best || score > best.score) {
          best = { plan: JSON.parse(canonical(plan)), score, scorePct };
        }
        if (result.accepted) {
          this.approval = { issue: params.issue, plan: JSON.parse(canonical(plan)), execution, result, waves };
          return { plan, ...result, isolation: execution.isolation, deadline_ms: deadline,
            best_score_pct: scorePct, iterations: revision,
            instructions: "Dispatch the first wave using this exact plan_digest. Between waves use cooperative_checkpoint inspect, integrate changes in the parent, then cooperative_checkpoint verify before dispatching again." };
        }
        feedback = canonical({ prior_plan: plan, scores: result.scores, required_threshold: 0.8 });
      }
      if (!best) throw new Error("No scoreable plan within three iterations");
      return { status: "iteration_limit", plan: best.plan, best_score_pct: best.scorePct,
        iterations: MAX_PLAN_ITERATIONS, dispatch_authorized: false,
        instructions: "No plan met every criterion within three iterations. This best-scoring plan is diagnostic only; dispatch is not authorized." };
    } catch { this.stop(); throw new Error("Planning stopped without approval (planner/scorer failure, cancellation or deadline). Start a new session after resolving the cause."); }
    finally { this.busy = false; this.lock(); }
  }
  async scoreOnly(params, ctx, signal, toolCallId) {
    this.enter(ctx);
    if (this.dispatched) { this.busy = false; throw new Error("Cannot rescore after worker dispatch"); }
    this.invalidate();
    try {
      const combined = AbortSignal.any([signal, this.abort.signal]);
      validatePlan(params.plan, ctx.cwd, params.plan?.slices?.length);
      return await this.scored(params.issue, params.plan, { cwd: resolve(ctx.cwd) }, combined, toolCallId);
    } catch { this.stop(); throw new Error("Scoring stopped without dispatch authorization"); }
    finally { this.busy = false; }
  }
  async dispatch(planDigest, ctx, signal) {
    this.enter(ctx);
    try {
      const approved = this.approval;
      if (!approved || this.activeRun || this.wave >= approved.waves.length
          || approved.result.plan_digest !== planDigest || Date.now() >= approved.execution.deadline
          || resolve(ctx.cwd) !== approved.execution.cwd || signal.aborted || this.abort.signal.aborted)
        throw new Error("No current matching unexpired approval");
      const { issue, plan, execution } = approved;
      if (bindingDigest(issue, plan, execution) !== planDigest) throw new Error("Approval changed");
      const waves = validatePlan(plan, ctx.cwd, execution.worker_count);
      // Consume the wave before awaiting: a lost acknowledgement can never cause duplicate workers.
      this.activeRun = "launch-pending"; this.dispatched = true;
      this.ceiling.update({ allowedAgents: ["worker"], allowedTools: WORK_TOOLS, denyExtensions: true });
      const data = await this.callRpc(this.pi, "spawn", { script: workerScript(issue, plan, [waves[this.wave]]),
        cwd: execution.cwd, context: "fresh", isolation: execution.isolation, async: true,
        timeoutMs: Math.max(1, execution.deadline - Date.now()) });
      const runId = data?.details?.runId ?? data?.details?.asyncId;
      if (typeof runId !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(runId)) throw new Error("Missing launch identity");
      this.activeRun = runId;
      return { dispatched: true, plan_digest: planDigest, run_id: runId, wave: this.wave,
        instructions: "Wait for completion, then call cooperative_checkpoint inspect. Parent mutation/shell tools stay blocked until exact child process-terminal proof is observed. Integrate and run checkpoint verify before releasing the next wave." };
    } catch { this.stop(); throw new Error("Dispatch denied or failed. Inspect child status; never redispatch on missing acknowledgement."); }
    // Async dependency waves re-check the current ceiling when each leaf launches.
    // Keep the approved worker ceiling while the owned workflow runs; the public launch hook stays closed.
    finally { this.busy = false; if (this.stopped || !this.dispatched) this.lock(); }
  }
  async checkpoint(action, ctx, signal) {
    this.enter(ctx);
    try {
      const approved = this.approval;
      if (!approved || !this.activeRun || Date.now() >= approved.execution.deadline || signal.aborted)
        throw new Error("No active checkpoint or deadline exceeded");
      if (action === "inspect") {
        const status = await this.callRpc(this.pi, "status", { id: this.activeRun });
        if (!terminalWave(status, this.activeRun, approved.waves[this.wave]))
          return { ready: false, instructions: "Exact successful child terminal proof is not available. Do not integrate or launch more work. Inspect status; stop on failure." };
        this.lock(); this.checkpointReady = true;
        return { ready: true, integration: approved.plan.integration, checks: approved.plan.checks,
          instructions: "Parent: inspect/reconcile this wave and integrate patches into the authoritative task tree. Do not commit/push. Then call checkpoint verify to execute the approved checks." };
      }
      if (action !== "verify" || !this.checkpointReady) throw new Error("Inspect terminal wave before integration/checks");
      for (const command of approved.plan.checks) {
        const remaining = approved.execution.deadline - Date.now();
        if (remaining <= 0) throw new Error("Deadline exceeded");
        const result = await this.pi.exec("bash", ["-lc", command], { cwd: approved.execution.cwd,
          timeout: Math.min(remaining, 300000), signal: AbortSignal.any([signal, this.abort.signal]) });
        if (result.code !== 0 || result.killed) return { verified: false, instructions: "An approved checkpoint check failed. Parent may fix integration and repeat verify; dependent workers remain blocked." };
      }
      signal.throwIfAborted();
      this.abort.signal.throwIfAborted();
      this.activeRun = null; this.checkpointReady = false; this.wave++;
      return { verified: true, complete: this.wave === approved.waves.length,
        instructions: this.wave === approved.waves.length ? "All waves integrated and checks passed. Report results and remaining risks." : "Checkpoint passed. Dispatch the next wave using the same approved plan digest." };
    } catch { this.stop(); throw new Error("Checkpoint failed closed; no dependent work authorized"); }
    finally { this.busy = false; }
  }
}

export function terminalWave(status, runId, keys) {
  // Targeted RPC status exposes canonical evidence here; asyncSnapshot is a display-only projection.
  const details = status?.details;
  const roster = details?.workflowChildren;
  const proof = details?.workflowTerminalProof;
  if (details?.runId !== runId || roster?.version !== 1 || roster.workflowRunId !== runId
      || roster.inventoryComplete !== true || roster.workflowState !== "completed"
      || proof?.version !== 1 || proof.kind !== "workflow" || proof.runId !== runId
      || proof.state !== "observed" || proof.dispatchClosed !== true
      || !Array.isArray(keys) || !keys.length || new Set(keys).size !== keys.length
      || !Array.isArray(roster.children) || roster.children.length !== keys.length
      || !Array.isArray(proof.children) || proof.children.length !== keys.length) return false;
  const runIds = new Set();
  return keys.every(key => {
    const matches = roster.children.filter(child => child?.childId === key);
    if (matches.length !== 1) return false;
    const child = matches[0];
    if (child.state !== "completed" || child.agent !== "worker" || typeof child.runId !== "string"
        || !child.runId || runIds.has(child.runId)) return false;
    runIds.add(child.runId);
    const proofs = proof.children.filter(item => item?.runId === child.runId);
    if (proofs.length !== 1) return false;
    const terminal = proofs[0];
    return terminal.version === 1 && terminal.state === "observed"
      && typeof terminal.runnerProcessInstanceId === "string" && !!terminal.runnerProcessInstanceId
      && Number.isFinite(terminal.observedAt) && terminal.observedAt >= 0;
  });
}
