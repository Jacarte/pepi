// Approve an initialized Kanban board for execution.
//
// args:
// {
//   expectedRevision: number
// }
//
// This workflow performs exactly one durable state transition:
// waiting_approval -> executing.

const expectedRevision = args.expectedRevision;

if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

const board = await state.get("kanban");

if (!board || typeof board !== "object") {
  throw new Error("kanban state is not initialized");
}

if (!Number.isInteger(board.revision)) {
  throw new Error("kanban state has an invalid revision");
}

if (board.revision !== expectedRevision) {
  throw new Error(
    `stale kanban revision: expected ${expectedRevision}, current ${board.revision}`,
  );
}

if (!board.workflow || board.workflow.state !== "waiting_approval") {
  const currentState = board.workflow?.state ?? "missing";
  throw new Error(
    `kanban approval requires workflow.state=waiting_approval; current ${currentState}`,
  );
}

const approved = JSON.parse(JSON.stringify(board));
approved.workflow.state = "executing";
approved.revision = board.revision + 1;
approved.updatedAt = new Date().toISOString();

await state.set("kanban", approved);

return approved;
