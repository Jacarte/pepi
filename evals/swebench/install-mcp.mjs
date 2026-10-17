// Runs only while building the image, never while evaluating a task.
import { readFileSync, realpathSync, accessSync, constants } from "node:fs";
import { spawnSync } from "node:child_process";

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
    if (!realpathSync(binary).startsWith(`${root}/node_modules/`)) {
      throw new Error(`MCP binary escapes provisioned dependencies: ${command}`);
    }
    accessSync(binary, constants.X_OK);
  }
}
