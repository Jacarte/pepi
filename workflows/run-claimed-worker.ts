// Launch the worker for one already-claimed Kanban task.
//
// args:
// {
//   expectedRevision: number,
//   taskId: string
// }
//
// The scheduler must claim the task first. This workflow awaits the worker,
// records bounded execution metadata, and advances the task to verification.
// It does not perform verification or review.

const expectedRevision = args.expectedRevision;
const taskId = typeof args.taskId === "string" ? args.taskId.trim() : "";

if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

if (!taskId) {
  throw new Error("args.taskId is required");
}

function findTask(board, id) {
  return board.tasks.find((task) => task.id === id);
}

function requireClaimedTask(board) {
  const task = findTask(board, taskId);

  if (!task) {
    throw new Error(`unknown task: ${taskId}`);
  }

  if (task.status !== "working") {
    throw new Error(`task ${task.id} must be working; got ${task.status}`);
  }

  if (task.phase !== "implementation") {
    throw new Error(
      `task ${task.id} phase must be implementation; got ${task.phase}`,
    );
  }

  if (!task.assignment || typeof task.assignment !== "object") {
    throw new Error(`task ${task.id} has no assignment`);
  }

  if (
    typeof task.assignment.workerKey !== "string" ||
    task.assignment.workerKey.length === 0
  ) {
    throw new Error(`task ${task.id} assignment workerKey is invalid`);
  }

  if (task.assignment.runId !== null) {
    throw new Error(`task ${task.id} worker has already been launched`);
  }

  if (task.assignment.attempt !== task.attempts) {
    throw new Error(`task ${task.id} assignment attempt is inconsistent`);
  }

  return task;
}

function requireBoard(board) {
  if (!board || typeof board !== "object") {
    throw new Error("kanban state is not initialized");
  }

  if (!Number.isInteger(board.revision)) {
    throw new Error("kanban revision is invalid");
  }

  if (board.revision !== expectedRevision) {
    throw new Error(
      `stale kanban revision: expected ${expectedRevision}, got ${board.revision}`,
    );
  }

  if (board.workflow?.state !== "executing") {
    throw new Error(
      `kanban workflow must be executing; got ${board.workflow?.state ?? "missing"}`,
    );
  }

  if (!Array.isArray(board.tasks)) {
    throw new Error("kanban tasks are invalid");
  }
}

function boundedSummary(output) {
  const text = typeof output === "string" ? output.trim() : "";
  const summary = text || "Worker completed the claimed task.";
  return summary.slice(0, 2000);
}

function firstArtifactPath(result) {
  if (!Array.isArray(result.artifactPaths)) {
    return null;
  }

  const path = result.artifactPaths.find(
    (value) => typeof value === "string" && value.length > 0,
  );

  return path ?? null;
}

const current = await state.get("kanban");
requireBoard(current);
const claimed = requireClaimedTask(current);

const expectedPaths =
  claimed.paths.length > 0
    ? claimed.paths.map((path) => `- ${path}`).join("\n")
    : "- no path hints supplied";

const acceptance =
  claimed.acceptance.length > 0
    ? claimed.acceptance.map((criterion) => `- ${criterion}`).join("\n")
    : "- no explicit acceptance criteria supplied";

const modeInstructions = claimed.modifying
  ? [
      "This is a modifying task.",
      "Implement only this task in the managed isolated worktree provided to you.",
      "Make the smallest coherent change that satisfies the acceptance criteria.",
      "Run relevant focused checks before returning.",
    ].join("\n")
  : [
      "This is a non-modifying task.",
      "Do not edit, create, delete, or rewrite repository files.",
      "Inspect and report only what is required by the task and acceptance criteria.",
    ].join("\n");

const workerTask = [
  "Execute this already-approved Kanban task.",
  "",
  `Task ID: ${claimed.id}`,
  `Title: ${claimed.title}`,
  "",
  "Description:",
  claimed.description,
  "",
  "Expected repository-relative paths:",
  expectedPaths,
  "",
  "Acceptance criteria:",
  acceptance,
  "",
  modeInstructions,
  "",
  "Repository boundary:",
  "- the workflow cwd is the repository root",
  "- stay inside that repository/worktree",
  "- do not traverse parent directories or search other repositories",
  "- do not access $HOME, ~, /Users, /home, /tmp, or filesystem root unless the task explicitly requires a repository-owned path there",
  "- do not push, publish, or move remote-facing refs",
  "",
  "Scope control:",
  "- paths are ownership hints, not permission to broaden the task",
  "- preserve unrelated work",
  "- if repository evidence shows a materially larger or risky change is required, stop and report the blocker instead of expanding scope",
  "",
  "Return a concise implementation summary and the checks you ran.",
].join("\n");

const worker = await runs.run(claimed.assignment.workerKey, {
  agent: "worker",
  context: "fresh",
  task: workerTask,
  worktree: claimed.modifying === true,
});

if (!worker || worker.ok === false) {
  throw new Error(`worker ${claimed.assignment.workerKey} failed`);
}

if (typeof worker.runId !== "string" || worker.runId.length === 0) {
  throw new Error(`worker ${claimed.assignment.workerKey} returned no runId`);
}

// Re-read after the potentially long-running child. Never overwrite a board that
// changed while the worker was running.
const latest = await state.get("kanban");
requireBoard(latest);
const latestTask = requireClaimedTask(latest);

if (
  latestTask.assignment.workerKey !== claimed.assignment.workerKey ||
  latestTask.assignment.attempt !== claimed.assignment.attempt
) {
  throw new Error(`task ${taskId} assignment changed while worker was running`);
}

const next = JSON.parse(JSON.stringify(latest));
const nextTask = findTask(next, taskId);
const outputReference = firstArtifactPath(worker);
const summary = boundedSummary(worker.output);
const completedAt = new Date().toISOString();

nextTask.assignment.runId = worker.runId;
nextTask.phase = "verification";
nextTask.result = {
  summary,
  verification: "pending",
  review: "pending",
  runId: worker.runId,
  outputReference,
};

next.revision += 1;
next.updatedAt = completedAt;

await state.set("kanban", next);

return {
  status: "worker-complete",
  revision: next.revision,
  taskId: nextTask.id,
  workerKey: nextTask.assignment.workerKey,
  runId: worker.runId,
  phase: nextTask.phase,
  summary,
  outputReference,
};
