import assert from "node:assert/strict";
import { stripTypeScriptTypes } from "node:module";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { test } from "node:test";

const root = fileURLToPath(new URL("../", import.meta.url));
const source = readFileSync(join(root, "extensions/pi-typesafe-router.ts"), "utf8");
const profiles = Object.fromEntries(["codex", "bedrock"].map((name) => [name,
  JSON.parse(readFileSync(join(root, "profiles", name, "models-router.json"), "utf8")),
]));
const lanes = ["LOCAL", "CHEAP", "FRONTIER"];
const js = stripTypeScriptTypes(source)
  .replace(/^import .* from ".*";\s*$/gm, "")
  .replace("export default function", "function register");

// Execute the real extension, isolating only external dependencies. Never read
// machine-local overlays/settings, call providers, or write the real router log.
function setup(options: any = {}) {
  const config = structuredClone(options.config ?? profiles.codex);
  const scores = options.scores ?? { hard: 0.9, trivial: 0 };
  const requests: any[] = [];
  const selected: any[] = [];
  const models = lanes.map((lane) => ({
    provider: config[lane]?.provider?.trim?.(), id: config[lane]?.id?.trim?.(),
    input: ["text", "image"], contextWindow: 100_000,
  }));
  const ctx: any = {
    model: models[0],
    sessionManager: { getSessionId: () => "test-session" },
    modelRegistry: {
      find: (provider: string, id: string) => models.find((m) => m.provider === provider && m.id === id),
      getAvailable: () => models,
    },
    getContextUsage: () => ({ tokens: 100 }),
  };
  const register = vm.runInNewContext(`${js}\nregister;`, {
    __dirname: join(root, "extensions"), dirname, resolve,
    readFileSync: (path: string) => {
      assert.equal(path, join(root, "models-router.json"));
      return options.rawConfig ?? JSON.stringify(config);
    },
    appendFileSync() {}, mkdirSync() {},
    Type: Object.fromEntries(["Object", "Union", "Literal", "String", "Optional", "Boolean"]
      .map((name) => [name, (...args: any[]) => ({ name, args })])),
    Text: class {},
    process: { env: { TYPESAFE_API_KEY: "offline-test", ...options.env } },
    AbortSignal,
    fetch: async (url: string, request: any) => {
      requests.push({ url, ...request });
      options.onFetch?.(ctx);
      return { ok: options.httpStatus === undefined, status: options.httpStatus,
        json: async () => ({ answers: Object.fromEntries(
          Object.entries(scores).map(([name, noul]) => [name, { noul }]),
        ) }) };
    },
  });
  const tools: any[] = [];
  register({
    registerTool: (tool: any) => tools.push(tool),
    setModel: async (model: any) => {
      selected.push(model);
      if (options.refuseSwitch) return false;
      if (!options.loseSwitch) ctx.model = model;
      return true;
    },
  });
  assert.equal(tools.length, 1);
  const tool = tools[0];
  return { config, ctx, models, requests, selected, tool,
    run: (params: any = {}, signal?: AbortSignal) => tool.execute("test-call", {
      action: "recommend", task: "  diagnose a failure  ", ...params,
    }, signal, undefined, ctx),
  };
}

for (const child of [false, true]) {
  test(`explicit registerTool contract in ${child ? "child" : "parent"}`, async () => {
    const h = setup({ env: { PI_SUBAGENT_CHILD: child ? "1" : undefined } });
    assert.equal(h.tool.name, "jev_route");
    assert.equal(h.tool.executionMode, "sequential");
    assert.equal(h.requests.length, 0); // Registration never dispatches.
    const result = await h.run({ context: "supplied context" });
    assert.equal(result.details.lane, "FRONTIER");
    assert.equal(result.details.changed, false);
    assert.equal(h.selected.length, 0); // recommend never switches.
    assert.equal(h.requests.length, 1);
    const body = JSON.parse(h.requests[0].body);
    assert.deepEqual(body.state, { task: "diagnose a failure", context: "supplied context" });
    assert.deepEqual(Object.keys(body.questions).sort(), ["hard", "trivial"]);
  });
}

for (const [name, config] of Object.entries(profiles)) {
  test(`${name} profile thresholds, boundaries, precedence and fallback`, async () => {
    const frontier = name === "codex" ? 0.85 : 0.8;
    assert.equal(config.FRONTIER.threshold, frontier);
    assert.equal(config.LOCAL.threshold, 0.5);
    assert.equal(config.CHEAP.threshold, 0.3);
    const cases: [number, number, string][] = [
      [frontier - 0.000001, 0, "CHEAP"], [frontier, 0, "CHEAP"],
      [frontier + 0.000001, 0, "FRONTIER"], [1, 1, "FRONTIER"],
      [frontier, 0.5, "LOCAL"], [0.3, 0.5, "LOCAL"],
      [0, 0.499999, "CHEAP"], [0, 0.5, "LOCAL"], [0, 0.500001, "LOCAL"],
      [0.299999, 0, "CHEAP"], [0.3, 0, "CHEAP"], [0.300001, 0, "CHEAP"],
      [0, 0, "CHEAP"],
    ];
    for (const [hard, trivial, expected] of cases) {
      const h = setup({ config, scores: { hard, trivial } });
      const { details } = await h.run();
      assert.equal(details.lane, expected, `${name}: hard=${hard}, trivial=${trivial}`);
      assert.equal(details.model, `${config[expected].provider}/${config[expected].id}`);
    }
  });
}

test("custom thresholds and endpoint values are honored, reloaded on every call", async () => {
  const h = setup({ scores: { hard: 0.6, trivial: 0.6 } });
  h.config.FRONTIER.threshold = 0.55;
  assert.equal((await h.run()).details.lane, "FRONTIER");
  h.config.FRONTIER.threshold = 1;
  h.config.LOCAL.threshold = 0.7;
  h.config.CHEAP.threshold = 1;
  assert.equal((await h.run()).details.lane, "CHEAP");
  h.config.LOCAL.threshold = 0;
  h.config.CHEAP.threshold = 0;
  assert.equal((await h.run()).details.lane, "LOCAL");
  h.config.FRONTIER.threshold = 0;
  assert.equal((await h.run()).details.lane, "FRONTIER");
  const endpoints = setup({ config: h.config, scores: { hard: 0, trivial: 0 } });
  assert.equal((await endpoints.run()).details.lane, "LOCAL");
  endpoints.config.FRONTIER.threshold = 1;
  endpoints.config.LOCAL.threshold = 1;
  assert.equal((await setup({ config: endpoints.config, scores: { hard: 1, trivial: 1 } }).run()).details.lane, "LOCAL");
});

for (const lane of lanes) {
  test(`${lane}.threshold rejects missing, nonnumeric and out-of-range config before fetching`, async () => {
    for (const value of [undefined, null, "0.5", true, false, [], {}, -0.001, 1.001]) {
      const h = setup();
      h.config[lane].threshold = value;
      await assert.rejects(h.run(), new RegExp(`${lane}\\.threshold must be a finite number`));
      assert.equal(h.requests.length, 0);
      assert.equal(h.selected.length, 0);
    }
    // JSON permits exponent notation that overflows to +/-Infinity in JS.
    for (const value of ["1e400", "-1e400", "NaN"]) {
      const config = structuredClone(profiles.codex);
      config[lane].threshold = "INVALID";
      const h = setup({ rawConfig: JSON.stringify(config).replace('"INVALID"', value) });
      await assert.rejects(h.run());
      assert.equal(h.requests.length, 0);
    }
  });
}

test("model mapping validation and HUMAN-ignore behavior remain intact", async () => {
  for (const lane of lanes) {
    for (const entry of [undefined, null, [], {}, { provider: "", id: "x" },
      { provider: "p", id: " " }, { provider: 3, id: "x" }, { provider: " human ", id: "x" }]) {
      const h = setup();
      h.config[lane] = entry;
      await assert.rejects(h.run());
      assert.equal(h.requests.length, 0);
    }
  }
  const h = setup();
  h.config.FRONTIER.provider = ` ${h.config.FRONTIER.provider} `;
  h.config.FRONTIER.id = ` ${h.config.FRONTIER.id} `;
  for (const human of [undefined, null, [], { threshold: "invalid", provider: "human" }]) {
    h.config.HUMAN = human;
    assert.equal((await h.run()).details.model, "openai-codex/gpt-6-astra");
  }
});

test("invalid classifier probabilities still fail without switching", async () => {
  for (const name of ["hard", "trivial"]) {
    for (const value of [undefined, null, "0.5", NaN, Infinity, -Infinity, -0.1, 1.1]) {
      const h = setup({ scores: { hard: 0.9, trivial: 0, [name]: value } });
      await assert.rejects(h.run({ action: "switch_self" }), /invalid probability/);
      assert.equal(h.selected.length, 0);
    }
  }
});

test("switch_self changes only the calling context and skips an already selected model", async () => {
  const h = setup();
  const other = setup();
  const result = await h.run({ action: "switch_self" });
  assert.equal(result.details.changed, true);
  assert.equal(result.details.currentModel, result.details.model);
  assert.equal(h.selected.length, 1);
  assert.equal(other.ctx.model.id, profiles.codex.LOCAL.id);
  assert.equal((await h.run({ action: "switch_self" })).details.changed, false);
  assert.equal(h.selected.length, 1);
});

test("registry, authentication, image and context guards remain enforced", async () => {
  for (const [mutate, error] of [
    [(h: any) => { h.ctx.modelRegistry.find = () => undefined; }, /model not registered/],
    [(h: any) => { h.ctx.modelRegistry.getAvailable = () => []; }, /no configured authentication/],
    [(h: any) => { h.models[2].input = ["text"]; }, /does not support images/],
    [(h: any) => { h.ctx.getContextUsage = () => ({ tokens: 100_000 }); }, /context is too large/],
  ] as const) {
    const h = setup();
    mutate(h);
    await assert.rejects(h.run({ action: "switch_self", requiresImages: true }), error);
    assert.equal(h.selected.length, 0);
  }
  await assert.rejects(setup({ refuseSwitch: true }).run({ action: "switch_self" }), /Pi refused/);
  await assert.rejects(setup({ loseSwitch: true }).run({ action: "switch_self" }), /did not retain/);
});

test("blank tasks, absent credentials, HTTP errors, abort and session changes fail explicitly", async () => {
  const h = setup();
  await assert.rejects(h.run({ task: " " }), /must not be blank/);
  await assert.rejects(h.run({}, AbortSignal.abort()), /abort/i);
  assert.equal(h.requests.length, 0);
  await assert.rejects(setup({ env: { TYPESAFE_API_KEY: "" } }).run(), /missing TYPESAFE_API_KEY/);
  await assert.rejects(setup({ httpStatus: 503 }).run(), /TypeSafe HTTP 503/);
  const changing = setup({ onFetch: (ctx: any) => { ctx.sessionManager.getSessionId = () => "new-session"; } });
  await assert.rejects(changing.run({ action: "switch_self" }), /session changed/);
  assert.equal(changing.selected.length, 0);
});

test("profile.sh preserves each tracked router config when applying profiles", () => {
  const dir = mkdtempSync(join(tmpdir(), "typesafe-router-profile-"));
  try {
    copyFileSync(join(root, "profile.sh"), join(dir, "profile.sh"));
    for (const name of Object.keys(profiles)) {
      mkdirSync(join(dir, "profiles", name), { recursive: true });
      copyFileSync(join(root, "profiles", name, "models-router.json"), join(dir, "profiles", name, "models-router.json"));
    }
    for (const name of ["codex", "bedrock", "codex"]) {
      execFileSync("bash", [join(dir, "profile.sh"), name]);
      assert.deepEqual(JSON.parse(readFileSync(join(dir, "models-router.json"), "utf8")), profiles[name]);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
