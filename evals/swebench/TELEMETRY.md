# Time, turns, tokens and cost

The eval-only observer is installed in the frozen profile, never in your live
Pi extensions. It snapshots pi-subagents v1 `cost` and `status` event-bus RPC
at settle/shutdown boundaries and copies discovered child sessions. Pin a
pi-subagents version supporting these APIs; unsupported versions are unknown,
not zero. The runner archives `/output`, including temporary async artifacts.

After generation, reduce archived events without any model calls:

```sh
python -m evals.swebench.telemetry evals/swebench/runs/smoke-01
```

Each `attempts/<id>/telemetry.json` contains combined turns and token categories,
parent/per-child usage, Pi-estimated cost, active-child status, parent tool
counts/durations, retries and compaction observations. The raw parent stream
also provides a parent-only per-model breakdown. Agent wall time is recorded
by the outside runner; parallel child durations are not summed as wall time.

Count final `message_end` usage once, not streaming snapshots or copies in
turn/agent-end. A turn is one assistant response with its tool activity, not an
HTTP request. Do not add reasoning tokens to output tokens again. RPC totals
already include parent and child usage; do not add those overlapping sources.

`available` means a full-shaped shutdown accounting report was returned, not
proof of complete billing. Missing entire workflow receipts may evade the
unresolved-child count; external calls, hidden provider retries and compaction
may not be fully represented. Compaction usage is retained separately rather
than blindly added. `partial` and `unavailable` never mean free or zero work.
Reconcile task-attributed gateway billing separately; shared `/key/info` spend
is not a per-task meter. Costs here are Pi model-price estimates, not invoices.

Inspect active-child status: process exit alone does not establish that all
workflow children completed. The runner stops the dedicated agent UID before
patch export. A timeout may therefore submit a partial diff, with incomplete
usage. Raw snapshots and session artifacts must stay private and out of Git.

```sh
python -m unittest discover -s evals/swebench/tests -v
node --test evals/swebench/tests/telemetry-extension.test.mjs
```

Contracts inspected:
- https://github.com/nicobailon/pi-subagents/blob/main/docs/extension-api.md
- https://github.com/nicobailon/pi-subagents/blob/main/docs/observability.md
- https://pi.dev/docs/latest/json
- https://pi.dev/docs/latest/message-types
