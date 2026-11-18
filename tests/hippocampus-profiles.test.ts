import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const profiles = ["codex", "bedrock", "cloud"];
const nativeRoles = ["oracle", "delegate", "researcher", "worker", "scout", "reviewer", "evidence-auditor"];
const readonlySuffix = "/extensions/subagent-only/hippocampus-readonly.ts";
const fallbackSuffix = "/extensions/subagent-only/mem0-fallback.ts";

function checkMemoryWiring(settings: any) {
  // An installed upstream package or child path can introduce a second passive
  // writer even when a role's tool allowlist contains only read tools.
  assert.doesNotMatch(JSON.stringify(settings), /@amaster\.ai\/pi-memory-mem0|"pi-memory-mem0"|"userIdScope"/);
  assert.equal(settings.hippocampus.enabled, true);
  assert.equal(settings.hippocampus.autoCapture, true);
  assert.equal(settings.hippocampus.autoRecall, true);
  assert.equal(settings.hippocampus.userId, "default-user");
  assert.equal(typeof settings.hippocampus.baseUrl, "string");
  assert.match(settings.hippocampus.baseUrl, /^https?:\/\//);
  assert.ok(settings.hippocampus.requestTimeoutMs > 0);
  assert.ok(settings.hippocampus.recallLimit > 0 && settings.hippocampus.recallLimit <= 50);
  assert.deepEqual(settings.hippocampus.legacyUserIds, []);
  assert.equal(settings.hippocampus.recallLegacyCwd, true);

  const defaults = settings.subagents.defaultSubagentOnlyExtensions;
  const overrides = settings.subagents.agentOverrides;
  for (const role of nativeRoles) assert.ok(overrides[role].tools.includes("mem0_memory"), role);
  const entryLists = [
    ["defaults", defaults],
    ...Object.entries(overrides)
      .filter(([, override]: [string, any]) => !override.disabled && override.tools?.includes("mem0_memory"))
      .map(([role, override]: [string, any]) => [role, override.subagentOnlyExtensions ?? defaults]),
  ];
  for (const [role, paths] of entryLists as [string, string[]][]) {
    const adapters = paths.filter((path) => path.endsWith(readonlySuffix));
    assert.equal(adapters.length, 1, `${role}: exactly one explicit readonly adapter`);
    assert.equal(paths.filter((path) => path.endsWith(fallbackSuffix)).length, 1, `${role}: fallback`);
    assert.ok(paths.indexOf(adapters[0]) < paths.findIndex((path) => path.endsWith(fallbackSuffix)),
      `${role}: real tool registers before unavailable fallback`);
    assert.ok(!paths.some((path) => path.endsWith("/extensions/hippocampus.ts")),
      `${role}: child cannot load parent writer`);
    assert.equal(dirname(adapters[0]), dirname(paths.find((path) => path.endsWith(fallbackSuffix))!));
  }
}

for (const profile of profiles) {
  test(`${profile} reapplication replaces the old memory writer and scopes every child to readonly`, () => {
    const temp = mkdtempSync(join(tmpdir(), "pepi-memory-profile-"));
    try {
      // Exercise the documented migration through the real overlay script,
      // without applying a profile to the developer's configuration directory.
      mkdirSync(join(temp, "profiles", profile), { recursive: true });
      copyFileSync(join(root, "profile.sh"), join(temp, "profile.sh"));
      copyFileSync(join(root, "profiles", profile, "settings.json"),
        join(temp, "profiles", profile, "settings.json"));
      writeFileSync(join(temp, "settings.json"), JSON.stringify({
        packages: ["npm:@amaster.ai/pi-memory-mem0"],
        "pi-memory-mem0": { userId: "default-user", userIdScope: "userIdScope" },
      }));
      execFileSync("bash", [join(temp, "profile.sh"), profile], { cwd: temp, stdio: "pipe" });
      checkMemoryWiring(JSON.parse(readFileSync(join(temp, "settings.json"), "utf8")));
    } finally {
      rmSync(temp, { recursive: true, force: true });
    }
  });
}
