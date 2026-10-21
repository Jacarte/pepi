// Runs only while building the image, never while evaluating a task.
import { readFileSync, realpathSync, accessSync, writeFileSync, constants } from "node:fs";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

// The final image copies this stage's /usr/local (Pi's Node) to /opt/pepi-node, after the task image's PATH.
export const PI_NODE = "/opt/pepi-node/bin/node";

// A provisioned server with `#!/usr/bin/env node` would start on the first `node` on PATH: the task image's
// (Node 20 in the Multilingual JS images). Pin it to Pi's Node. Other shebangs (`env -S`, shell wrappers) stay.
export function pinNodeShebang(file) {
  const data = readFileSync(file);
  const match = /^#![ \t]*\/usr\/bin\/env[ \t]+node(?=[ \t\r\n]|$)/.exec(data.subarray(0, 256).toString("latin1"));
  if (!match) return false;
  writeFileSync(file, Buffer.concat([Buffer.from(`#!${PI_NODE}`), data.subarray(match[0].length)]));
  return true;
}

function main() {
  const root = "/opt/pepi-mcp";
  const { commands } = JSON.parse(readFileSync(`${root}/policy.json`, "utf8"));
  if (commands.length) {
    const result = spawnSync("npm", ["ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], {
      cwd: root, stdio: "inherit",
    });
    if (result.error) throw result.error;
    if (result.status !== 0) process.exit(result.status ?? 1);
    for (const command of commands) {
      const binary = `${root}/node_modules/.bin/${command}`;
      const target = realpathSync(binary);
      if (!target.startsWith(`${root}/node_modules/`)) {
        throw new Error(`MCP binary escapes provisioned dependencies: ${command}`);
      }
      accessSync(binary, constants.X_OK);
      if (pinNodeShebang(target)) console.log(`pepi-eval: ${command} pinned to ${PI_NODE}`);
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
