import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import * as core from "../extensions/herdr-collab/core.ts";

const root = fileURLToPath(new URL("../", import.meta.url));
const realBuiltinAgents = path.join(root, "npm", "node_modules", "pi-subagents", "agents");
const M = "litellm/model-cheap";
const hasJj = (() => { try { execFileSync("jj", ["--version"], { stdio: "ignore" }); return true; } catch { return false; } })();

// Fake herdr CLI: a node script backed by a JSON state file, so the real exec path is exercised.
const FAKE_HERDR = String.raw`#!/usr/bin/env node
const fs = require("fs");
const file = process.env.FAKE_HERDR_STATE;
const lockDir = file + ".lock";
for (let i = 0; ; i++) {
  try { fs.mkdirSync(lockDir); break; } catch { if (i > 4000) throw new Error("fake herdr lock"); Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2); }
}
const args = process.argv.slice(2);
const st = JSON.parse(fs.readFileSync(file, "utf8"));
st.calls.push(args);
const out = (result) => process.stdout.write(JSON.stringify({ id: "fake", result }) + "\n");
const fail = (code, message) => { process.stdout.write(JSON.stringify({ id: "fake", error: { code, message } }) + "\n"); process.exitCode = 1; };
const opt = (name) => { const i = args.indexOf(name); return i === -1 ? undefined : args[i + 1]; };
const opts = (name) => args.flatMap((a, i) => a === name ? [args[i + 1]] : []);
const byAgent = (t) => { const id = st.agents[t] || (st.panes[t] && st.panes[t].agent ? t : undefined); return id ? st.panes[id] : undefined; };
const info = (p) => Object.assign({ name: p.agent.name, pane_id: p.id, agent: "pi", agent_status: p.agent.status, cwd: p.cwd }, p.agent.session ? { agent_session: { kind: "id", value: "s" } } : {});
let sleepThenTimeout = 0;
try {
  const [group, cmd] = args;
  const b = (name) => st.behaviors[name] || (st.behaviors[name] = {});
  if (group === "pane" && cmd === "split") {
    if (!st.panes[args[2]]) fail("pane_not_found", "no pane " + args[2]);
    else {
      const id = "w1:p" + st.next++;
      const env = Object.fromEntries(opts("--env").map((e) => [e.slice(0, e.indexOf("=")), e.slice(e.indexOf("=") + 1)]));
      // Same geometry rule as herdr: the split pane keeps round(size * ratio) cells.
      const r = st.panes[args[2]].rect, ratio = Number(opt("--ratio") || 0.5);
      let rect;
      if (opt("--direction") === "right") { const keep = Math.round(r.width * ratio); rect = { x: r.x + keep, y: r.y, width: r.width - keep, height: r.height }; r.width = keep; }
      else { const keep = Math.round(r.height * ratio); rect = { x: r.x, y: r.y + keep, width: r.width, height: r.height - keep }; r.height = keep; }
      st.panes[id] = { id, cwd: opt("--cwd"), env, rect, split: { from: args[2], direction: opt("--direction"), ratio: opt("--ratio") }, screen: [] };
      out({ type: "pane_info", pane: { pane_id: id, cwd: opt("--cwd") } });
    }
  } else if (group === "pane" && cmd === "close") {
    const p = st.panes[args[2]];
    if (!p) fail("pane_not_found", "no pane");
    else if ((st.closeFail || []).includes(args[2])) fail("pane_close_failed", "close refused");
    else { if (p.agent) delete st.agents[p.agent.name]; delete st.panes[args[2]]; st.closed.push(args[2]); out({ type: "ok" }); }
  } else if (group === "pane" && cmd === "layout") {
    out({ type: "pane_layout", layout: { panes: Object.values(st.panes).filter((p) => p.rect).map((p) => ({ pane_id: p.id, rect: p.rect })) } });
  } else if (group === "pane" && cmd === "get") {
    const p = st.panes[args[2]];
    p ? out({ type: "pane_info", pane: { pane_id: p.id, cwd: p.cwd } }) : fail("pane_not_found", "no pane");
  } else if (group === "pane" && cmd === "run") {
    const m = args[3].match(/^cd '(.*)'$/);
    if (m) st.panes[args[2]].cwd = m[1];
    out({ type: "ok" });
  } else if (group === "pane" && cmd === "process-info") {
    const p = st.panes[opt("--pane")];
    out({ process_info: { shell_pid: 100, foreground_process_group_id: p.agent ? 200 : 100 } });
  } else if (group === "pane" && cmd === "read") {
    process.stdout.write((st.panes[args[2]].screen || []).slice(-Number(opt("--lines"))).join("\n") + "\n");
  } else if (group === "agent" && cmd === "start") {
    const name = args[2], p = st.panes[opt("--pane")];
    if (b(name).startFail) fail("agent_not_ready", "agent blocked during startup");
    else if (b(name).notShell > 0) { b(name).notShell--; fail("pane_not_available", "agent target pane " + p.id + " is not an available shell"); }
    else {
      p.agent = { name, status: "idle", session: true, argv: args.slice(args.indexOf("--") + 1), prompts: 0 };
      st.agents[name] = p.id;
      p.screen.push("pi started");
      out({ type: "agent_started", agent: info(p) });
    }
  } else if (group === "agent" && cmd === "get") {
    const p = byAgent(args[2]);
    p ? out({ type: "agent_info", agent: info(p) }) : fail("agent_not_found", "agent target not found");
  } else if (group === "agent" && cmd === "prompt") {
    const p = byAgent(args[2]), text = args[3];
    if (!p) fail("agent_not_found", "agent target not found");
    else if (p.agent.status === "blocked") fail("agent_blocked", "blocked");
    else if (text === "/quit") { st.quits.push(p.agent.name); delete st.agents[p.agent.name]; delete p.agent; out({ type: "agent_info" }); }
    else {
      const beh = b(p.agent.name);
      p.agent.prompts++;
      if (beh.stallFirst && p.agent.prompts === 1) fail("agent_prompt_stalled", "no working state");
      else {
        st.prompts.push({ target: p.agent.name, text, statusBefore: p.agent.status });
        p.screen.push(...text.split("\n"));
        const after = beh.onPrompt || "done";
        p.agent.status = after === "hang" ? "working" : after;
        if (after === "done") p.screen.push(...(beh.reply || ["STATUS: done"]));
        out({ type: "agent_info", agent: Object.assign(info(p), { agent_status: after === "blocked" ? "blocked" : "working" }) });
      }
    }
  } else if (group === "agent" && cmd === "wait") {
    const p = byAgent(args[2]);
    if (!p) fail("agent_not_found", "agent target not found");
    else {
      const beh = b(p.agent.name);
      if (p.agent.status === "working" && beh.busyUntilWait) { p.agent.status = "idle"; beh.busyUntilWait = false; }
      if (["idle", "done", "blocked"].includes(p.agent.status)) out({ type: "agent_info", agent: info(p) });
      else sleepThenTimeout = Math.min(Number(opt("--timeout")), 60);
    }
  } else if (group === "agent" && cmd === "read") {
    const p = byAgent(args[2]);
    process.stdout.write(p.screen.slice(-Number(opt("--lines"))).join("\n") + "\n");
  } else fail("fake_unknown", args.join(" "));
} finally {
  fs.writeFileSync(file, JSON.stringify(st));
  fs.rmSync(lockDir, { recursive: true, force: true });
}
if (sleepThenTimeout) setTimeout(() => fail("timeout", "timed out waiting for agent status"), sleepThenTimeout);
`;

// Fake codegraph on PATH: logs each call; mode file selects ok | fail | hang.
const FAKE_CODEGRAPH = String.raw`#!/usr/bin/env node
const fs = require("fs"), path = require("path");
let mode = "ok";
try { mode = fs.readFileSync(process.env.FAKE_CODEGRAPH_MODE, "utf8").trim(); } catch {}
fs.appendFileSync(process.env.FAKE_CODEGRAPH_LOG, JSON.stringify({ cwd: process.cwd(), args: process.argv.slice(2), noDaemon: process.env.CODEGRAPH_NO_DAEMON }) + "\n");
if (mode === "fail") { console.error("boom"); process.exit(3); }
if (mode === "fail-after-mkdir" || mode === "stubborn") {
  fs.mkdirSync(path.join(process.argv[3], ".codegraph"), { recursive: true });
  fs.writeFileSync(path.join(process.argv[3], ".codegraph", ".gitignore"), "*\n!.gitignore\n");
}
if (mode === "fail-after-mkdir") process.exit(4);
if (mode === "stubborn") {
  process.on("SIGTERM", () => {});
  const grandchild = require("child_process").spawn(process.execPath, ["-e", "process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"], { stdio: "inherit" });
  fs.writeFileSync(process.env.FAKE_CODEGRAPH_LOG + ".grandchild", String(grandchild.pid));
  setInterval(() => {}, 1000);
  return;
}
if (mode === "hang") { setTimeout(() => {}, 10000); return; }
const dir = path.join(process.argv[3], ".codegraph");
fs.mkdirSync(dir, { recursive: true });
fs.writeFileSync(path.join(dir, ".gitignore"), "*\n!.gitignore\n");
fs.writeFileSync(path.join(dir, "codegraph.db"), "db");
`;

type Harness = ReturnType<typeof setup>;

function sh(command: string, args: string[], cwd: string, env: NodeJS.ProcessEnv): string {
  return execFileSync(command, args, { cwd, env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
}

// TypeSafe Choice answers in the shape pi-typesafe-router.ts parses.
const LANE_OPTIONS = ["local", "cheap", "frontier"];
const BLAST_RADIUS_OPTIONS = ["contained", "cross_cutting", "critical"];
const choiceAnswer = (options: string[], choice: string, confidence: number) =>
  ({ type: "choice", choice, confidence, probabilities: Object.fromEntries(options.map((o) => [o, o === choice ? confidence : (1 - confidence) / 2])) });
const routerAnswers = (lane: string, blastRadius = "contained", confidence = 1, blastConfidence = 1) =>
  ({ lane: choiceAnswer(LANE_OPTIONS, lane, confidence), blast_radius: choiceAnswer(BLAST_RADIUS_OPTIONS, blastRadius, blastConfidence) });

function setup(options: { repo?: "jj" | "git" | "none"; answers?: unknown; fetchFails?: boolean; noKey?: boolean } = {}) {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "herdr-collab-test-")));
  const agentDir = path.join(tmp, "agent");
  for (const dir of ["agents", "extensions/subagent-only", "skills/fixture-skill", "bin"]) fs.mkdirSync(path.join(agentDir, dir), { recursive: true });
  const ext = (name: string) => {
    const file = path.join(agentDir, "extensions", name);
    fs.writeFileSync(file, "export default function () {}\n");
    return file;
  };
  const selfExt = ext("herdr-collab.ts");
  const routerExt = ext("router.ts");
  const defaultOnly = ext("subagent-only/default-only.ts");
  ext("herdr-agent-state.ts");
  fs.writeFileSync(path.join(agentDir, "skills", "fixture-skill", "SKILL.md"), "---\nname: fixture-skill\n---\nbody\n");
  fs.writeFileSync(path.join(agentDir, "agents", "deep-worker.md"), [
    "---", "name: deep-worker", "description: fixture", "thinking: xhigh", "systemPromptMode: replace",
    "inheritProjectContext: true", "inheritSkills: true", "tools: read, bash, edit, write, contact_supervisor", "---", "",
    "You are the fixture deep-worker.",
  ].join("\n"));
  fs.writeFileSync(path.join(agentDir, "settings.json"), JSON.stringify({
    subagents: {
      defaultProvider: "litellm",
      defaultSubagentOnlyExtensions: [defaultOnly],
      agentOverrides: {
        worker: { model: "model-local", tools: ["read", "bash", "edit", "write", "contact_supervisor", "jev_route"], subagentOnlyExtensions: [routerExt] },
        "deep-worker": { model: "model-frontier", skills: ["fixture-skill"], subagentOnlyExtensions: [routerExt] },
        reviewer: { model: "litellm/model-frontier", skills: ["fixture-skill"], tools: ["read", "grep", "watchdog_diff", "contact_supervisor"] },
        oracle: { disabled: true },
        researcher: { thinking: false },
      },
    },
  }));
  const lanes = { LOCAL: "model-local", CHEAP: "model-cheap", FRONTIER: "model-frontier" };
  fs.writeFileSync(path.join(agentDir, "models-router.json"), JSON.stringify({
    minConfidence: 0.4,
    ...Object.fromEntries(Object.entries(lanes).map(([lane, id]) => [lane, { provider: "litellm", id }])),
    HUMAN: { provider: "human", id: "javi" },
  }));
  const fakeBin = path.join(agentDir, "bin", "herdr");
  fs.writeFileSync(fakeBin, FAKE_HERDR, { mode: 0o755 });
  fs.writeFileSync(path.join(agentDir, "bin", "codegraph"), FAKE_CODEGRAPH, { mode: 0o755 });
  const stateFile = path.join(tmp, "herdr-state.json");
  fs.writeFileSync(stateFile, JSON.stringify({ next: 1, panes: { "w1:p0": { id: "w1:p0", cwd: tmp, screen: [], rect: { x: 0, y: 0, width: 248, height: 81 } } }, agents: {}, calls: [], prompts: [], quits: [], closed: [], behaviors: {} }));
  fs.writeFileSync(path.join(tmp, "jjconfig.toml"), '[user]\nname = "Test"\nemail = "test@example.com"\n[ui]\ncolor = "never"\npaginate = "never"\n');
  fs.writeFileSync(path.join(tmp, "gitconfig"), "[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n");
  const env: Record<string, string | undefined> = {
    ...process.env,
    HERDR_ENV: "1",
    HERDR_PANE_ID: "w1:p0",
    HERDR_BIN_PATH: fakeBin,
    FAKE_HERDR_STATE: stateFile,
    JJ_CONFIG: path.join(tmp, "jjconfig.toml"),
    GIT_CONFIG_GLOBAL: path.join(tmp, "gitconfig"),
    GIT_CONFIG_NOSYSTEM: "1",
    TYPESAFE_API_KEY: options.noKey ? undefined : "offline-test",
    PATH: `${path.join(agentDir, "bin")}${path.delimiter}${process.env.PATH}`,
    FAKE_CODEGRAPH_MODE: path.join(tmp, "codegraph-mode"),
    FAKE_CODEGRAPH_LOG: path.join(tmp, "codegraph-calls.jsonl"),
  };
  const repo = path.join(tmp, "repo");
  fs.mkdirSync(repo);
  if (options.repo === "jj") {
    sh("jj", ["git", "init"], repo, env as NodeJS.ProcessEnv);
    fs.writeFileSync(path.join(repo, "f.txt"), "one\ntwo\nthree\n");
    sh("jj", ["commit", "-m", "base"], repo, env as NodeJS.ProcessEnv);
  } else if (options.repo === "git") {
    sh("git", ["init", "-q"], repo, env as NodeJS.ProcessEnv);
    fs.writeFileSync(path.join(repo, "f.txt"), "one\ntwo\nthree\n");
    sh("git", ["add", "-A"], repo, env as NodeJS.ProcessEnv);
    sh("git", ["commit", "-q", "-m", "base"], repo, env as NodeJS.ProcessEnv);
  }
  const routerRequests: Array<{ url: string; body: any }> = [];
  const deps: core.CollabDeps = {
    exec: core.createExec(env),
    fetch: (async (url: string, init: { body: string }) => {
      routerRequests.push({ url, body: JSON.parse(init.body) });
      if (options.fetchFails) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, json: async () => ({ answers: options.answers ?? routerAnswers("cheap") }) };
    }) as unknown as typeof fetch,
    env,
    agentDir,
    builtinAgentsDir: realBuiltinAgents,
    homeDir: tmp,
    wsRoot: path.join(tmp, "ws"),
    extensionPath: selfExt,
    timing: { settleMs: 0, pollMs: 5, promptRetryMs: 1, readyTimeoutMs: 1_000, lockTimeoutMs: 5_000 },
  };
  const registry = {
    find: (provider: string, id: string) => provider === "litellm" && Object.values(lanes).includes(id) ? { provider, id, input: ["text"] } : undefined,
    getAvailable: () => Object.values(lanes).map((id) => ({ provider: "litellm", id })),
  };
  const ctx: core.CallContext = { cwd: repo, modelRegistry: registry, trusted: false };
  return {
    tmp, agentDir, repo, env, deps, ctx, selfExt, routerExt, defaultOnly,
    collab: core.createHerdrCollab(deps),
    fetches: () => routerRequests.length,
    routerRequests,
    state: () => JSON.parse(fs.readFileSync(stateFile, "utf8")),
    patchState: (mutate: (state: any) => void) => {
      const state = JSON.parse(fs.readFileSync(stateFile, "utf8"));
      mutate(state);
      fs.writeFileSync(stateFile, JSON.stringify(state));
    },
    run: (runId: string) => core.readRun(path.join(tmp, "ws"), runId),
    jj: (args: string[], cwd = repo) => sh("jj", args, cwd, env as NodeJS.ProcessEnv),
    codegraphMode: (mode: string) => fs.writeFileSync(path.join(tmp, "codegraph-mode"), mode),
    codegraphCalls: (): Array<{ cwd: string; args: string[]; noDaemon?: string }> => {
      const file = path.join(tmp, "codegraph-calls.jsonl");
      return fs.existsSync(file) ? fs.readFileSync(file, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line)) : [];
    },
    git: (args: string[], cwd = repo) => sh("git", args, cwd, env as NodeJS.ProcessEnv),
    done: () => fs.rmSync(tmp, { recursive: true, force: true }),
  };
}

const worker = (name: string, extra: Partial<core.WorkerSpec> = {}): core.WorkerSpec => ({ name, role: "worker", task: `task for ${name}`, model: M, ...extra });
const argvOf = (h: Harness, agentName: string): string[] => Object.values(h.state().panes as Record<string, any>).find((p) => p.agent?.name === agentName)?.agent.argv
  ?? ((call: string[]) => call.slice(call.indexOf("--") + 1))(h.state().calls.find((c: string[]) => c[0] === "agent" && c[1] === "start" && c[2] === agentName));

// ---------------------------------------------------------------------------
// Role resolution and flag mapping

test("real pi-subagents definitions and settings resolve for every herdr role", () => {
  for (const role of core.ROLE_NAMES) {
    const definition = core.resolveRole(role, { agentDir: root, builtinAgentsDir: realBuiltinAgents });
    assert.equal(definition.name, role);
    assert.ok(definition.systemPrompt.length > 100, `${role} has a role prompt`);
    assert.ok(definition.tools?.length, `${role} has a tool allowlist`);
  }
  const deep = core.resolveRole("deep-worker", { agentDir: root, builtinAgentsDir: realBuiltinAgents });
  assert.equal(deep.source, path.join(root, "agents", "deep-worker.md"));
  assert.equal(core.resolveRole("reviewer", { agentDir: root, builtinAgentsDir: realBuiltinAgents }).readOnly, true);
  assert.equal(core.resolveRole("worker", { agentDir: root, builtinAgentsDir: realBuiltinAgents }).readOnly, false);
});

test("role resolution applies user agents, defaults, overrides, and fails loudly", (t) => {
  const h = setup();
  t.after(h.done);
  const opts = { agentDir: h.agentDir, builtinAgentsDir: realBuiltinAgents };
  const workerRole = core.resolveRole("worker", opts);
  assert.equal(workerRole.configuredModel, "litellm/model-local", "defaultProvider is applied to a bare model id");
  assert.deepEqual(workerRole.tools, ["read", "bash", "edit", "write", "contact_supervisor", "jev_route"]);
  assert.deepEqual(workerRole.subagentOnlyExtensions, [h.routerExt]);
  assert.equal(workerRole.systemPromptMode, "replace");
  const deep = core.resolveRole("deep-worker", opts);
  assert.match(deep.systemPrompt, /fixture deep-worker/);
  assert.equal(deep.inheritSkills, true);
  assert.deepEqual(deep.skills, ["fixture-skill"]);
  const scout = core.resolveRole("scout", opts);
  assert.deepEqual(scout.subagentOnlyExtensions, [h.defaultOnly], "default subagent-only extensions apply when a role has none");
  assert.equal(scout.configuredModel, undefined);
  assert.equal(core.resolveRole("reviewer", opts).configuredModel, "litellm/model-frontier");
  assert.equal(core.resolveRole("researcher", opts).thinking, undefined, "thinking: false removes the value");
  assert.equal(core.resolveRole("delegate", opts).systemPromptMode, "append");
  assert.throws(() => core.resolveRole("oracle", opts), /disabled in settings/);
  assert.throws(() => core.resolveRole("planner", opts), /unknown role "planner"/);
  assert.throws(() => core.resolveSkillPath("missing-skill", { cwd: h.repo, agentDir: h.agentDir, homeDir: h.tmp }), /not found/);
});

test("flag mapping matches pi CLI flags for writer, reviewer, and append roles", (t) => {
  const h = setup();
  t.after(h.done);
  const opts = { agentDir: h.agentDir, builtinAgentsDir: realBuiltinAgents };
  const base = { promptFile: "/tmp/p.md", skillPaths: [], trusted: false, sessionName: "herdr-hc000000-w1", agentDir: h.agentDir, extensionPath: h.selfExt };
  const writer = core.buildPiArgs(core.resolveRole("worker", opts), { ...base, model: M, messaging: true });
  const value = (args: string[], flag: string) => args[args.indexOf(flag) + 1];
  assert.equal(value(writer, "--model"), M);
  assert.equal(value(writer, "--thinking"), "high");
  assert.equal(value(writer, "--tools"), "read,bash,edit,write,jev_route,herdr_peers,herdr_send");
  assert.equal(value(writer, "--system-prompt"), "/tmp/p.md");
  assert.ok(writer.includes("--no-skills") && writer.includes("--no-approve") && !writer.includes("--no-context-files"));
  assert.ok(!writer.includes("--no-extensions"));
  assert.deepEqual(writer.flatMap((a, i) => a === "-e" ? [writer[i + 1]] : []), [h.routerExt, h.selfExt]);

  const reviewerRole = core.resolveRole("reviewer", opts);
  const skill = core.resolveSkillPath("fixture-skill", { cwd: h.repo, agentDir: h.agentDir, homeDir: h.tmp });
  const reviewer = core.buildPiArgs(reviewerRole, { ...base, model: `${M}:low`, skillPaths: [skill], trusted: true, messaging: false });
  assert.equal(value(reviewer, "--tools"), "read,grep", "child-runtime-only tools are dropped; no messaging tools");
  assert.ok(!reviewer.includes("--thinking"), "an explicit :thinking suffix wins");
  assert.ok(reviewer.includes("--approve"));
  assert.equal(value(reviewer, "--skill"), skill);

  const delegate = core.buildPiArgs(core.resolveRole("delegate", opts), { ...base, model: M, messaging: true });
  assert.equal(value(delegate, "--append-system-prompt"), "/tmp/p.md");
  assert.ok(!delegate.includes("--system-prompt"));

  const isolated = { ...core.resolveRole("worker", opts), extensions: [h.routerExt] };
  const args = core.buildPiArgs(isolated, { ...base, model: M, messaging: true });
  assert.ok(args.includes("--no-extensions"));
  assert.ok(args.includes(path.join(h.agentDir, "extensions", "herdr-agent-state.ts")), "herdr state reporting stays loaded");
  assert.throws(() => core.buildPiArgs({ ...isolated, subagentOnlyExtensions: ["/nope/missing.ts"] }, { ...base, model: M, messaging: false }), /does not exist/);
});

test("layout chooser: parent split once, then the largest visual pane (oldest on ties); wide -> right, tall -> down", () => {
  const g = (paneId: string, x: number, y: number, width: number, height: number) => ({ paneId, x, y, width, height });
  assert.deepEqual(core.chooseSplit("P", []), { target: "P", direction: "right", ratio: 0.5 }, "first worker: split the parent once");
  // Right half of a 248x81 tab: 124x81 is visually tall (81 rows ~ 162 columns).
  assert.deepEqual(core.chooseSplit("P", [g("w1", 124, 0, 124, 81)]), { target: "w1", direction: "down", ratio: 0.5 });
  const two = [g("w1", 124, 0, 124, 41), g("w2", 124, 41, 124, 40)];
  assert.deepEqual(core.chooseSplit("P", two), { target: "w1", direction: "right", ratio: 0.5 }, "third: largest is w1, visually wide");
  const three = [g("w1", 124, 0, 62, 41), g("w2", 124, 41, 124, 40), g("w3", 186, 0, 62, 41)];
  assert.deepEqual(core.chooseSplit("P", three), { target: "w2", direction: "right", ratio: 0.5 }, "fourth: the full-width w2");
  const tie = [g("old", 124, 0, 62, 40), g("new", 186, 0, 62, 40)];
  assert.equal(core.chooseSplit("P", tie).target, "old", "ties keep the oldest pane");
  // After w2 closed, its space went back to a neighbour; the chooser only sees live geometry.
  const afterClose = [g("w1", 124, 0, 62, 81), g("w3", 186, 0, 62, 81)];
  assert.deepEqual(core.chooseSplit("P", afterClose), { target: "w1", direction: "down", ratio: 0.5 });
  assert.equal(core.chooseSplit("P", [g("w9", 0, 0, 80, 40)]).direction, "right", "exactly 2:1 counts as wide");
});

test("pane prompt filtering drops global AGENTS.md and the orchestration skill only", () => {
  const options = {
    contextFiles: [{ path: path.join(root, "AGENTS.md") }, { path: "/work/project/AGENTS.md" }],
    skills: [{ name: "pi-subagents" }, { name: "go-reviewer" }],
  };
  core.filterPanePromptOptions(options, root, false);
  assert.deepEqual(options.contextFiles.map((f) => f.path), ["/work/project/AGENTS.md"]);
  assert.deepEqual(options.skills.map((s) => s.name), ["go-reviewer"]);
  const global = fs.readFileSync(path.join(root, "AGENTS.md"), "utf8");
  const copy = { contextFiles: [{ path: "/Users/x/.herdr-ws/hc123456/w1/AGENTS.md", content: global }, { path: "/w/AGENTS.md", content: "repo rules" }] };
  core.filterPanePromptOptions(copy, root, false);
  assert.deepEqual(copy.contextFiles.map((f) => f.path), ["/w/AGENTS.md"], "a workspace copy of the global AGENTS.md is dropped");
  const inherit = { contextFiles: [{ path: path.join(root, "AGENTS.md") }] };
  core.filterPanePromptOptions(inherit, root, true);
  assert.equal(inherit.contextFiles.length, 1);
});

// Run the real jev_route recommend (vm-evaluated like tests/typesafe-router.test.ts) on the
// same models-router.json, registry, and fake TypeSafe answers as the in-process duplicate.
function loadJevRoute(h: ReturnType<typeof setup>, respond: () => unknown) {
  const source = fs.readFileSync(path.join(root, "extensions", "pi-typesafe-router.ts"), "utf8");
  const js = stripTypeScriptTypes(source)
    .replace(/^import .* from ".*";\s*$/gm, "")
    .replace("export default function", "function register");
  const requests: Array<{ url: string; body: any }> = [];
  const tools: any[] = [];
  const register = vm.runInNewContext(`${js}\nregister;`, {
    __dirname: path.join(root, "extensions"), dirname: path.dirname, resolve: path.resolve,
    readFileSync: () => fs.readFileSync(path.join(h.agentDir, "models-router.json"), "utf8"),
    appendFileSync() {}, mkdirSync() {},
    Type: new Proxy({}, { get: () => () => ({}) }), StringEnum: () => ({}), Text: class {},
    process: { env: h.env }, AbortSignal,
    fetch: async (url: string, init: { body: string }) => {
      requests.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({ answers: respond() }) };
    },
  });
  register({ registerTool: (tool: any) => tools.push(tool) });
  const ctx = { sessionManager: { getSessionId: () => "sync" }, modelRegistry: h.ctx.modelRegistry, getContextUsage: () => undefined };
  return {
    requests,
    recommend: (params: { role: string; task: string; context: string }) =>
      tools[0].execute("sync", { action: "recommend", ...params }, undefined, undefined, ctx).then((result: any) => result.details),
  };
}

// Objects created inside the vm realm have a different Object.prototype.
const plain = (value: unknown) => JSON.parse(JSON.stringify(value));
const routingOutcome = (decision: Promise<any>) => decision.then(
  (d) => plain({ lane: d.lane, model: d.model, escalated: d.escalated, reason: d.reason, laneAnswer: d.laneAnswer, blastRadius: d.blastRadius }),
  (error: Error) => ({ error: error.message.replace(/^(Jev|herdr) router: /, "") }),
);

test("in-process router matches pi-typesafe-router recommend: request, lane decisions, and validation", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  let answers: unknown;
  const herdrRequests: Array<{ url: string; body: any }> = [];
  const deps: core.CollabDeps = {
    ...h.deps,
    fetch: (async (url: string, init: { body: string }) => {
      herdrRequests.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({ answers }) };
    }) as unknown as typeof fetch,
  };
  const jev = loadJevRoute(h, () => answers);
  const compare = async (label: string, role: core.RoleName = "worker") => {
    const input = { role, task: `  ${label}  `, context: "supplied context" };
    const [expected, actual] = await Promise.all([
      routingOutcome(jev.recommend(input)),
      routingOutcome(core.recommendModel(deps, input, h.ctx.modelRegistry)),
    ]);
    assert.deepEqual(actual, expected, label);
    return actual;
  };

  const reasons = new Set<unknown>();
  for (const lane of LANE_OPTIONS) for (const blast of BLAST_RADIUS_OPTIONS) {
    for (const confidence of [0.39, 0.4, 0.9]) for (const blastConfidence of [0.39, 0.4, 0.9]) {
      answers = routerAnswers(lane, blast, confidence, blastConfidence);
      reasons.add((await compare(`${lane} ${blast} ${confidence} ${blastConfidence}`)).reason);
    }
  }
  assert.deepEqual(reasons, new Set([null, "low_confidence", "critical", "uncertain_critical_floor", "cross_cutting_floor"]));
  answers = routerAnswers("cheap");
  await compare("scout task", "scout");
  assert.equal(herdrRequests.length, jev.requests.length);
  assert.deepEqual(plain(herdrRequests), plain(jev.requests), "same endpoint, classifier, state (role/task/context/files), and questions");
  assert.equal(herdrRequests.at(-1)!.body.state.role, "scout");

  const valid = routerAnswers("cheap");
  const invalid: Array<[string, unknown]> = [
    ["missing blast_radius", { lane: valid.lane }],
    ["unknown choice", { ...valid, lane: { ...valid.lane, choice: "huge" } }],
    ["confidence out of range", { ...valid, lane: { ...valid.lane, confidence: 1.5 } }],
    ["missing probability", { ...valid, blast_radius: { ...valid.blast_radius, probabilities: { contained: 1 } } }],
    ["extra probability", { ...valid, lane: { ...valid.lane, probabilities: { ...valid.lane.probabilities, huge: 0 } } }],
    ["answers not an object", null],
  ];
  for (const [label, bad] of invalid) {
    answers = bad;
    assert.ok("error" in (await compare(label)), label);
  }

  answers = routerAnswers("cheap", "contained", 0.9);
  const configFile = path.join(h.agentDir, "models-router.json");
  const config = JSON.parse(fs.readFileSync(configFile, "utf8"));
  fs.writeFileSync(configFile, JSON.stringify({ ...config, minConfidence: 0.95 }));
  assert.equal((await compare("minConfidence 0.95")).reason, "low_confidence");
  for (const minConfidence of [undefined, 1.5]) {
    fs.writeFileSync(configFile, JSON.stringify({ ...config, minConfidence }));
    assert.ok("error" in (await compare(`minConfidence ${minConfidence}`)));
  }
});

// ---------------------------------------------------------------------------
// Models, caps, and failure modes

test("routing: worker/delegate route in-process; pinned roles keep their model; explicit model skips both", async (t) => {
  const h = setup({ repo: "none", answers: routerAnswers("frontier") });
  t.after(h.done);
  const routed = await h.collab.spawn({ workers: [{ name: "w1", role: "worker", task: "fix a subtle bug" }] }, h.ctx);
  assert.equal(h.fetches(), 1);
  assert.equal(h.routerRequests[0]!.body.state.role, "worker", "the pane role is passed to the router");
  assert.deepEqual([routed.panes[0]!.model, routed.panes[0]!.modelSource], ["litellm/model-frontier", "routed"]);
  const decision = JSON.parse(fs.readFileSync(path.join(h.agentDir, "logs", "router.log"), "utf8").split("\n").find((line) => line.startsWith("{"))!);
  assert.deepEqual([decision.lane, decision.provider, decision.id, decision.action, decision.changed, decision.escalated, decision.reason, decision.role],
    ["FRONTIER", "litellm", "model-frontier", "herdr_recommend", false, false, null, "worker"]);
  const argv = argvOf(h, `${routed.runId}-w1`);
  assert.equal(argv[argv.indexOf("--model") + 1], "litellm/model-frontier");
  const review = await h.collab.review({ target: "diff", reviewers: [{ name: "r1", lens: "correctness" }] }, h.ctx);
  assert.equal(h.fetches(), 1, "pinned reviewer does not route");
  assert.deepEqual([review.panes[0]!.model, review.panes[0]!.modelSource], ["litellm/model-frontier", "pinned"]);
  await h.collab.spawn({ workers: [{ name: "s2", role: "scout", task: "x", model: "litellm/model-local" }] }, h.ctx);
  assert.equal(h.fetches(), 1, "explicit model skips the router");
  const unpinned = await h.collab.spawn({ workers: [{ name: "s3", role: "scout", task: "map it" }] }, h.ctx);
  assert.equal(h.fetches(), 2, "a role without a pinned model falls back to routing");
  assert.equal(h.routerRequests[1]!.body.state.role, "scout");
  assert.equal(unpinned.panes[0]!.modelSource, "routed-no-pin");
  assert.match(unpinned.panes[0]!.modelNote!, /no pinned model/);
  await assert.rejects(h.collab.spawn({ workers: [{ name: "s4", role: "scout", task: "x", model: "litellm/unknown" }] }, h.ctx), /not registered/);
});

test("router failure fails the spawn loudly with no panes, workspaces, or run", async (t) => {
  for (const options of [{ fetchFails: true }, { noKey: true }]) {
    const h = setup({ repo: "none", ...options });
    t.after(h.done);
    await assert.rejects(h.collab.spawn({ workers: [{ name: "w1", role: "worker", task: "x" }] }, h.ctx), /model routing failed, nothing was spawned/);
    assert.equal(h.state().calls.length, 0);
    assert.ok(!fs.existsSync(h.deps.wsRoot));
  }
});

test("caps: max 4 parallel workers, max 3 reviewers, distinct review lenses", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  const scouts = ["s1", "s2", "s3", "s4"].map((name) => ({ name, role: "scout", task: "x", model: M }));
  const { runId } = await h.collab.spawn({ workers: scouts }, h.ctx);
  const afterFour = h.state();
  await assert.rejects(h.collab.spawn({ runId, workers: [{ name: "s5", role: "scout", task: "x", model: M }] }, h.ctx), /max 4 parallel workers/);
  await assert.rejects(h.collab.review({ runId, target: "t", reviewers: [{ name: "r1", lens: "Security", model: M }, { name: "r2", lens: "security", model: M }] }, h.ctx), /different, nonempty lens/);
  await h.collab.review({ runId, target: "t", reviewers: ["r1", "r2", "r3"].map((name, i) => ({ name, lens: `lens ${i}`, model: M })) }, h.ctx);
  await assert.rejects(h.collab.review({ runId, target: "t", reviewers: [{ name: "r4", lens: "extra", model: M }] }, h.ctx), /max 3 reviewer panes/);
  await assert.rejects(h.collab.spawn({ runId, workers: [{ name: "s1", role: "scout", task: "x", model: M }] }, h.ctx), /already used/);
  // Layout: one parent split, then BSP on the right half (largest visual pane; wide -> right, tall -> down).
  const state = afterFour;
  const panes = Object.values(state.panes as Record<string, any>).filter((p) => p.split);
  const [s1, s2, s3, s4] = panes;
  assert.deepEqual(panes.slice(0, 4).map((p) => [p.split.from, p.split.direction, p.split.ratio]),
    [["w1:p0", "right", "0.5"], [s1.id, "down", "0.5"], [s1.id, "right", "0.5"], [s2.id, "right", "0.5"]]);
  assert.ok(panes.every((p) => p.split.from === "w1:p0" ? p === s1 : true), "the parent is split exactly once");
  assert.deepEqual(state.panes["w1:p0"].rect, { x: 0, y: 0, width: 124, height: 81 }, "parent keeps the full-height left half");
  assert.deepEqual([s1.rect, s2.rect, s3.rect, s4.rect].map((r: any) => [r.x, r.y, r.width, r.height]),
    [[124, 0, 62, 41], [124, 41, 62, 40], [186, 0, 62, 41], [186, 41, 62, 40]]);
  const reviewers = Object.values(h.state().panes as Record<string, any>).filter((p) => p.split).slice(4);
  assert.ok(reviewers.every((p) => p.split.from !== "w1:p0"), "reviewers tile the right half too; the parent is never split again");
});

test("wait reports blocked, settled, and timeout separately; stalled prompts are retried once", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  const first = await h.collab.spawn({ workers: [{ name: "ok", role: "scout", task: "fine", model: M }] }, h.ctx);
  const spawnOne = async (name: string, behavior: Record<string, unknown>) => {
    h.patchState((s) => { s.behaviors[`${first.runId}-${name}`] = behavior; });
    return h.collab.spawn({ runId: first.runId, workers: [{ name, role: "scout", task: name, model: M }] }, h.ctx);
  };
  await spawnOne("blk", { onPrompt: "blocked" });
  await spawnOne("slow", { onPrompt: "hang" });
  await spawnOne("flaky", { stallFirst: true });
  const prompts = h.state().prompts.filter((p: any) => p.target === `${first.runId}-flaky`);
  assert.equal(prompts.length, 1, "stalled prompt resent once after the marker was missing");
  const result = await h.collab.wait({ runId: first.runId, timeoutMs: 1_000 });
  const byName = Object.fromEntries(result.outcomes.map((o) => [o.name, o]));
  assert.equal(byName.ok.state, "done");
  assert.equal(byName.blk.state, "blocked");
  assert.equal(byName.slow.state, "timeout");
  assert.equal(byName.slow.current, "working");
  assert.equal(byName.flaky.state, "done");
  const any = await h.collab.wait({ runId: first.runId, names: ["slow", "ok"], timeoutMs: 5_000, mode: "any" });
  assert.deepEqual(any.outcomes.map((o) => o.state), ["pending", "done"]);
});

test("agent start failure reports started panes and leaves cleanup possible", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  const { runId } = await h.collab.spawn({ workers: [{ name: "s1", role: "scout", task: "x", model: M }] }, h.ctx);
  h.patchState((s) => { s.behaviors[`${runId}-s2`] = { startFail: true }; });
  await assert.rejects(h.collab.spawn({ runId, workers: [{ name: "s2", role: "scout", task: "x", model: M }] }, h.ctx), /failed after starting \[none\][\s\S]*blocked during startup/);
  const pane = h.run(runId).panes.find((p) => p.name === "s2")!;
  assert.equal(pane.status, "exited");
  const cleaned = await h.collab.cleanup({ runId });
  assert.deepEqual(cleaned.closed.sort(), ["s1", "s2"]);
});

test("agent start retries while a new pane's shell is still starting", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  const { runId } = await h.collab.spawn({ workers: [{ name: "s1", role: "scout", task: "x", model: M }] }, h.ctx);
  h.patchState((s) => { s.behaviors[`${runId}-s2`] = { notShell: 2 }; });
  const second = await h.collab.spawn({ runId, workers: [{ name: "s2", role: "scout", task: "x", model: M }] }, h.ctx);
  assert.equal(second.panes[0]!.status, "running");
  assert.equal(h.state().calls.filter((c: string[]) => c[1] === "start" && c[2] === `${runId}-s2`).length, 3);
});

test("collect returns the pane tail with a truncated flag", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  const { runId } = await h.collab.spawn({ workers: [{ name: "s1", role: "scout", task: "x", model: M }] }, h.ctx);
  h.patchState((s) => {
    const pane = Object.values(s.panes as Record<string, any>).find((p) => p.agent?.name === `${runId}-s1`);
    pane.screen.push(...Array.from({ length: 300 }, (_, i) => `line ${i}`), "STATUS: done");
  });
  const small = await h.collab.collect({ runId, lines: 50 });
  assert.equal(small.results[0]!.truncated, true);
  assert.match(small.results[0]!.tail, /STATUS: done$/);
  assert.equal(small.results[0]!.tail.split("\n").length, 50);
  const all = await h.collab.collect({ runId, lines: 2_000 });
  assert.equal(all.results[0]!.truncated, false);
  assert.equal(all.results[0]!.diff, undefined, "read-only panes have no diff");
});

// ---------------------------------------------------------------------------
// Workspace lifecycle, merge, conflict, cleanup (real jj / git in a temp repo)

test("jj: lone worker uses the main checkout; parallel workers get workspaces; clean merge seals; panes stay alive until cleanup", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const lone = await h.collab.spawn({ newRun: true, workers: [worker("solo")] }, h.ctx);
  assert.equal(lone.panes[0]!.placement, "main");
  assert.equal(lone.panes[0]!.cwd, h.repo);
  assert.equal(lone.panes[0]!.workspace, undefined);
  await h.collab.cleanup({ runId: lone.runId });

  const { runId, panes } = await h.collab.spawn({ newRun: true, workers: [worker("w1"), worker("w2")] }, h.ctx);
  const [w1, w2] = panes.map((p) => p.workspace!);
  assert.equal(w1!.path, path.join(h.deps.wsRoot, runId, "w1"));
  assert.ok(fs.existsSync(path.join(w1!.path, "f.txt")));
  const paneEnv = Object.values(h.state().panes as Record<string, any>).find((p) => p.agent?.name === `${runId}-w1`).env;
  assert.deepEqual([paneEnv.PI_HERDR_COLLAB_RUN, paneEnv.PI_HERDR_COLLAB_NAME, paneEnv.PI_HERDR_COLLAB_WORKER], [runId, "w1", "1"]);
  assert.match(h.state().prompts.find((p: any) => p.target === `${runId}-w1`).text, new RegExp(`^\\[herdr run ${runId} · w1 \\(worker\\)\\]`));
  const prompt = fs.readFileSync(argvOf(h, `${runId}-w1`)[argvOf(h, `${runId}-w1`).indexOf("--system-prompt") + 1], "utf8");
  assert.match(prompt, /<herdr_pane run="hc[0-9a-f]{6}" name="w1" role="worker"\/>/);
  assert.match(prompt, /herdr_send/);

  fs.writeFileSync(path.join(w1!.path, "a.txt"), "from w1\n");
  fs.writeFileSync(path.join(w2!.path, "f.txt"), "one\ntwo\nthree\nfour\n");
  const collected = await h.collab.collect({ runId, names: ["w1"] });
  assert.match(collected.results[0]!.diff!.stat, /a\.txt/);
  assert.equal(collected.results[0]!.diff!.snapshotted, true);

  const merged = await h.collab.merge({ runId }, h.ctx) as any;
  assert.equal(merged.status, "merged");
  assert.deepEqual(merged.merged, ["w1", "w2"]);
  const run = h.run(runId);
  const integration = run.integration!;
  assert.equal(fs.readFileSync(path.join(integration.path, "a.txt"), "utf8"), "from w1\n");
  assert.match(fs.readFileSync(path.join(integration.path, "f.txt"), "utf8"), /four/);
  // Merged panes stay alive: no /quit, no cd, workspaces kept until herdr_cleanup.
  assert.ok(fs.existsSync(w1!.path) && fs.existsSync(w2!.path));
  assert.deepEqual(h.state().quits, []);
  assert.ok(run.panes.every((p) => p.status === "running" && p.merge === "merged" && p.workspace!.mergedHead));
  assert.equal(h.state().panes[run.panes[0]!.paneId!].agent.name, `${runId}-w1`);
  const description = h.jj(["--ignore-working-copy", "log", "-r", integration.head!, "--no-graph", "-T", "description"]);
  assert.match(description, new RegExp(`herdr ${runId}: integrate w1, w2`));

  // Next wave starts from the sealed integration change, and its lone writer still gets a workspace.
  const next = await h.collab.spawn({ runId, workers: [worker("w3")] }, h.ctx);
  assert.equal(next.panes[0]!.placement, "workspace");
  const parent = h.jj(["--ignore-working-copy", "log", "-r", `${runId}-w3@-`, "--no-graph", "-T", "change_id"]);
  assert.equal(parent.trim(), integration.head);

  const cleaned = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.deepEqual(cleaned.removed.sort(), ["integration", "w1", "w2", "w3"]);
  assert.ok(!fs.existsSync(w1!.path) && !fs.existsSync(w2!.path));
  assert.doesNotMatch(h.jj(["--ignore-working-copy", "workspace", "list"]), new RegExp(runId));
  assert.ok(!h.state().closed.includes("w1:p0"), "parent pane is never closed");
  assert.ok(fs.existsSync(path.join(h.deps.wsRoot, runId, "run.json")), "registry survives cleanup");
});

test("jj: conflicting workers keep workspaces, spawn one fixer, then merge after resolution", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj", answers: routerAnswers("frontier") });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2")] }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "f.txt"), "ONE-w1\ntwo\nthree\n");
  fs.writeFileSync(path.join(panes[1]!.workspace!.path, "f.txt"), "ONE-w2\ntwo\nthree\n");
  const conflict = await h.collab.merge({ runId }, h.ctx) as any;
  assert.equal(conflict.status, "conflict");
  assert.deepEqual(conflict.files, ["f.txt"]);
  assert.deepEqual(conflict.workers, ["w1", "w2"]);
  assert.equal(conflict.fixer, "fixer");
  const run = h.run(runId);
  const fixer = run.panes.find((p) => p.name === "fixer")!;
  assert.equal(fixer.role, "deep-worker");
  assert.equal(fixer.kind, "fixer");
  assert.equal(fixer.cwd, run.integration!.path);
  assert.deepEqual([fixer.model, fixer.modelSource], ["litellm/model-frontier", "pinned"], "fixer keeps the pinned deep-worker model");
  assert.equal(h.fetches(), 0);
  const brief = h.state().prompts.find((p: any) => p.target === `${runId}-fixer`).text;
  assert.match(brief, /f\.txt \(touched by w1, w2\)/);
  assert.match(brief, /### w1 \(worker\)[\s\S]*task for w1[\s\S]*### w2 \(worker\)/);
  assert.ok(fs.existsSync(panes[0]!.workspace!.path) && fs.existsSync(panes[1]!.workspace!.path), "conflicted workspaces are kept");
  await assert.rejects(h.collab.spawn({ runId, workers: [worker("w3")] }, h.ctx), /unresolved conflicts/);
  await assert.rejects(h.collab.merge({ runId, workers: ["w1"] }, h.ctx), /unresolved conflicts/);
  const still = await h.collab.merge({ runId }, h.ctx) as any;
  assert.equal(still.status, "conflict");

  fs.writeFileSync(path.join(run.integration!.path, "f.txt"), "ONE-both\ntwo\nthree\n");
  const resolved = await h.collab.merge({ runId }, h.ctx) as any;
  assert.equal(resolved.status, "merged");
  assert.deepEqual(resolved.merged, ["w1", "w2"]);
  const after = h.run(runId);
  assert.equal(after.integration!.pending, undefined);
  assert.ok(after.panes.filter((p) => p.kind === "worker").every((p) => p.merge === "merged" && !p.workspace!.removed));
  const conflicted = h.jj(["--ignore-working-copy", "log", "-r", after.integration!.head!, "--no-graph", "-T", 'if(conflict, "yes", "no")']);
  assert.equal(conflicted.trim(), "no");
});

test("jj: an edit after the merge snapshot keeps the workspace; queued messages block merge", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2")] }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "a.txt"), "a\n");
  fs.writeFileSync(path.join(panes[1]!.workspace!.path, "b.txt"), "b\n");
  await updateQueued(h, runId, { from: "w2", to: "w1" });
  await assert.rejects(h.collab.merge({ runId }, h.ctx), /message mq is still queued for w1/);
  await core.updateRun(h.deps, runId, (run) => { run.messages = []; });
  assert.equal((await h.collab.merge({ runId }, h.ctx) as any).status, "merged");
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "late.txt"), "written after merge\n");
  const cleaned = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.match(cleaned.kept.w1!, /edited after merge; merge status cleared/);
  assert.ok(cleaned.removed.includes("w2"));
  assert.ok(fs.existsSync(path.join(panes[0]!.workspace!.path, "late.txt")), "late edit survives");
  const w1 = h.run(runId).panes.find((p) => p.name === "w1")!;
  assert.equal(w1.merge, undefined);
  assert.equal(w1.workspace!.removed, false);
});

test("jj: cleanup never removes a workspace whose pane failed to close", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2")] }, h.ctx);
  h.patchState((s) => { s.closeFail = [panes[0]!.paneId]; });
  const cleaned = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.match(cleaned.errors.w1!, /close refused/);
  assert.match(cleaned.kept.w1!, /pane not closed/);
  assert.ok(fs.existsSync(panes[0]!.workspace!.path));
  assert.deepEqual(cleaned.removed, ["w2"]);
});

test("jj: parallel spawn after a main-checkout writer needs an explicit base", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const { runId } = await h.collab.spawn({ workers: [worker("solo")] }, h.ctx);
  await assert.rejects(h.collab.spawn({ runId, workers: [worker("w1"), worker("w2")] }, h.ctx), /main-checkout writer \(solo\)[\s\S]*pass base explicitly/);
  assert.equal(h.run(runId).panes.length, 1, "nothing created by the refused spawn");
  const explicit = await h.collab.spawn({ runId, base: "@-", workers: [worker("w1"), worker("w2")] }, h.ctx);
  assert.equal(explicit.panes[0]!.placement, "workspace");
  fs.writeFileSync(path.join(explicit.panes[0]!.workspace!.path, "a.txt"), "a\n");
  await h.collab.merge({ runId }, h.ctx);
  const later = await h.collab.spawn({ runId, workers: [worker("w3")] }, h.ctx);
  assert.equal(later.panes[0]!.placement, "workspace", "guard expires once the run has an integration");
});

test("run lock: an ownerless lock older than 30 s is stale; a fresh one is honored", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  const { runId } = await h.collab.spawn({ workers: [{ name: "s1", role: "scout", task: "x", model: M }] }, h.ctx);
  const lock = path.join(h.deps.wsRoot, runId, ".locks", "run");
  fs.mkdirSync(lock, { recursive: true });
  const fast = { ...h.deps, timing: { ...h.deps.timing, lockTimeoutMs: 200 } };
  await assert.rejects(core.updateRun(fast, runId, () => undefined), /timed out waiting for lock/);
  const old = new Date(Date.now() - 60_000);
  fs.utimesSync(lock, old, old);
  await core.updateRun(fast, runId, (run) => { run.messageCapPerPair = 3; });
  assert.equal(h.run(runId).messageCapPerPair, 3);
});

test("jj: re-merging a pane edited after merge keeps earlier integrated work", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2")] }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "a.txt"), "a\n");
  fs.writeFileSync(path.join(panes[1]!.workspace!.path, "b.txt"), "b\n");
  await h.collab.merge({ runId }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "late.txt"), "late\n");
  const cleaned = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.deepEqual(cleaned.removed, ["w2"]);
  const again = await h.collab.merge({ runId, workers: ["w1"] }, h.ctx) as any;
  assert.equal(again.status, "merged");
  const files = h.jj(["--ignore-working-copy", "file", "list", "-r", again.head]).split("\n");
  for (const file of ["a.txt", "b.txt", "late.txt", "f.txt"]) assert.ok(files.includes(file), `${file} in re-merged head`);
  assert.ok(!files.some((file) => file.startsWith(".codegraph")), "codegraph index never tracked");
});

test("jj: cleanup keeps unmerged changes, a starting pane's workspace, and late integration edits", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2"), worker("w3")] }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "a.txt"), "a\n");
  fs.writeFileSync(path.join(panes[2]!.workspace!.path, "c.txt"), "c\n");
  await h.collab.merge({ runId, workers: ["w3"] }, h.ctx);
  fs.writeFileSync(path.join(h.run(runId).integration!.path, "late.txt"), "after seal\n");
  await core.updateRun(h.deps, runId, (run) => {
    const pane = run.panes.find((p) => p.name === "w2")!;
    pane.status = "starting";
    delete pane.paneId;
  });
  const cleaned = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.match(cleaned.kept.w1!, /changes never merged/, "unmerged work is kept even without mergedHead");
  assert.match(cleaned.kept.w2!, /pane not closed/, "an in-flight spawn's workspace is kept");
  assert.deepEqual(cleaned.removed, ["w3"]);
  assert.match(cleaned.kept.integration!, /worker workspaces kept: w1, w2/, "integration stays while kept workers may re-merge");
  // Once the workers are gone, a late edit in the integration working copy still keeps it.
  await core.updateRun(h.deps, runId, (run) => {
    for (const pane of run.panes) if (pane.workspace) pane.workspace.removed = true;
  });
  const second = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.match(second.kept.integration!, /edits after the sealed head/);
});

test("jj: merge refuses while the integration working copy has edits after the sealed head", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2"), worker("w3")] }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "a.txt"), "a\n");
  fs.writeFileSync(path.join(panes[2]!.workspace!.path, "c.txt"), "c\n");
  const first = await h.collab.merge({ runId, workers: ["w3"] }, h.ctx) as any;
  assert.equal(first.status, "merged");
  const integration = h.run(runId).integration!;
  // A late fixer turn writes into the empty child that sealIntegration left as @.
  fs.writeFileSync(path.join(integration.path, "late.txt"), "after seal\n");
  await assert.rejects(h.collab.merge({ runId, workers: ["w1"] }, h.ctx), /integration working copy has edits after the sealed head[\s\S]*jj squash/);
  const refused = h.run(runId);
  assert.equal(refused.integration!.head, first.head, "sealed head unchanged");
  const w1 = refused.panes.find((p) => p.name === "w1")!;
  assert.equal(w1.merge, undefined, "w1 not marked merged");
  assert.equal(h.jj(["log", "--no-graph", "-r", "@", "-T", 'if(empty, "empty", "edited")'], w1.workspace!.path), "edited", "w1 not frozen by the refused merge");
  assert.equal(fs.readFileSync(path.join(integration.path, "late.txt"), "utf8"), "after seal\n", "late edit survives");
  // Folding the late edit into the sealed head lets the next wave include it.
  h.jj(["squash"], integration.path);
  const second = await h.collab.merge({ runId, workers: ["w1"] }, h.ctx) as any;
  assert.equal(second.status, "merged");
  const files = h.jj(["--ignore-working-copy", "file", "list", "-r", second.head]).split("\n");
  for (const file of ["a.txt", "c.txt", "late.txt"]) assert.ok(files.includes(file), `${file} in merged head`);
});

test("jj: a failed fixer close keeps the integration workspace", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2")] }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "f.txt"), "X\ntwo\nthree\n");
  fs.writeFileSync(path.join(panes[1]!.workspace!.path, "f.txt"), "Y\ntwo\nthree\n");
  assert.equal((await h.collab.merge({ runId }, h.ctx) as any).fixer, "fixer");
  const run = h.run(runId);
  fs.writeFileSync(path.join(run.integration!.path, "f.txt"), "XY\ntwo\nthree\n");
  assert.equal((await h.collab.merge({ runId }, h.ctx) as any).status, "merged");
  h.patchState((s) => { s.closeFail = [run.panes.find((p) => p.name === "fixer")!.paneId]; });
  const cleaned = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.ok(cleaned.errors.fixer);
  assert.match(cleaned.kept.integration!, /panes still open there: fixer/);
  assert.ok(fs.existsSync(run.integration!.path));
});

test("git: cleanup with a pending conflict keeps every worktree, including remaining sides", async (t) => {
  const h = setup({ repo: "git" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2"), worker("w3")] }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "f.txt"), "X\ntwo\nthree\n");
  fs.writeFileSync(path.join(panes[1]!.workspace!.path, "f.txt"), "Y\ntwo\nthree\n");
  fs.writeFileSync(path.join(panes[2]!.workspace!.path, "c.txt"), "c\n");
  const conflict = await h.collab.merge({ runId, spawnFixer: false }, h.ctx) as any;
  assert.deepEqual([conflict.status, conflict.workers, conflict.remaining], ["conflict", ["w1", "w2"], ["w3"]]);
  assert.equal(h.run(runId).panes.find((p) => p.name === "w3")!.workspace!.mergedHead, undefined, "remaining side is not marked merged");
  const cleaned = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.deepEqual(cleaned.removed, []);
  assert.match(cleaned.kept.w3!, /unresolved conflicts/);
  assert.ok(panes.every((p) => fs.existsSync(p.workspace!.path)));
});

test("codegraph init: once per run-owned workspace, never the main checkout, .codegraph never tracked", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const lone = await h.collab.spawn({ newRun: true, workers: [worker("solo")] }, h.ctx);
  assert.equal(lone.panes[0]!.codegraph, undefined);
  assert.equal(h.codegraphCalls().length, 0, "not run for the main checkout");
  await h.collab.cleanup({ runId: lone.runId });
  const { runId, panes } = await h.collab.spawn({ newRun: true, workers: [worker("w1"), worker("w2")] }, h.ctx);
  const calls = h.codegraphCalls();
  assert.deepEqual(calls.map((c) => [c.cwd, c.args[0], c.args[1], c.noDaemon]), panes.map((p) => [p.workspace!.path, "init", p.workspace!.path, "1"]));
  assert.ok(panes.every((p) => p.codegraph === "ok"));
  assert.equal(fs.readFileSync(path.join(panes[0]!.workspace!.path, ".codegraph", ".gitignore"), "utf8"), "*\n", "index ignores itself");
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "a.txt"), "a\n");
  const merged = await h.collab.merge({ runId }, h.ctx) as any;
  assert.doesNotMatch(h.jj(["--ignore-working-copy", "file", "list", "-r", merged.head]), /\.codegraph/);
  const review = await h.collab.review({ runId, target: "t", reviewers: [{ name: "r1", lens: "a", model: M }, { name: "r2", lens: "b", model: M }] }, h.ctx);
  assert.equal(h.codegraphCalls().length, 3, "integration workspace initialised once for two reviewers");
  assert.deepEqual(review.panes.map((p) => p.codegraph), ["ok", "ok"]);
  await h.collab.review({ runId, target: "t", reviewers: [{ name: "r3", lens: "c", model: M }] }, h.ctx);
  assert.equal(h.codegraphCalls().length, 3, "still once per workspace");
});

test("codegraph init: failure, timeout, missing binary, and an existing index never fail the spawn", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  h.codegraphMode("fail");
  const failed = await h.collab.spawn({ newRun: true, workers: [worker("w1"), worker("w2")] }, h.ctx);
  assert.ok(failed.panes.every((p) => p.codegraph === "failed:exit 3 boom" && p.status === "running"));
  h.codegraphMode("hang");
  const slow = core.createHerdrCollab({ ...h.deps, timing: { ...h.deps.timing, codegraphTimeoutMs: 300 } });
  const timedOut = await slow.spawn({ newRun: true, workers: [worker("w1"), worker("w2")] }, h.ctx);
  assert.ok(timedOut.panes.every((p) => p.codegraph === "failed:timeout" && p.status === "running"));
  const missing = core.createHerdrCollab({ ...h.deps, codegraphBin: "codegraph-missing-xyz" });
  const noBinary = await missing.spawn({ newRun: true, workers: [worker("w1"), worker("w2")] }, h.ctx);
  assert.ok(noBinary.panes.every((p) => p.codegraph === "missing" && p.status === "running"));
  fs.writeFileSync(path.join(noBinary.panes[0]!.workspace!.path, "a.txt"), "a\n");
  await missing.merge({ runId: noBinary.runId }, h.ctx);
  fs.mkdirSync(path.join(h.run(noBinary.runId).integration!.path, ".codegraph"));
  const before = h.codegraphCalls().length;
  const review = await missing.review({ runId: noBinary.runId, target: "t", reviewers: [{ name: "r1", lens: "a", model: M }] }, h.ctx);
  assert.equal(review.panes[0]!.codegraph, "skipped-exists");
  assert.equal(h.codegraphCalls().length, before);
});

test("codegraph init: hard timeout kills a SIGTERM-ignoring tool and its grandchild; index ignores itself on failure", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  h.codegraphMode("fail-after-mkdir");
  const failed = await h.collab.spawn({ newRun: true, workers: [worker("w1"), worker("w2")] }, h.ctx);
  assert.equal(failed.panes[0]!.codegraph, "failed:exit 4");
  assert.equal(fs.readFileSync(path.join(failed.panes[0]!.workspace!.path, ".codegraph", ".gitignore"), "utf8"), "*\n");
  h.codegraphMode("stubborn");
  const bounded = core.createHerdrCollab({ ...h.deps, timing: { ...h.deps.timing, codegraphTimeoutMs: 300 } });
  const started = Date.now();
  const slow = await bounded.spawn({ newRun: true, workers: [worker("w1"), worker("w2")] }, h.ctx);
  assert.ok(Date.now() - started < 8_000, "spawn is not held open by the stubborn tool or its grandchild");
  assert.ok(slow.panes.every((p) => p.codegraph === "failed:timeout" && p.status === "running"));
  assert.equal(fs.readFileSync(path.join(slow.panes[0]!.workspace!.path, ".codegraph", ".gitignore"), "utf8"), "*\n");
  const grandchild = Number(fs.readFileSync(`${h.env.FAKE_CODEGRAPH_LOG}.grandchild`, "utf8"));
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.throws(() => process.kill(grandchild, 0), /ESRCH/, "the whole process group was SIGKILLed");
});

test("jj: merge checks the integration first and freezes a side only when its @ is non-empty", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2")] }, h.ctx);
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "a.txt"), "a\n");
  fs.writeFileSync(path.join(panes[1]!.workspace!.path, "b.txt"), "b\n");
  const jjName = (name: string) => h.run(runId).panes.find((p) => p.name === name)!.workspace!.jjName!;
  const count = (name: string) => h.jj(["--ignore-working-copy", "log", "--no-graph", "-r", `${panes[0]!.workspace!.base}..${jjName(name)}@`, "-T", '"x\\n"']).trim().split("\n").length;
  // An integration that cleanup removed: merge refuses before touching any workspace.
  await core.updateRun(h.deps, runId, (run) => { run.integration = { path: path.join(h.tmp, "gone"), head: "abc", merges: [] }; });
  await assert.rejects(h.collab.merge({ runId }, h.ctx), /start a new run with base abc/);
  assert.equal(count("w1"), 1, "w1 not frozen by the refused merge");
  await assert.rejects(h.collab.spawn({ runId, workers: [worker("w3")] }, h.ctx), /start a new run with base abc/);
  await assert.rejects(h.collab.review({ runId, target: "t", reviewers: [{ name: "r1", lens: "a", model: M }] }, h.ctx), /start a new run with base abc/);
  await core.updateRun(h.deps, runId, (run) => { delete run.integration; });
  // Simulate a side already frozen by an earlier, failed merge attempt.
  h.jj(["new"], panes[0]!.workspace!.path);
  assert.equal(count("w1"), 2);
  const merged = await h.collab.merge({ runId }, h.ctx) as any;
  assert.equal(merged.status, "merged");
  assert.equal(count("w1"), 2, "no extra empty commit when @ was already empty");
  assert.equal(count("w2"), 2, "a non-empty side is frozen once");
  assert.ok(h.jj(["--ignore-working-copy", "file", "list", "-r", merged.head]).includes("a.txt"));
});

test("git fallback: worktrees per worker, sequential merges, cleanup pins refs/herdr/<run>", async (t) => {
  const h = setup({ repo: "git" });
  t.after(h.done);
  const { runId, panes } = await h.collab.spawn({ workers: [worker("w1"), worker("w2")] }, h.ctx);
  assert.equal(h.run(runId).vcs, "git");
  assert.equal(panes[0]!.workspace!.vcs, "git");
  fs.writeFileSync(path.join(panes[0]!.workspace!.path, "a.txt"), "a\n");
  fs.writeFileSync(path.join(panes[1]!.workspace!.path, "b.txt"), "b\n");
  const collected = await h.collab.collect({ runId, names: ["w1"] });
  assert.match(collected.results[0]!.diff!.stat, /untracked:\na\.txt/);
  const merged = await h.collab.merge({ runId }, h.ctx) as any;
  assert.equal(merged.status, "merged");
  const integration = h.run(runId).integration!;
  assert.ok(fs.existsSync(path.join(integration.path, "a.txt")) && fs.existsSync(path.join(integration.path, "b.txt")));
  assert.ok(fs.existsSync(panes[0]!.workspace!.path), "kept until cleanup");
  const cleaned = await h.collab.cleanup({ runId, removeWorkspaces: true });
  assert.deepEqual(cleaned.removed.sort(), ["integration", "w1", "w2"]);
  assert.equal(h.git(["rev-parse", `refs/herdr/${runId}`]).trim(), integration.head, "integration head pinned by a non-branch ref");
  assert.doesNotMatch(h.git(["branch", "--list"]), /herdr/, "no branches created");
});

test("parallel writers outside jj/git are refused before any side effect", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  await assert.rejects(h.collab.spawn({ workers: [worker("w1"), worker("w2")] }, h.ctx), /need isolated workspaces/);
  assert.equal(h.fetches(), 0);
  assert.equal(h.state().calls.length, 0);
});

test("cleanup survives a restart: a fresh instance loads run.json by run id", async (t) => {
  const h = setup({ repo: "none" });
  t.after(h.done);
  const { runId } = await h.collab.spawn({ workers: [{ name: "s1", role: "scout", task: "x", model: M }] }, h.ctx);
  const restarted = core.createHerdrCollab(h.deps);
  await assert.rejects(restarted.cleanup({}), /no current run.*runs on disk: hc/);
  const result = await restarted.cleanup({ runId });
  assert.deepEqual(result.closed, ["s1"]);
  assert.equal(h.run(runId).panes[0]!.status, "closed");
});

// ---------------------------------------------------------------------------
// Peer messaging (decision 15)

async function messagingRun(h: Harness, cap?: number) {
  const { runId } = await h.collab.spawn({ messageCap: cap, workers: [worker("w1"), worker("w2", { role: "delegate" })] }, h.ctx);
  await h.collab.review({ runId, target: "review", reviewers: [{ name: "r1", lens: "security", model: M }] }, h.ctx);
  return runId;
}

test("messaging: queued until the target is idle, then delivered and logged", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const runId = await messagingRun(h);
  h.patchState((s) => {
    const pane = Object.values(s.panes as Record<string, any>).find((p) => p.agent?.name === `${runId}-w2`);
    pane.agent.status = "working";
    s.behaviors[`${runId}-w2`] = { busyUntilWait: true };
    s.calls = [];
  });
  const sent = await h.collab.send({ runId, name: "w1" }, { to: "w2", message: "I renamed foo() to bar()." });
  assert.equal(sent.message.status, "delivered");
  const calls = h.state().calls.map((c: string[]) => `${c[0]} ${c[1]} ${c[2]}`);
  const waitAt = calls.indexOf(`agent wait ${runId}-w2`);
  const promptAt = calls.indexOf(`agent prompt ${runId}-w2`);
  assert.ok(waitAt !== -1 && promptAt > waitAt, "prompt is sent only after the working target settled");
  const delivered = h.state().prompts.find((p: any) => p.target === `${runId}-w2` && p.text.includes("renamed"));
  assert.equal(delivered.statusBefore, "idle");
  assert.match(delivered.text, /^\[herdr message m1 from w1 to w2 · run hc/);
  const log = h.run(runId).messages;
  assert.equal(log.length, 1);
  assert.deepEqual([log[0]!.from, log[0]!.to, log[0]!.status, log[0]!.text], ["w1", "w2", "delivered", "I renamed foo() to bar()."]);
  assert.ok(Date.parse(log[0]!.ts) > 0);
  const peers = await h.collab.peers({ runId, name: "w1" });
  assert.equal(peers.peers.find((p) => p.name === "w2")!.remaining, 4);
  assert.equal(peers.peers.find((p) => p.name === "r1")!.reachable, false);
  const collected = await h.collab.collect({ runId, names: ["w2"], diff: false });
  assert.equal(collected.results[0]!.messages.length, 1);
});

test("messaging: per-pair cap, self-send, unknown target, blocked target, and mutual wait", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const runId = await messagingRun(h, 2);
  const w1 = { runId, name: "w1" };
  await h.collab.send(w1, { to: "w2", message: "one" });
  await h.collab.send(w1, { to: "w2", message: "two" });
  await assert.rejects(h.collab.send(w1, { to: "w2", message: "three" }), /cap of 2 messages from w1 to w2/);
  await h.collab.send({ runId, name: "w2" }, { to: "w1", message: "other direction has its own cap" });
  await assert.rejects(h.collab.send(w1, { to: "w1", message: "me" }), /cannot message itself/);
  await assert.rejects(h.collab.send(w1, { to: "ghost", message: "hi" }), /unknown target "ghost". Peers: w2/);
  h.patchState((s) => {
    Object.values(s.panes as Record<string, any>).find((p) => p.agent?.name === `${runId}-w2`).agent.status = "blocked";
  });
  await assert.rejects(h.collab.send({ runId, name: "parent" }, { to: "w2", message: "status?" }), /blocked/);
  assert.equal(h.run(runId).messages.at(-1)!.status, "failed");
  await updateQueued(h, runId, { from: "w1", to: "w2" });
  await assert.rejects(h.collab.send({ runId, name: "w2" }, { to: "w1", message: "x" }), /waiting to deliver message/);
});

async function updateQueued(h: Harness, runId: string, pair: { from: string; to: string }) {
  await core.updateRun(h.deps, runId, (run) => {
    run.messages.push({ id: "mq", ...pair, ts: new Date().toISOString(), text: "pending", status: "queued" });
  });
}

test("messaging: reviewers stay isolated; the parent can still reach them", { skip: !hasJj }, async (t) => {
  const h = setup({ repo: "jj" });
  t.after(h.done);
  const runId = await messagingRun(h);
  const reviewerArgv = argvOf(h, `${runId}-r1`);
  const tools = reviewerArgv[reviewerArgv.indexOf("--tools") + 1]!;
  assert.ok(!tools.includes("herdr_send") && !tools.includes("herdr_peers"), "reviewer panes get no messaging tools");
  const writerArgv = argvOf(h, `${runId}-w1`);
  assert.match(writerArgv[writerArgv.indexOf("--tools") + 1]!, /herdr_peers,herdr_send/);
  await assert.rejects(h.collab.send({ runId, name: "w1" }, { to: "r1", message: "look at this" }), /reviewer panes are isolated/);
  await assert.rejects(h.collab.send({ runId, name: "r1" }, { to: "w1", message: "hi" }), /read-only and isolated/);
  await assert.rejects(h.collab.peers({ runId, name: "r1" }), /read-only and isolated/);
  const sent = await h.collab.send({ runId, name: "parent" }, { to: "r1", message: "Repeat your verdict line." });
  assert.equal(sent.message.status, "delivered");
});

// ---------------------------------------------------------------------------
// Extension registration (same vm evaluation style as tests/typesafe-router.test.ts)

function loadExtension(env: Record<string, string | undefined>) {
  const source = fs.readFileSync(path.join(root, "extensions", "herdr-collab.ts"), "utf8");
  const js = stripTypeScriptTypes(source)
    .replace(/^import[\s\S]*?from\s+"[^"]+";\s*$/gm, "")
    .replace("export default function", "function register");
  const Type = new Proxy({}, { get: (_t, kind) => (...args: unknown[]) => ({ kind, args }) });
  const register = vm.runInNewContext(`${js}\nregister;`, {
    ...core, Type, homedir: os.homedir, join: path.join, resolve: path.resolve,
    __dirname: path.join(root, "extensions"), process: { env }, fetch,
  });
  const tools: string[] = [];
  const events: string[] = [];
  register({ registerTool: (tool: { name: string }) => tools.push(tool.name), on: (name: string) => events.push(name) });
  return { tools, events };
}

test("extension registers parent tools only inside herdr, worker tools only in herdr panes", () => {
  assert.deepEqual(loadExtension({}).tools, []);
  assert.deepEqual(loadExtension({ HERDR_ENV: "1", PI_SUBAGENT_CHILD: "1" }).tools, []);
  assert.deepEqual(loadExtension({ HERDR_ENV: "1", HERDR_PANE_ID: "w1:p1" }).tools,
    ["herdr_spawn", "herdr_review", "herdr_wait", "herdr_collect", "herdr_merge", "herdr_cleanup", "herdr_peers", "herdr_send"]);
  const pane = loadExtension({ HERDR_ENV: "1", PI_HERDR_COLLAB_WORKER: "1", PI_HERDR_COLLAB_RUN: "hcabc123", PI_HERDR_COLLAB_NAME: "w1" });
  assert.deepEqual(pane.tools, ["herdr_peers", "herdr_send"]);
  assert.deepEqual(pane.events, ["before_agent_start"]);
});
