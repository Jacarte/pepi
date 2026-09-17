import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
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

function readFixture(name) {
  return JSON.parse(
    fs.readFileSync(path.join(__dirname, "fixtures", name), "utf8"),
  );
}

test("representative active board is valid", () => {
  const board = readFixture("valid-active.json");

  assert.equal(validate(board), true, JSON.stringify(validate.errors));
});

test("working task requires an assignment", () => {
  const board = readFixture("invalid-working-no-assignment.json");

  assert.equal(validate(board), false);
  assert.ok(validate.errors?.length > 0);
});
