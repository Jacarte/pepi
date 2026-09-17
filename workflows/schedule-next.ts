// Claim ready Kanban tasks up to configured worker capacity.
//
// args:
// {
//   expectedRevision: number
// }
//
// This workflow persists assignments only. It does not launch workers.
// Worker capacity counts only tasks whose current phase actively requires a
// worker process (`implementation` or `fix`), not tasks waiting in verification
// or review.

const expectedRevision = args.expectedRevision;

if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

const current = await state.get("kanban");

if (!current || typeof current !== "object") {
  throw new Error("kanban state is not initialized");
}

if (!Number.isInteger(current.revision)) {
  throw new Error("kanban revision is invalid");
}

if (current.revision !== expectedRevision) {
  throw new Error(
    `stale kanban revision: expected ${expectedRevision}, got ${current.revision}`,
  );
}

if (current.workflow?.state !== "executing") {
  throw new Error(
    `kanban workflow must be executing; got ${current.workflow?.state ?? "missing"}`,
  );
}

if (!Array.isArray(current.tasks)) {
  throw new Error("kanban tasks are invalid");
}

const maxWorkers = current.scheduler?.maxWorkers;

if (!Number.isInteger(maxWorkers) || maxWorkers < 1 || maxWorkers > 16) {
  throw new Error("kanban scheduler.maxWorkers must be an integer from 1 to 16");
}

function taskById(board, id) {
  return board.tasks.find((task) => task.id === id);
}

function dependenciesDone(board, task) {
  return task.dependsOn.every((dependencyId) => {
    const dependency = taskById(board, dependencyId);
    return dependency?.status === "done";
  });
}

function isWorkerActive(task) {
  return (
    task.status === "working" &&
    (task.phase === "implementation" || task.phase === "fix")
  );
}

const workerActive = current.tasks.filter(isWorkerActive);

if (workerActive.length > maxWorkers) {
  throw new Error(
    `active worker count ${workerActive.length} exceeds scheduler.maxWorkers ${maxWorkers}`,
  );
}

const availableCapacity = maxWorkers - workerActive.length;

if (availableCapacity === 0) {
  return {
    status: "busy",
    revision: current.revision,
    maxWorkers,
    availableCapacity: 0,
    workerActiveTaskIds: workerActive.map((task) => task.id),
  };
}

const ready = current.tasks
  .filter((task) => task.status === "todo" && dependenciesDone(current, task))
  .slice(0, availableCapacity);

if (ready.length === 0) {
  const unfinished = current.tasks.filter(
    (task) => task.status !== "done" && task.status !== "cancelled",
  );

  return {
    status: "idle",
    reason: unfinished.length === 0 ? "no-unfinished-tasks" : "no-ready-task",
    revision: current.revision,
    maxWorkers,
    availableCapacity,
    workerActiveTaskIds: workerActive.map((task) => task.id),
  };
}

const next = JSON.parse(JSON.stringify(current));
const startedAt = new Date().toISOString();
const claims = [];

for (const readyTask of ready) {
  const task = taskById(next, readyTask.id);
  const attempt = task.attempts + 1;
  const workerKey = `worker-${task.id}-${attempt}`;

  task.status = "working";
  task.phase = "implementation";
  task.attempts = attempt;
  task.assignment = {
    workerKey,
    runId: null,
    attempt,
    startedAt,
  };
  task.blocker = null;
  task.result = null;

  claims.push({
    taskId: task.id,
    workerKey,
    attempt,
    task: {
      id: task.id,
      title: task.title,
      description: task.description,
      paths: [...task.paths],
      acceptance: [...task.acceptance],
      modifying: task.modifying,
    },
  });
}

next.revision += 1;
next.updatedAt = startedAt;

await state.set("kanban", next);

return {
  status: "claimed",
  revision: next.revision,
  maxWorkers,
  availableBeforeClaim: availableCapacity,
  claims,
  workerActiveTaskIds: [
    ...workerActive.map((task) => task.id),
    ...claims.map((claim) => claim.taskId),
  ],
};
