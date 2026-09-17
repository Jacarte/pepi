// Convert one failed worker launch/run into an explicit infrastructure blocker.
// args: { expectedRevision: number, taskId: string, reason?: string }

const expectedRevision = args.expectedRevision;
const taskId = typeof args.taskId === "string" ? args.taskId.trim() : "";
const reason = typeof args.reason === "string" && args.reason.trim()
  ? args.reason.trim().slice(0, 1500)
  : "Worker execution failed before producing a durable successful handoff.";

if (!Number.isInteger(expectedRevision) || expectedRevision < 1) throw new Error("args.expectedRevision must be a positive integer");
if (!taskId) throw new Error("args.taskId is required");

const current = await state.get("kanban");
if (!current || typeof current !== "object") throw new Error("kanban state is not initialized");
if (current.revision !== expectedRevision) throw new Error(`stale kanban revision: expected ${expectedRevision}, got ${current.revision}`);
if (current.workflow?.state !== "executing") throw new Error("kanban workflow must be executing");

const task = current.tasks?.find((candidate) => candidate.id === taskId);
if (!task) throw new Error(`unknown task: ${taskId}`);
if (task.status !== "working" || !["implementation", "fix"].includes(task.phase)) {
  throw new Error(`task ${task.id} must be working in implementation/fix`);
}
if (!task.assignment || task.assignment.runId !== null) {
  throw new Error(`task ${task.id} must have an uncompleted worker assignment`);
}

const next = JSON.parse(JSON.stringify(current));
const nextTask = next.tasks.find((candidate) => candidate.id === taskId);
nextTask.status = "blocked";
nextTask.phase = "queued";
nextTask.assignment = null;
nextTask.blocker = { kind: "infrastructure", reason, taskIds: [] };
if (nextTask.result && typeof nextTask.result === "object") {
  const prior = typeof nextTask.result.summary === "string" ? nextTask.result.summary : "";
  nextTask.result.summary = `${prior}\n\nWorker infrastructure failure: ${reason}`.trim().slice(0, 2000);
}
next.revision += 1;
next.updatedAt = new Date().toISOString();
await state.set("kanban", next);

return { status: "worker-failure-blocked", revision: next.revision, taskId, blockerKind: "infrastructure" };
