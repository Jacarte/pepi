import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { installHippocampus } from "../hippocampus/install.ts";

// Explicit child entrypoint: foreground in-process subagents have no universal child environment flag.
export default function hippocampusReadOnly(pi: ExtensionAPI) { installHippocampus(pi, true); }
