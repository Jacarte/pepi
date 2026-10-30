import { appendFileSync, mkdirSync, lstatSync } from "node:fs";
import { createRequire } from "node:module";
import { join, dirname, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { Type } from "@earendil-works/pi-ai";
import { Text } from "@earendil-works/pi-tui";
import { getAgentDir, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Controller, PLAN_SCHEMA } from "./controller.mjs";

// Both profiles load the tools by default; subagent launches require cooperative_plan.
export default function (pi: ExtensionAPI) {
  let cooperative = false;
  let requireCooperativePlan = true;
  let controller: Controller | undefined;
  let restriction: { update(value: unknown): void; dispose(): void } | undefined;
  const dispose = () => {
    controller?.stop();
    // Aborted in-flight calls can still run finally blocks after session replacement.
    if (controller) controller.ceiling = undefined;
    controller = undefined;
    restriction?.dispose(); restriction = undefined;
    cooperative = false;
  };
  const createController = (ctx: ExtensionContext) => {
    const directory = resolve(process.env.PEPI_EVAL_OUTPUT ?? join(getAgentDir(), "logs"), "jev-receipts");
    for (let path = directory; path !== dirname(path); path = dirname(path)) {
      try {
        if (lstatSync(path).isSymbolicLink() && !(process.platform === "darwin" && ["/var", "/tmp"].includes(path)))
          throw new Error("Unsafe receipt directory");
      }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    }
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const file = join(directory, `${randomUUID()}.jsonl`);
    return new Controller({ pi, ceiling: restriction, sessionId: ctx.sessionManager.getSessionId(),
      receipt: (record: unknown) => appendFileSync(file, JSON.stringify(record) + "\n", { mode: 0o600, flag: "a" }) });
  };
  const releasePlanRequirement = () => { requireCooperativePlan = false; dispose(); };
  const active = () => {
    if (!cooperative || !controller) throw new Error("No active cooperative controller; dispatch is disabled");
    return controller;
  };
  const start = (ctx: ExtensionContext) => {
    if (!cooperative) {
      // Latch and install the ceiling synchronously, before any await or fallible receipt setup.
      // Pi 1.1 requires Node >=22.19, which supports require() of this public ESM export.
      cooperative = true;
      controller?.stop(); controller = undefined;
      const require = createRequire(join(getAgentDir(), "npm", "package.json"));
      const api = require("pi-subagents/capability-ceiling");
      restriction = api.registerSubagentCapabilityCeiling({ sessionId: ctx.sessionManager.getSessionId(),
        source: "cooperative-plan-gate", ceiling: { allowedAgents: [], allowedTools: [], denyExtensions: true } });
      controller = createController(ctx);
    }
    return active();
  };
  pi.on("session_start", () => { requireCooperativePlan = true; dispose(); });
  pi.on("session_shutdown", dispose);
  // Switching branches/sessions cannot resurrect an approval from a previous transcript.
  pi.on("session_before_switch", () => { controller?.stop(); });
  pi.on("session_before_fork", () => { controller?.stop(); });
  pi.on("session_before_tree", () => { controller?.stop(); });
  const subagentManagementActions = ["status", "stop", "list", "get", "guide", "children.list"];
  pi.on("tool_call", event => {
    if (event.toolName === "subagent" && !subagentManagementActions.includes(String(event.input.action))) {
      if (cooperative || requireCooperativePlan)
        return { block: true, reason: cooperative
          ? "Cooperative mode requires controller-owned approved-plan dispatch; generic launch/resume/workflows are disabled."
          : "Call cooperative_plan before launching subagents; an explicit not_parallelizable result restores ordinary routing." };
    }
    if (!cooperative) return;
    if (!["read", "grep", "find", "ls", "subagent", "subagents_enable", "bg_wait", "cooperative_plan", "jev_score_plan", "cooperative_dispatch", "cooperative_checkpoint"].includes(event.toolName)
        && (!controller || controller.stopped || !controller.dispatched || (controller.activeRun && !controller.checkpointReady)))
      return { block: true, reason: "Cooperative mode permits parent mutation/shell tools only at a verified terminal integration checkpoint." };
  });
  pi.registerTool({
    name: "cooperative_plan", label: "Plan and score cooperative work", executionMode: "sequential",
    description: "Read-only planner + parent Jev scoring for at most 3 iterations. Approval still requires every immutable criterion to reach 0.8. If none passes, return the best-scoring plan as diagnostic only; dispatch stays unauthorized. The parent session displays the best score as a percentage. Requires explicitly authorized TypeSafe egress and TYPESAFE_API_KEY. Issue and plan leave this machine. No secrets. Auto selects worktrees for independent plans in clean Git repos and shared-tree execution otherwise. Before scoring, an explicit not_parallelizable result restores ordinary worker/deep-worker routing without approval or dispatch. No workers launch here.",
    renderResult(result, { isPartial }, theme) {
      if (isPartial) return new Text(theme.fg("warning", "Planning/scoring..."), 0, 0);
      const details = result.details as { best_score_pct?: unknown } | undefined;
      const score = details?.best_score_pct;
      const percentage = typeof score === "number" && Number.isFinite(score)
        ? `${Number.isInteger(score) ? score : score.toFixed(1)}%` : "No score";
      return new Text(theme.fg(typeof score === "number" && Number.isFinite(score) ? "success" : "dim",
        typeof score === "number" && Number.isFinite(score) ? `Best: ${percentage}` : percentage), 0, 0);
    },
    parameters: Type.Object({ issue: Type.String({ minLength: 1, maxLength: 32000 }),
      workers: Type.Integer({ minimum: 2, maximum: 8 }), isolation: Type.Union([Type.Literal("none"), Type.Literal("worktree"), Type.Literal("auto")]),
      timeout_ms: Type.Integer({ minimum: 1000, maximum: 7200000 }) }, { additionalProperties: false }),
    async execute(id, params, signal, _update, ctx) {
      const current = start(ctx);
      const details = await current.plan(params, ctx, signal ?? new AbortController().signal, id);
      if (details.status === "not_parallelizable" && controller === current && !current.stopped && !signal?.aborted)
        releasePlanRequirement();
      return { content: [{ type: "text", text: JSON.stringify(details) }], details };
    },
  });
  pi.registerTool({
    name: "jev_score_plan", label: "Score an explicit coding plan", executionMode: "sequential",
    description: "Score only the explicitly supplied issue and structured plan with the immutable Jev rubric. Invalidates any prior dispatch approval. A standalone score NEVER activates cooperative mode or authorizes workers: use cooperative_plan for controller-owned planning. No routing, session dumps or secrets.",
    parameters: Type.Object({ issue: Type.String({ minLength: 1, maxLength: 32000 }), plan: Type.Unsafe(PLAN_SCHEMA) }, { additionalProperties: false }),
    async execute(id, params, signal, _update, ctx) {
      if (!cooperative) { controller?.stop(); controller = createController(ctx); }
      const current = cooperative ? active() : controller!;
      const details = await current.scoreOnly(params, ctx, signal ?? new AbortController().signal, id);
      return { content: [{ type: "text", text: JSON.stringify({ ...details, dispatch_authorized: false }) }], details };
    },
  });
  pi.registerTool({
    name: "cooperative_dispatch", label: "Dispatch approved coding slices", executionMode: "sequential",
    description: "Launch the next approved dependency wave, once. Briefs and ownership come from the scored plan, not caller text. Workers use configured models. Every wave requires parent integration and controller-verified checks before the next. A lost launch reply is not permission to retry.",
    parameters: Type.Object({ plan_digest: Type.String({ pattern: "^[a-f0-9]{64}$" }) }, { additionalProperties: false }),
    async execute(_id, params, signal, _update, ctx) {
      const details = await active().dispatch(params.plan_digest, ctx, signal ?? new AbortController().signal);
      return { content: [{ type: "text", text: JSON.stringify(details) }], details };
    },
  });
  pi.registerTool({
    name: "cooperative_checkpoint", label: "Verify integration checkpoint", executionMode: "sequential",
    description: "Inspect waits for exact successful process-terminal proof for every worker in the current wave before enabling parent integration. After parent inspection/reconciliation, verify executes the approved plan checks in the authoritative task tree. Only passing checks release the next dependency wave. No commits/pushes or automatic merging.",
    parameters: Type.Object({ action: Type.Union([Type.Literal("inspect"), Type.Literal("verify")]) }, { additionalProperties: false }),
    async execute(_id, params, signal, _update, ctx) {
      const current = active();
      const details = await current.checkpoint(params.action, ctx, signal ?? new AbortController().signal);
      // Release only after the last wave's terminal proof, parent integration and passing checks.
      if (details.complete && controller === current && !current.stopped && !signal?.aborted)
        releasePlanRequirement();
      return { content: [{ type: "text", text: JSON.stringify(details) }], details };
    },
  });
}
