# Kanban state contract

This directory defines the durable Kanban state that Pepi workflows will store under the `kanban` mission-state key.

The JSON Schema in `kanban-state.schema.json` is the canonical contract.

## Ownership

The scheduler is the only component allowed to mutate Kanban state. Workers, reviewers, scouts, and verifiers return results to the scheduler; they do not update the board directly.

## Status and phase

User-facing task statuses are deliberately small:

- `init`
- `todo`
- `working`
- `blocked`
- `done`
- `cancelled`

`phase` records the internal lifecycle while a card remains in a stable Kanban column:

- `queued`
- `implementation`
- `verification`
- `review`
- `fix`
- `integration`
- `complete`

## State rules

- Every state mutation increments `revision` and updates `updatedAt`.
- `activeWorkers` is derived from working tasks and is not persisted.
- Mission ID is owned by pi-subagents and is not duplicated in the board.
- Do not store prompts, transcripts, source code, diffs, logs, or full review output in mission state. Store bounded summaries and run/output references instead.

Semantic DAG validation (unique task IDs, dependency existence, cycle detection, and scheduler transitions) is intentionally deferred to the next change.

## Test

```bash
cd orchestration/kanban
npm install
npm test
```

Validate an arbitrary state file with:

```bash
npm run validate -- fixtures/valid-active.json
```
