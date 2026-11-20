import { getAgentDir, VERSION } from "@earendil-works/pi-coding-agent";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerHippocampus } from "./register.ts";

export function installHippocampus(pi: ExtensionAPI, readOnly: boolean) {
  registerHippocampus(pi, { agentDir: getAgentDir(), version: VERSION,
    // Plain JSON Schema works across Pi's schema-library transition.
    parameters: { type: "object", required: ["action"], additionalProperties: false, properties: {
      action: { type: "string", enum: ["search", "add", "get_all", "delete", "status", "retry"] },
      query: { type: "string", description: "Required for search." },
      content: { type: "string", description: "Selected durable text; required for add." },
      memory_id: { type: "string", description: "Required for delete." },
      kind: { type: "string", enum: ["overview"], description: "Only with add: store a short project overview (purpose, stack, key paths, how to test) as a long-lived stable-fact." },
    } },
  }, readOnly);
}
