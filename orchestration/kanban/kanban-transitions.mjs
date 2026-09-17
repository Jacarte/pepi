import {
  dependenciesDone,
  refreshDependencyBlockers,
  taskById,
} from "./kanban-model.mjs";

const BLOCKER_KINDS = new Set([
  "dependency",
  "human_decision",
  "technical",
  "infrastructure",
  "integration",
]);

function requireTask(board, taskId) {
  const task = taskById(board, taskId);

  if (!task) {
    throw new Error(`unknown task: ${taskId}`);
  }

  return task;
}

function requireStatus(task, ...allowed) {
  if (!allowed.includes(task.status)) {
    throw new Error(
      `task ${task.id} must be ${allowed.join(" or ")}; got ${task.status}`,
    );
  }
}

function requirePhase(task, ...allowed) {
  if (!allowed.includes(task.phase)) {
    throw new Error(
      `task ${task.id} phase must be ${allowed.join(" or ")}; got ${task.phase}`,
    );
  }
}

function timestamp(at) {
  return at ?? new Date().toISOString();
}

function touch(board, at) {
  board.revision += 1;
  board.updatedAt = timestamp(at);
  return board;
}

function validateBlocker(blocker) {
  if (!blocker || !BLOCKER_KINDS.has(blocker.kind)) {
    throw new Error("blocker.kind is invalid");
  }

  if (typeof blocker.reason !== "string" || blocker.reason.length === 0) {
    throw new Error("blocker.reason is required");
  }

  if (!Array.isArray(blocker.taskIds)) {
    throw new Error("blocker.taskIds must be an array");
  }
}

function validateCompletionResult(result) {
  if (!result || typeof result.summary !== "string" || result.summary.length === 0) {
    throw new Error("completion result summary is required");
  }

  if (result.verification !== "pass") {
    throw new Error("task cannot complete without passing verification");
  }

  if (result.review !== "pass") {
    throw new Error("task cannot complete without passing review");
  }
}

export function startTask(
  board,
  taskId,
  { workerKey, runId = null, at } = {},
) {
  const task = requireTask(board, taskId);
  requireStatus(task, "todo");

  if (!dependenciesDone(board, task)) {
    throw new Error(`task ${task.id} has unfinished dependencies`);
  }

  if (typeof workerKey !== "string" || workerKey.length === 0) {
    throw new Error("workerKey is required");
  }

  const startedAt = timestamp(at);
  const attempt = task.attempts + 1;

  task.status = "working";
  task.phase = "implementation";
  task.attempts = attempt;
  task.assignment = {
    workerKey,
    runId,
    attempt,
    startedAt,
  };
  task.blocker = null;
  task.result = null;

  return touch(board, startedAt);
}

export function beginVerification(board, taskId, { at } = {}) {
  const task = requireTask(board, taskId);
  requireStatus(task, "working");
  requirePhase(task, "implementation", "fix", "integration");

  task.phase = "verification";
  return touch(board, at);
}

export function beginReview(board, taskId, { at } = {}) {
  const task = requireTask(board, taskId);
  requireStatus(task, "working");
  requirePhase(task, "verification");

  task.phase = "review";
  return touch(board, at);
}

export function beginFix(board, taskId, { at } = {}) {
  const task = requireTask(board, taskId);
  requireStatus(task, "working");
  requirePhase(task, "verification", "review");

  task.phase = "fix";
  return touch(board, at);
}

export function blockTask(board, taskId, blocker, { at } = {}) {
  const task = requireTask(board, taskId);

  if (task.status === "done" || task.status === "cancelled") {
    throw new Error(`terminal task ${task.id} cannot be blocked`);
  }

  validateBlocker(blocker);

  task.status = "blocked";
  task.phase = "queued";
  task.assignment = null;
  task.blocker = {
    kind: blocker.kind,
    reason: blocker.reason,
    taskIds: [...blocker.taskIds],
  };

  return touch(board, at);
}

export function requeueTask(board, taskId, { at } = {}) {
  const task = requireTask(board, taskId);
  requireStatus(task, "blocked");

  if (task.blocker?.kind === "dependency" && !dependenciesDone(board, task)) {
    throw new Error(`task ${task.id} still has unfinished dependencies`);
  }

  task.status = "todo";
  task.phase = "queued";
  task.assignment = null;
  task.blocker = null;

  return touch(board, at);
}

export function completeTask(board, taskId, result, { at } = {}) {
  const task = requireTask(board, taskId);
  requireStatus(task, "working");
  requirePhase(task, "review");
  validateCompletionResult(result);

  task.status = "done";
  task.phase = "complete";
  task.assignment = null;
  task.blocker = null;
  task.result = {
    summary: result.summary,
    verification: result.verification,
    review: result.review,
    runId: result.runId ?? null,
    outputReference: result.outputReference ?? null,
  };

  refreshDependencyBlockers(board);
  return touch(board, at);
}
