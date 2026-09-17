// Finalize or block an executing Kanban workflow when no more work can run.
//
// args:
// {
//   expectedRevision: number
// }
//
// This workflow launches no agents. It only derives workflow-level state from
// durable task state and persists a transition when completion or blocking is
// unambiguous.

const expectedRevision = args.expectedRevision;

if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

function findTask(board, id) {
  return board.tasks.find((task) => task.id === id);
}

function dependenciesDone(board, task) {
  return task.dependsOn.every((dependencyId) => {
    const dependency = findTask(board, dependencyId);
    return dependency?.status === "done" || dependency?.status === "cancelled";
  });
}

function isTerminal(task) {
  return task.status === "done" || task.status === "cancelled";
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

const unfinished = current.tasks.filter((task) => !isTerminal(task));

if (unfinished.length === 0) {
  const next = JSON.parse(JSON.stringify(current));
  const at = new Date().toISOString();

  next.workflow.state = "completed";
  next.revision += 1;
  next.updatedAt = at;

  await state.set("kanban", next);

  return {
    status: "completed",
    revision: next.revision,
    completedTaskIds: current.tasks
      .filter((task) => task.status === "done")
      .map((task) => task.id),
    cancelledTaskIds: current.tasks
      .filter((task) => task.status === "cancelled")
      .map((task) => task.id),
  };
}

const working = unfinished.filter((task) => task.status === "working");

if (working.length > 0) {
  return {
    status: "running",
    reason: "working-tasks",
    revision: current.revision,
    taskIds: working.map((task) => task.id),
  };
}

const ready = unfinished.filter(
  (task) => task.status === "todo" && dependenciesDone(current, task),
);

if (ready.length > 0) {
  return {
    status: "running",
    reason: "ready-tasks",
    revision: current.revision,
    taskIds: ready.map((task) => task.id),
  };
}

const blocked = unfinished.filter((task) => task.status === "blocked");

if (blocked.length > 0) {
  const next = JSON.parse(JSON.stringify(current));
  const at = new Date().toISOString();

  next.workflow.state = "blocked";
  next.revision += 1;
  next.updatedAt = at;

  await state.set("kanban", next);

  return {
    status: "blocked",
    revision: next.revision,
    blockers: blocked.map((task) => ({
      taskId: task.id,
      kind: task.blocker?.kind ?? "unknown",
      reason: task.blocker?.reason ?? "Task is blocked without blocker metadata.",
      taskIds: Array.isArray(task.blocker?.taskIds)
        ? [...task.blocker.taskIds]
        : [],
    })),
  };
}

// At this point there are unfinished tasks, but none are working, ready, or
// explicitly blocked. Persisting completed/blocked would hide a broken state
// machine, so fail loudly and let reconciliation repair the invariant.
throw new Error(
  `kanban is stalled with unfinished tasks and no runnable or blocked state: ${unfinished
    .map((task) => `${task.id}:${task.status}/${task.phase}`)
    .join(", ")}`,
);
