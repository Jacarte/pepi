import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";

// skillOf is not exported: evaluate its definition from the extension source.
const source = readFileSync(new URL("../extensions/daggervals-process-stats.js", import.meta.url), "utf8");
const body = source.slice(source.indexOf("const skillNamePattern"), source.indexOf("const emptyUsage"));
const identity = (v: unknown) => typeof v === "string" && v.length > 0 && v.length <= 256 ? v : null;
const skillOf = new Function("identity", `${body}; return skillOf;`)(identity) as (event: unknown) => string | null;

const bash = (command: string) => skillOf({ toolName: "bash", args: { command } });

test("a read of <name>/SKILL.md records the skill", () => {
  assert.equal(skillOf({ toolName: "read", args: { path: "/a/b/tdd/SKILL.md" } }), "tdd");
  assert.equal(skillOf({ toolName: "read", args: { path: "SKILL.md" } }), null);
});

test("a bash command that reads <name>/SKILL.md records the skill", () => {
  assert.equal(bash("cat ~/.agents/skills/javier-writing-style/SKILL.md"), "javier-writing-style");
  assert.equal(bash("sed -n 1,40p /root/.agents/skills/tdd/SKILL.md | head"), "tdd");
  assert.equal(bash("echo \"x\" && cat ./deslop/SKILL.md"), "deslop");
  assert.equal(bash("head -50 'a/b/diagnosing-bugs/SKILL.md'"), "diagnosing-bugs");
});

test("globs, bare SKILL.md and look-alike paths are not a skill", () => {
  assert.equal(bash("cat skills/*/SKILL.md"), null);
  assert.equal(bash("ls skills"), null);
  assert.equal(bash("cat SKILL.md"), null);
  assert.equal(bash("grep -c foo skills/a/SKILL.md.bak"), null);
});
