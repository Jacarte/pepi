// Claim at most one ready Kanban task. This v1 scheduler is intentionally serial.
//
// args:
// {
//   expectedRevision: number
// }
//
// It does not launch a worker. It only persists the task claim that a later
// worker-launch workflow will consume.

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

function taskById(board, id) {
  return board.tasks.find((task) => task.id === id);
}

function dependenciesDone(board, task) {
  return task.dependsOn.every((dependencyId) => {
    const dependency = taskById(board, dependencyId);
    return dependency?.status === "done";
  });
}

const active = current.tasks.filter((task) => task.status === "working");

// Serial scheduler v1: never claim a second task while one is active, even if
// scheduler.maxWorkers is configured above 1 for a future parallel scheduler.
if (active.length > 0) {
  return {
    status: "busy",
    revision: current.revision,
    activeTaskIds: active.map((task) => task.id),
  };
}

const ready = current.tasks.find(
  (task) => task.status === "todo" && dependenciesDone(current, task),
);

if (!ready) {
  const unfinished = current.tasks.filter(
    (task) => task.status !== "done" && task.status !== "cancelled",
  );

  return {
    status: "idle",
    reason: unfinished.length === 0 ? "no-unfinished-tasks" : "no-ready-task",
    revision: current.revision,
  };
}

const next = JSON.parse(JSON.stringify(current));
const task = taskById(next, ready.id);
const attempt = task.attempts + 1;
const workerKey = `worker-${task.id}-${attempt}`;
const startedAt = new Date().toISOString();

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

next.revision += 1;
next.updatedAt = startedAt;

await state.set("kanban", next);

return {
  status: "claimed",
  revision: next.revision,
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
};
