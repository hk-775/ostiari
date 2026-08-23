import assert from "node:assert/strict";
import test from "node:test";
import { selectSafeMcpCall } from "../src/lib/sandboxMcp";

test("MCP demo prefers a read-only discovery call", () => {
  assert.deepEqual(
    selectSafeMcpCall([
      { name: "drawio.create_diagram", server: "drawio" },
      { name: "fs.read_text_file", server: "filesystem" },
      { name: "fs.list_allowed_directories", server: "filesystem" },
    ]),
    { name: "fs.list_allowed_directories", params: {} },
  );
});

test("MCP demo never auto-executes an unknown discovered tool", () => {
  assert.equal(
    selectSafeMcpCall([
      { name: "github.create_issue", server: "github" },
      { name: "drawio.create_diagram", server: "drawio" },
    ]),
    undefined,
  );
});
