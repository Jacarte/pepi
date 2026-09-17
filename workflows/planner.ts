// Structured task-DAG planner for confirmed complex work.
//
// args:
// {
//   task: string,
//   tier: "T2" | "T3",
//   riskSignals?: string[]
// }
//
// The oracle may inspect only the current repository rooted at cwd. It must not
// modify files. The workflow validates the returned dependency graph before
// returning it to any later Kanban/scheduler stage.

const task = typeof args.task === "string" ? args.task.trim() : "";
const tier = args.tier;
const riskSignals = Array.isArray(args.riskSignals) ? args.riskSignals : [];

if (!task) {
  throw new Error("args.task is required");
}

if (tier !== "T2" && tier !== "T3") {
  throw new Error("planner requires confirmed tier T2 or T3");
}

const taskSchema = {
  type: "object",
  properties: {
    id: {
      type: "string",
      pattern: "^[a-z0-9][a-z0-9-]{0,63}$"
    },
    title: {
      type: "string",
      minLength: 1,
      maxLength: 200
    },
    description: {
      type: "string",
      minLength: 1,
      maxLength: 2000
    },
    dependsOn: {
      type: "array",
      items: {
        type: "string",
        pattern: "^[a-z0-9][a-z0-9-]{0,63}$"
      },
      maxItems: 32,
      uniqueItems: true
    },
    paths: {
      type: "array",
      items: {
        type: "string",
        minLength: 1,
        maxLength: 512
      },
      maxItems: 64,
      uniqueItems: true
    },
    acceptance: {
      type: "array",
      items: {
        type: "string",
        minLength: 1,
        maxLength: 1000
      },
      minItems: 1,
      maxItems: 32
    },
    modifying: {
      type: "boolean"
    }
  },
  required: [
    "id",
    "title",
    "description",
    "dependsOn",
    "paths",
    "acceptance",
    "modifying"
  ],
  additionalProperties: false
};

const planSchema = {
  type: "object",
  properties: {
    summary: {
      type: "string",
      minLength: 1,
      maxLength: 2000
    },
    tasks: {
      type: "array",
      minItems: 1,
      maxItems: 32,
      items: taskSchema
    }
  },
  required: ["summary", "tasks"],
  additionalProperties: false
};

const result = await runs.run("planner", {
  agent: "oracle",
  context: "fresh",
  task: [
    "Create a dependency DAG for this confirmed complex engineering task.",
    "",
    "Repository boundary:",
    "- the current working directory is the repository root",
    "- inspect only files under cwd",
    "- never inspect parent directories, $HOME, ~, /Users, /home, /tmp, or filesystem root",
    "- never use .. to escape cwd",
    "- do not search for other repositories",
    "- do not modify files, run destructive commands, push, publish, or deploy",
    "",
    "Original task:",
    task,
    "",
    "Confirmed workflow tier:",
    tier,
    "",
    "Known risk signals:",
    riskSignals.length > 0 ? riskSignals.join(", ") : "none supplied",
    "",
    "Planning rules:",
    "- inspect the current repository enough to ground the task decomposition",
    "- create the smallest coherent independently executable tasks",
    "- each task must have one clear responsibility",
    "- make dependencies explicit and avoid artificial dependencies",
    "- do not create status/reporting-only tasks",
    "- identify likely repository-relative paths owned by each task",
    "- acceptance criteria must describe observable completion evidence",
    "- mark modifying=false only for genuinely read-only/non-mutating work",
    "- avoid assigning the same logical ownership boundary to independent modifying tasks",
    "- use stable kebab-case task IDs",
    "- return an acyclic graph",
    "",
    "Do not implement anything. Return only the structured plan."
  ].join("\n"),
  outputSchema: planSchema
});

if (!result || !result.structuredOutput) {
  throw new Error("planner returned no structured output");
}

const plan = result.structuredOutput;

if (!Array.isArray(plan.tasks) || plan.tasks.length === 0) {
  throw new Error("planner returned no tasks");
}

const tasksById = new Map();

for (const plannedTask of plan.tasks) {
  if (tasksById.has(plannedTask.id)) {
    throw new Error(`planner returned duplicate task id: ${plannedTask.id}`);
  }

  tasksById.set(plannedTask.id, plannedTask);
}

for (const plannedTask of plan.tasks) {
  const dependencies = new Set();

  for (const dependencyId of plannedTask.dependsOn) {
    if (dependencyId === plannedTask.id) {
      throw new Error(`planner task ${plannedTask.id} depends on itself`);
    }

    if (!tasksById.has(dependencyId)) {
      throw new Error(
        `planner task ${plannedTask.id} depends on unknown task ${dependencyId}`
      );
    }

    if (dependencies.has(dependencyId)) {
      throw new Error(
        `planner task ${plannedTask.id} repeats dependency ${dependencyId}`
      );
    }

    dependencies.add(dependencyId);
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
    const nextIndegree = indegree.get(dependentId) - 1;
    indegree.set(dependentId, nextIndegree);

    if (nextIndegree === 0) {
      queue.push(dependentId);
    }
  }
}

if (visited !== plan.tasks.length) {
  throw new Error("planner returned a cyclic task dependency graph");
}

return plan;
