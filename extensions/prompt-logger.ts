import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Logs every provider request (system prompt, messages, tools) for the parent
// and any subagent child that loads this extension. One JSON object per line.
// Disable with PROMPT_LOG=0. Override the path with PROMPT_LOG_FILE.
const LOG_FILE = process.env.PROMPT_LOG_FILE || resolve(__dirname, "..", "logs", "prompts.jsonl");
const SECRET_KEY = /(api[-_]?key|authorization|token|secret|password)/i;

// Redact values under secret-looking keys; guard against cycles.
function redact(value: unknown, seen = new WeakSet<object>()): unknown {
  if (!value || typeof value !== "object") return value;
  if (seen.has(value as object)) return "[circular]";
  seen.add(value as object);
  if (Array.isArray(value)) return value.map((v) => redact(v, seen));
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    out[k] = SECRET_KEY.test(k) && typeof v === "string" ? "[redacted]" : redact(v, seen);
  }
  return out;
}

export default function(pi: ExtensionAPI) {
  if (process.env.PROMPT_LOG === "0") return;
  pi.on("before_provider_request", (event, ctx) => {
    try {
      const entry = {
        ts: new Date().toISOString(),
        session: ctx.sessionManager.getSessionId(),
        child: process.env.PI_SUBAGENT_CHILD === "1",
        parentSession: process.env.PI_SUBAGENT_PARENT_SESSION || null,
        model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : null,
        cwd: ctx.cwd,
        payload: redact(event.payload),
      };
      mkdirSync(dirname(LOG_FILE), { recursive: true });
      appendFileSync(LOG_FILE, `${JSON.stringify(entry)}\n`);
    } catch {
      // Logging must never break a request.
    }
    // Return nothing: payload is left unchanged.
  });
}
