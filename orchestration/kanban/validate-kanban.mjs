import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "kanban-state.schema.json"), "utf8"),
);

const ajv = new Ajv2020({ allErrors: true, strict: true });
addFormats(ajv);

const validate = ajv.compile(schema);
const target = process.argv[2];

if (!target) {
  console.error("Usage: node validate-kanban.mjs <kanban.json>");
  process.exit(2);
}

const absoluteTarget = path.resolve(process.cwd(), target);
const value = JSON.parse(fs.readFileSync(absoluteTarget, "utf8"));

if (!validate(value)) {
  console.error(JSON.stringify(validate.errors, null, 2));
  process.exit(1);
}

console.log(`Valid Kanban state: ${absoluteTarget}`);
