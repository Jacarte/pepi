import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { installHippocampus } from "./hippocampus/install.ts";

export default function hippocampus(pi: ExtensionAPI) { installHippocampus(pi, false); }
