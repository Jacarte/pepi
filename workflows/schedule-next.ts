// Claim ready Kanban tasks up to configured worker capacity.
//
// args: { expectedRevision: number }
//
// The scheduler is the sole assignment authority. Worker capacity counts only
// implementation/fix phases. Parallel modifying work is additionally admitted
// only when declared path ownership does not overlap.

const expectedRevision = args.expectedRevision;
if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

const current = await state.get("kanban");
if (!current || typeof current !== "object") throw new Error("kanban state is not initialized");
if (!Number.isInteger(current.revision)) throw new Error("kanban revision is invalid");
if (current.revision !== expectedRevision) {
  throw new Error(`stale kanban revision: expected ${expectedRevision}, got ${current.revision}`);
}
if (current.workflow?.state !== "executing") {
  throw new Error(`kanban workflow must be executing; got ${current.workflow?.state ?? "missing"}`);
}
if (!Array.isArray(current.tasks)) throw new Error("kanban tasks are invalid");

const maxWorkers = current.scheduler?.maxWorkers;
if (!Number.isInteger(maxWorkers) || maxWorkers < 1 || maxWorkers > 16) {
  throw new Error("kanban scheduler.maxWorkers must be an integer from 1 to 16");
}

function taskById(board, id) {
  return board.tasks.find((task) => task.id === id);
}
function dependenciesDone(board, task) {
  return task.dependsOn.every((dependencyId) => taskById(board, dependencyId)?.status === "done");
}
function isWorkerActive(task) {
  return task.status === "working" && (task.phase === "implementation" || task.phase === "fix");
}
function normalizePath(value) {
  return String(value).replace(/\\/g, "/").replace(/^\.\//, "").replace(/\/+$/, "");
}
function pathsOverlap(left, right) {
  const a = normalizePath(left);
  const b = normalizePath(right);
  return a === b || a.startsWith(`${b}/`) || b.startsWith(`${a}/`);
}
function modifyingTasksConflict(left, right) {
  if (left.modifying !== true || right.modifying !== true) return false;
  if (!Array.isArray(left.paths) || left.paths.length === 0) return true;
  if (!Array.isArray(right.paths) || right.paths.length === 0) return true;
  return left.paths.some((a) => right.paths.some((b) => pathsOverlap(a, b)));
}

const workerActive = current.tasks.filter(isWorkerActive);
if (workerActive.length > maxWorkers) {
  throw new Error(`active worker count ${workerActive.length} exceeds scheduler.maxWorkers ${maxWorkers}`);
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

const allReady = current.tasks.filter(
  (task) => task.status === "todo" && dependenciesDone(current, task),
);
const selected = [];
const deferredConflictTaskIds = [];
const ownership = [...workerActive];

for (const candidate of allReady) {
  if (selected.length >= availableCapacity) break;
  const conflicts = ownership.some((running) => modifyingTasksConflict(candidate, running));
  if (conflicts) {
    deferredConflictTaskIds.push(candidate.id);
    continue;
  }
  selected.push(candidate);
  ownership.push(candidate);
}

if (selected.length === 0) {
  const unfinished = current.tasks.filter(
    (task) => task.status !== "done" && task.status !== "cancelled",
  );
  const response = {
    status: "idle",
    reason:
      allReady.length > 0 && deferredConflictTaskIds.length > 0
        ? "path-conflict"
        : unfinished.length === 0
          ? "no-unfinished-tasks"
          : "no-ready-task",
    revision: current.revision,
    maxWorkers,
    availableCapacity,
    workerActiveTaskIds: workerActive.map((task) => task.id),
  };
  if (deferredConflictTaskIds.length > 0) {
    response.deferredConflictTaskIds = deferredConflictTaskIds;
  }
  return response;
}

const next = JSON.parse(JSON.stringify(current));
const startedAt = new Date().toISOString();
const claims = [];
for (const readyTask of selected) {
  const task = taskById(next, readyTask.id);
  const attempt = task.attempts + 1;
  const workerKey = `worker-${task.id}-${attempt}`;
  task.status = "working";
  task.phase = "implementation";
  task.attempts = attempt;
  task.assignment = { workerKey, runId: null, attempt, startedAt };
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

const response = {
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
if (deferredConflictTaskIds.length > 0) {
  response.deferredConflictTaskIds = deferredConflictTaskIds;
}
return response;
