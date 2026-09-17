// Initialize durable Kanban mission state from confirmed triage and a validated plan.
//
// args:
// {
//   task: string,
//   triage: {
//     initial: { tier, confidence, needsScout, reason, riskSignals },
//     confirmed: { tier, confidence, needsScout, reason, riskSignals },
//     scoutUsed: boolean
//   },
//   plan: {
//     summary: string,
//     tasks: [{ id, title, description, dependsOn, paths, acceptance, modifying }]
//   },
//   maxWorkers?: number
// }

const task = typeof args.task === "string" ? args.task.trim() : "";
if (!task) {
  throw new Error("args.task is required");
}

const triage = args.triage;
if (!triage || !triage.initial || !triage.confirmed) {
  throw new Error("args.triage with initial and confirmed classifications is required");
}

const confirmedTier = triage.confirmed.tier;
if (confirmedTier !== "T2" && confirmedTier !== "T3") {
  throw new Error("Kanban initialization requires confirmed T2 or T3 work");
}

const maxWorkers = args.maxWorkers === undefined ? 3 : args.maxWorkers;
if (!Number.isInteger(maxWorkers) || maxWorkers < 1 || maxWorkers > 16) {
  throw new Error("args.maxWorkers must be an integer between 1 and 16");
}

const plan = args.plan;
if (!plan || !Array.isArray(plan.tasks) || plan.tasks.length === 0) {
  throw new Error("args.plan with at least one task is required");
}

// Defensively re-check the DAG before persisting it. The planner already validates
// this, but init-kanban may also be invoked directly.
const tasksById = new Map();
for (const plannedTask of plan.tasks) {
  if (!plannedTask || typeof plannedTask.id !== "string" || !plannedTask.id) {
    throw new Error("every planned task requires a non-empty id");
  }
  if (tasksById.has(plannedTask.id)) {
    throw new Error(`duplicate task id: ${plannedTask.id}`);
  }
  tasksById.set(plannedTask.id, plannedTask);
}

for (const plannedTask of plan.tasks) {
  if (!Array.isArray(plannedTask.dependsOn)) {
    throw new Error(`task ${plannedTask.id} requires dependsOn`);
  }
  for (const dependencyId of plannedTask.dependsOn) {
    if (dependencyId === plannedTask.id) {
      throw new Error(`task ${plannedTask.id} depends on itself`);
    }
    if (!tasksById.has(dependencyId)) {
      throw new Error(`task ${plannedTask.id} depends on unknown task ${dependencyId}`);
    }
  }
}

const indegree = new Map(plan.tasks.map((plannedTask) => [plannedTask.id, 0]));
const dependents = new Map(plan.tasks.map((plannedTask) => [plannedTask.id, []]));
for (const plannedTask of plan.tasks) {
  for (const dependencyId of plannedTask.dependsOn) {
    indegree.set(plannedTask.id, indegree.get(plannedTask.id) + 1);
    dependents.get(dependencyId).push(plannedTask.id);
  }
}

const queue = plan.tasks
  .filter((plannedTask) => indegree.get(plannedTask.id) === 0)
  .map((plannedTask) => plannedTask.id);
let visited = 0;
while (queue.length > 0) {
  const current = queue.shift();
  visited += 1;
  for (const dependentId of dependents.get(current)) {
    const next = indegree.get(dependentId) - 1;
    indegree.set(dependentId, next);
    if (next === 0) {
      queue.push(dependentId);
    }
  }
}
if (visited !== plan.tasks.length) {
  throw new Error("task dependency graph contains a cycle");
}

const existing = await state.get("kanban");
if (existing !== undefined) {
  throw new Error("kanban mission state is already initialized");
}

const timestamp = new Date().toISOString();
const board = {
  schemaVersion: 1,
  revision: 1,
  workflow: {
    goal: task,
    tier: confirmedTier,
    state: "waiting_approval",
    classification: {
      initialTier: triage.initial.tier,
      confirmedTier,
      confidence: triage.confirmed.confidence,
      reason: triage.confirmed.reason,
      riskSignals: Array.isArray(triage.confirmed.riskSignals)
        ? [...triage.confirmed.riskSignals]
        : []
    }
  },
  scheduler: {
    maxWorkers
  },
  tasks: plan.tasks.map((plannedTask) => {
    const dependencies = [...plannedTask.dependsOn];
    const blocked = dependencies.length > 0;
    return {
      id: plannedTask.id,
      title: plannedTask.title,
      description: plannedTask.description,
      status: blocked ? "blocked" : "todo",
      phase: "queued",
      dependsOn: dependencies,
      paths: Array.isArray(plannedTask.paths) ? [...plannedTask.paths] : [],
      acceptance: Array.isArray(plannedTask.acceptance) ? [...plannedTask.acceptance] : [],
      modifying: Boolean(plannedTask.modifying),
      attempts: 0,
      assignment: null,
      blocker: blocked
        ? {
            kind: "dependency",
            reason: `Waiting for dependencies: ${dependencies.join(", ")}`,
            taskIds: dependencies
          }
        : null,
      result: null
    };
  }),
  createdAt: timestamp,
  updatedAt: timestamp
};

await state.set("kanban", board);
return board;
