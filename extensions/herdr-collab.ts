import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  createExec,
  createHerdrCollab,
  filterPanePromptOptions,
  MAX_PARALLEL_WORKERS,
  MAX_REVIEWERS,
  PANE_ENV,
  PANE_NAME_PATTERN,
  PARENT_NAME,
  ROLE_NAMES,
  RUN_ID_PATTERN,
  type CallContext,
  type CollabDeps,
} from "./herdr-collab/core.ts";

// Opt-in herdr collaboration mode. Parent tools register only inside herdr (HERDR_ENV=1);
// panes started by herdr_spawn get only the peer-messaging tools.
export default function (pi: ExtensionAPI) {
  const env = process.env;
  if (env.PI_SUBAGENT_CHILD === "1" || env.HERDR_ENV !== "1") return;
  const agentDir = resolve(__dirname, "..");
  const deps: CollabDeps = {
    exec: createExec(env),
    env,
    agentDir,
    builtinAgentsDir: join(agentDir, "npm", "node_modules", "pi-subagents", "agents"),
    homeDir: homedir(),
    wsRoot: env[PANE_ENV.root] || join(homedir(), ".herdr-ws"),
    extensionPath: join(__dirname, "herdr-collab.ts"),
  };
  const collab = createHerdrCollab(deps);
  const text = (summary: string, details: unknown) => ({
    content: [{ type: "text" as const, text: `${summary}\n\n${JSON.stringify(details, null, 2)}` }],
    details,
  });
  const runId = Type.Optional(Type.String({ pattern: RUN_ID_PATTERN, description: "Run id; defaults to this session's current run." }));
  const names = Type.Optional(Type.Array(Type.String({ pattern: PANE_NAME_PATTERN }), { description: "Pane names; defaults to all relevant panes." }));
  const sendParams = {
    to: Type.String({ pattern: PANE_NAME_PATTERN, description: "Target pane name." }),
    message: Type.String({ minLength: 1, maxLength: 4_000 }),
    timeoutMs: Type.Optional(Type.Integer({ minimum: 1_000, maximum: 1_800_000, description: "Bounded wait for the target to become idle (default 10 min)." })),
  };

  const workerName = env[PANE_ENV.name];
  const workerRun = env[PANE_ENV.run];
  if (env[PANE_ENV.worker] === "1") {
    if (!workerName || !workerRun) return;
    const inheritGlobal = env[PANE_ENV.inheritGlobal] === "1";
    pi.on("before_agent_start", (event) => {
      filterPanePromptOptions(event.systemPromptOptions as any, agentDir, inheritGlobal);
    });
    const identity = { runId: workerRun, name: workerName };
    pi.registerTool({
      name: "herdr_peers",
      label: "Herdr peers",
      description: "List the other panes in this herdr run (name, role, live state, remaining message budget) and your message history.",
      executionMode: "sequential",
      parameters: Type.Object({}, { additionalProperties: false }),
      async execute(_id, _params, signal) {
        const result = await collab.peers(identity, signal);
        return text(`herdr run ${result.runId}: ${result.peers.length} peers`, result);
      },
    });
    pi.registerTool({
      name: "herdr_send",
      label: "Herdr send",
      description:
        "Send a short message to another pane in this herdr run. It is queued until the target is idle " +
        "and never interrupts work; the call fails on timeout, a blocked or exited target, an unknown target, " +
        "a reviewer target, or the per-pair cap. Do not wait for replies.",
      executionMode: "sequential",
      parameters: Type.Object(sendParams, { additionalProperties: false }),
      async execute(_id, params, signal) {
        const result = await collab.send(identity, params, signal);
        return text(`delivered ${result.message.id} to ${params.to}`, result);
      },
    });
    return;
  }

  const call = (ctx: any, signal?: AbortSignal): CallContext => ({
    cwd: ctx.cwd,
    modelRegistry: ctx.modelRegistry,
    trusted: typeof ctx.isProjectTrusted === "function" ? ctx.isProjectTrusted() : undefined,
    signal,
  });
  const optIn = "Herdr collaboration mode only: use when the user explicitly asks for herdr mode; pi-subagents stays the default.";
  const model = Type.Optional(Type.String({ description: "Explicit provider/id. Omit to use the role's pinned model; a role without one fails." }));
  const spawnRoles = ROLE_NAMES.map((role) => Type.Literal(role));

  pi.registerTool({
    name: "herdr_spawn",
    label: "Herdr spawn",
    description:
      `${optIn} Start a wave of interactive pi panes (parent keeps the left half; panes tile the right half) with pi-subagents role definitions. ` +
      `Max ${MAX_PARALLEL_WORKERS} open worker panes per run. Parallel writers each get a jj workspace (git worktree fallback) ` +
      "under ~/.herdr-ws/<run>/<name>; a lone writer uses the main checkout (herdr_merge never integrates it). Without model, " +
      "each role uses its pinned settings model; a role with no pin requires model. " +
      "Returns after each task is submitted; use herdr_wait next.",
    executionMode: "sequential",
    parameters: Type.Object({
      runId,
      newRun: Type.Optional(Type.Boolean({ description: "Start a new run even if this session has a current run." })),
      workers: Type.Array(Type.Object({
        name: Type.String({ pattern: PANE_NAME_PATTERN }),
        role: Type.Union(spawnRoles),
        task: Type.String({ minLength: 1, maxLength: 12_000 }),
        context: Type.Optional(Type.String({ maxLength: 20_000 })),
        model,
        requiresImages: Type.Optional(Type.Boolean()),
      }, { additionalProperties: false }), { minItems: 1, maxItems: MAX_PARALLEL_WORKERS }),
      base: Type.Optional(Type.String({ description: "Base revision for new workspaces (default: sealed integration change, else main @-)." })),
      messageCap: Type.Optional(Type.Integer({ minimum: 0, maximum: 50, description: "Per-pair message cap for a new run (default 5)." })),
    }, { additionalProperties: false }),
    async execute(_id, params, signal, _update, ctx) {
      const result = await collab.spawn(params, call(ctx, signal));
      return text(`herdr run ${result.runId}: started ${result.panes.map((p) => `${p.name} (${p.role}, ${p.model} [${p.modelSource}], ${p.placement}${p.codegraph ? `, codegraph ${p.codegraph}` : ""})`).join("; ")}`, result);
    },
  });

  pi.registerTool({
    name: "herdr_review",
    label: "Herdr review panel",
    description:
      `${optIn} Start up to ${MAX_REVIEWERS} read-only reviewer panes, each with a different lens, on the shared checkout ` +
      "(the sealed integration workspace when present, else the main checkout). No workspaces, no peer messaging.",
    executionMode: "sequential",
    parameters: Type.Object({
      runId,
      target: Type.String({ minLength: 1, maxLength: 12_000, description: "What to review: requirements, changed paths, check results." }),
      reviewers: Type.Array(Type.Object({
        name: Type.String({ pattern: PANE_NAME_PATTERN }),
        lens: Type.String({ minLength: 1, maxLength: 500 }),
        model,
      }, { additionalProperties: false }), { minItems: 1, maxItems: MAX_REVIEWERS }),
      checkout: Type.Optional(Type.Union([Type.Literal("integration"), Type.Literal("main")])),
    }, { additionalProperties: false }),
    async execute(_id, params, signal, _update, ctx) {
      const result = await collab.review(params, call(ctx, signal));
      return text(`herdr run ${result.runId}: review panel ${result.panes.map((p) => `${p.name}${p.codegraph ? ` (codegraph ${p.codegraph})` : ""}`).join(", ")}`, result);
    },
  });

  pi.registerTool({
    name: "herdr_wait",
    label: "Herdr wait",
    description:
      `${optIn} Wait for panes to settle (herdr agent state). Reports idle/done, blocked, timeout, and exited separately; ` +
      "settled is not success, so read results with herdr_collect. Default timeout 30 min.",
    executionMode: "sequential",
    parameters: Type.Object({
      runId,
      names,
      timeoutMs: Type.Optional(Type.Integer({ minimum: 1_000, maximum: 7_200_000 })),
      mode: Type.Optional(Type.Union([Type.Literal("all"), Type.Literal("any")])),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const result = await collab.wait(params, signal);
      const groups = new Map<string, string[]>();
      for (const outcome of result.outcomes) groups.set(outcome.state, [...(groups.get(outcome.state) ?? []), outcome.name]);
      const summary = [...groups].map(([state, list]) => `${state}: ${list.join(", ")}`).join("; ");
      return text(`herdr_wait ${result.runId}: ${summary} (idle/done = settled, not success; blocked needs input)`, result);
    },
  });

  pi.registerTool({
    name: "herdr_collect",
    label: "Herdr collect",
    description:
      `${optIn} Read the last N lines of each pane (with a truncated flag), the workspace diff --stat for writers, ` +
      "and the run's message log entries for each pane.",
    executionMode: "sequential",
    parameters: Type.Object({
      runId,
      names,
      lines: Type.Optional(Type.Integer({ minimum: 10, maximum: 2_000 })),
      diff: Type.Optional(Type.Boolean()),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const result = await collab.collect(params, signal);
      const body = result.results.map((r) =>
        `## ${r.name} (${r.role}, ${r.state}${r.merge ? `, ${r.merge}` : ""})${r.truncated ? " [tail truncated]" : ""}\n${r.tail || r.readError || "(no output)"}` +
        (r.diff ? `\n### diff --stat${r.diff.snapshotted ? "" : " (not snapshotted)"}\n${r.diff.stat || "(no changes)"}${r.diff.note ? `\n(${r.diff.note})` : ""}` : ""),
      ).join("\n\n");
      return {
        content: [{ type: "text" as const, text: `herdr_collect ${result.runId} (last ${result.lines} lines)\n\n${body}` }],
        details: result,
      };
    },
  });

  pi.registerTool({
    name: "herdr_merge",
    label: "Herdr merge",
    description:
      `${optIn} Mechanically integrate finished worker workspaces: jj new w1 w2 ... in ~/.herdr-ws/<run>/integration ` +
      "(git: sequential merges). Clean: seal the change and mark panes merged (panes and workspaces stay alive). Conflict: " +
      "spawn one worker fixer (model: fixerModel, else the worker role's pinned model) in the integration workspace; after it finishes, call herdr_merge with no workers to re-check.",
    executionMode: "sequential",
    parameters: Type.Object({
      runId,
      workers: names,
      spawnFixer: Type.Optional(Type.Boolean()),
      fixerModel: model,
    }, { additionalProperties: false }),
    async execute(_id, params, signal, _update, ctx) {
      const result = await collab.merge(params, call(ctx, signal));
      return text(`herdr_merge ${result.runId}: ${result.status}`, result);
    },
  });

  pi.registerTool({
    name: "herdr_cleanup",
    label: "Herdr cleanup",
    description:
      `${optIn} Close run panes (never the parent pane). With removeWorkspaces, delete workspaces of closed panes only, ` +
      "after a fresh snapshot; a workspace edited since its merge (or a dirty git worktree) is kept and its merge status cleared.",
    executionMode: "sequential",
    parameters: Type.Object({
      runId,
      names,
      removeWorkspaces: Type.Optional(Type.Boolean()),
    }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const result = await collab.cleanup(params, signal);
      return text(`herdr_cleanup ${result.runId}: closed [${result.closed.join(", ")}], removed [${result.removed.join(", ")}]`, result);
    },
  });

  pi.registerTool({
    name: "herdr_peers",
    label: "Herdr peers",
    description: `${optIn} List the run's panes with live state and the full message log.`,
    executionMode: "sequential",
    parameters: Type.Object({ runId }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const id = params.runId ?? collab.currentRunId;
      if (!id) throw new Error("herdr_peers: no current run; pass runId.");
      const result = await collab.peers({ runId: id, name: PARENT_NAME }, signal);
      return text(`herdr run ${result.runId}: ${result.peers.length} panes, ${result.messages.length} messages`, result);
    },
  });

  pi.registerTool({
    name: "herdr_send",
    label: "Herdr send",
    description:
      `${optIn} Send a message to a run pane (reviewers included, e.g. to ask for a repeated verdict). ` +
      "Delivered only when the target is idle; bounded wait.",
    executionMode: "sequential",
    parameters: Type.Object({ runId, ...sendParams }, { additionalProperties: false }),
    async execute(_id, params, signal) {
      const id = params.runId ?? collab.currentRunId;
      if (!id) throw new Error("herdr_send: no current run; pass runId.");
      const result = await collab.send({ runId: id, name: PARENT_NAME }, params, signal);
      return text(`delivered ${result.message.id} to ${params.to}`, result);
    },
  });
}
