// Prepare one software-engineering request for execution.
//
// args: { task: string, maxWorkers?: number }
//
// T0/T1/T1R are returned as simple routes without creating Kanban state.
// T2/T3 are optionally scouted, planned into a validated DAG, and persisted as
// a durable waiting_approval board. Approval/execution stay separate.

const request = typeof args.task === "string" ? args.task.trim() : "";
const maxWorkers = Number.isInteger(args.maxWorkers) ? args.maxWorkers : 3;
if (!request) throw new Error("args.task is required");
if (maxWorkers < 1 || maxWorkers > 16) throw new Error("args.maxWorkers must be from 1 to 16");

if (await state.get("kanban")) {
  throw new Error("kanban state already exists for this mission");
}

const classificationSchema = {
  type: "object",
  properties: {
    tier: { type: "string", enum: ["T0", "T1", "T1R", "T2", "T3"] },
    confidence: { type: "string", enum: ["low", "medium", "high"] },
    needsScout: { type: "boolean" },
    reason: { type: "string" },
    riskSignals: { type: "array", items: { type: "string" }, maxItems: 16 },
  },
  required: ["tier", "confidence", "needsScout", "reason", "riskSignals"],
  additionalProperties: false,
};

const triagePrompt = [
  "Classify this software-engineering request for workflow routing.",
  "Do NOT inspect the repository in this first step.",
  "Choose the cheapest safe tier justified by the request wording.",
  "Missing repository facts should produce needsScout=true rather than automatic promotion.",
  "T0 = answer-only; T1 = obvious reversible small edit; T1R = bounded repair of an existing failing check; T2 = coordinated feature/fix/refactor; T3 = migration/security/destructive/public-contract/deployment/high-risk work.",
  "Request:",
  request,
].join("\n\n");

const initialRun = await runs.run("prepare-triage", {
  agent: "oracle",
  context: "fresh",
  task: triagePrompt,
  outputSchema: classificationSchema,
});
if (!initialRun?.structuredOutput) throw new Error("initial triage returned no structured output");
const initial = initialRun.structuredOutput;
let confirmed = initial;
let scoutUsed = false;

if (initial.needsScout) {
  const scout = await runs.run("prepare-scout", {
    agent: "scout",
    context: "fresh",
    task: [
      "Inspect only this repository to gather the minimum evidence needed to classify the request.",
      "Stay inside workflow cwd. Do not traverse parents, $HOME, /Users, /home, /tmp, filesystem root, or other repositories.",
      "Read-only inspection only. Summarize concrete scope/risk evidence; do not implement.",
      "Request:", request,
      "Initial classification:", JSON.stringify(initial),
    ].join("\n\n"),
  });
  if (!scout || scout.ok === false || typeof scout.output !== "string" || !scout.output.trim()) {
    throw new Error("scout returned no usable evidence");
  }
  scoutUsed = true;

  const confirm = await runs.run("prepare-confirm", {
    agent: "oracle",
    context: "fresh",
    task: [
      "Confirm or revise the workflow tier using only the request and supplied repository evidence.",
      "Do not inspect the repository again.",
      "Promote only when evidence demonstrates broader/riskier scope; demote when evidence proves narrower scope.",
      "If material evidence is still missing, keep needsScout=true and state the exact gap; do not loop.",
      "Request:", request,
      "Initial:", JSON.stringify(initial),
      "Repository evidence:", scout.output,
    ].join("\n\n"),
    outputSchema: classificationSchema,
  });
  if (!confirm?.structuredOutput) throw new Error("confirmed triage returned no structured output");
  confirmed = confirm.structuredOutput;
}

if (!["T2", "T3"].includes(confirmed.tier)) {
  return {
    status: "simple-route",
    kanbanCreated: false,
    initial,
    confirmed,
    scoutUsed,
  };
}

if (confirmed.needsScout) {
  throw new Error(`complex classification still needs repository evidence: ${confirmed.reason}`);
}

const planSchema = {
  type: "object",
  properties: {
    summary: { type: "string" },
    tasks: {
      type: "array",
      minItems: 1,
      maxItems: 64,
      items: {
        type: "object",
        properties: {
          id: { type: "string", pattern: "^[a-z0-9][a-z0-9-]{0,63}$" },
          title: { type: "string" },
          description: { type: "string" },
          dependsOn: { type: "array", items: { type: "string" }, maxItems: 32 },
          paths: { type: "array", items: { type: "string" }, maxItems: 64 },
          acceptance: { type: "array", items: { type: "string" }, maxItems: 32 },
          modifying: { type: "boolean" },
        },
        required: ["id", "title", "description", "dependsOn", "paths", "acceptance", "modifying"],
        additionalProperties: false,
      },
    },
  },
  required: ["summary", "tasks"],
  additionalProperties: false,
};

const planned = await runs.run("prepare-plan", {
  agent: "oracle",
  context: "fresh",
  task: [
    "Create the smallest coherent dependency DAG for this confirmed complex request.",
    "You may inspect the repository read-only inside cwd. Never edit files or leave the repository.",
    "Each task must have one responsibility, explicit real dependencies, repository-relative path ownership hints, observable acceptance criteria, and a modifying boolean.",
    "Avoid overlapping modifying ownership between independent tasks. Do not artificially serialize unrelated work.",
    "Request:", request,
    "Confirmed classification:", JSON.stringify(confirmed),
  ].join("\n\n"),
  outputSchema: planSchema,
});
if (!planned?.structuredOutput) throw new Error("planner returned no structured output");
const plan = planned.structuredOutput;

const byId = new Map();
for (const task of plan.tasks) {
  if (byId.has(task.id)) throw new Error(`duplicate task id: ${task.id}`);
  byId.set(task.id, task);
  if (new Set(task.dependsOn).size !== task.dependsOn.length) throw new Error(`task ${task.id} repeats a dependency`);
}
for (const task of plan.tasks) {
  for (const dep of task.dependsOn) {
    if (dep === task.id) throw new Error(`task ${task.id} depends on itself`);
    if (!byId.has(dep)) throw new Error(`task ${task.id} depends on unknown task ${dep}`);
  }
}
const visiting = new Set();
const visited = new Set();
function visit(id) {
  if (visiting.has(id)) throw new Error("task dependency graph contains a cycle");
  if (visited.has(id)) return;
  visiting.add(id);
  for (const dep of byId.get(id).dependsOn) visit(dep);
  visiting.delete(id);
  visited.add(id);
}
for (const id of byId.keys()) visit(id);

const now = new Date().toISOString();
const board = {
  schemaVersion: 1,
  revision: 1,
  workflow: {
    goal: request,
    tier: confirmed.tier,
    state: "waiting_approval",
    classification: {
      initialTier: initial.tier,
      confirmedTier: confirmed.tier,
      confidence: confirmed.confidence,
      reason: confirmed.reason,
      riskSignals: [...confirmed.riskSignals],
    },
  },
  scheduler: { maxWorkers },
  tasks: plan.tasks.map((task) => ({
    id: task.id,
    title: task.title,
    description: task.description,
    status: task.dependsOn.length === 0 ? "todo" : "blocked",
    phase: "queued",
    dependsOn: [...task.dependsOn],
    paths: [...task.paths],
    acceptance: [...task.acceptance],
    modifying: task.modifying,
    attempts: 0,
    assignment: null,
    blocker: task.dependsOn.length === 0
      ? null
      : { kind: "dependency", reason: "Waiting for dependency tasks to complete.", taskIds: [...task.dependsOn] },
    result: null,
  })),
  createdAt: now,
  updatedAt: now,
};

await state.set("kanban", board);
return {
  status: "waiting-approval",
  kanbanCreated: true,
  revision: 1,
  initial,
  confirmed,
  scoutUsed,
  planSummary: plan.summary,
  taskCount: board.tasks.length,
  maxWorkers,
};
