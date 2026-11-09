import { StringEnum, Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Keeps `mem0_memory` registered in subagent children when the real
// @amaster.ai/pi-memory-mem0 tool is absent (extension missing or failing to
// load, platform mode without apiKey, invalid config, toolEnabled false,
// memoryMode passive). pi-subagents fails a child at agent_start when a tool in
// its allowlist is unregistered, and mem0 is optional.
//
// Pi resolves duplicate tool names by extension order (first wins), so list this
// file AFTER the real mem0 path in subagentOnlyExtensions: the real tool then
// shadows this stub whenever mem0 registers it. It lives outside the
// auto-discovered extensions/*.ts so the parent session never loads it.
// No network, no state.
const UNAVAILABLE = "mem0 is unavailable in this session. Continue without it.";

export default function(pi: ExtensionAPI) {
  pi.registerTool({
    name: "mem0_memory",
    label: "Mem0 Memory (unavailable)",
    description: `${UNAVAILABLE} Do not call this tool.`,
    parameters: Type.Object({
      action: StringEnum(["search", "add", "get_all", "delete"], {
        description: "The memory operation to perform.",
      }),
      query: Type.Optional(Type.String({ description: "Search query. Required for search." })),
      content: Type.Optional(Type.String({ description: "Memory content to store. Required for add." })),
      memory_id: Type.Optional(Type.String({ description: "Memory id to remove. Required for delete." })),
    }),
    async execute() {
      return { content: [{ type: "text", text: UNAVAILABLE }], details: { message: UNAVAILABLE } };
    },
  });
}
