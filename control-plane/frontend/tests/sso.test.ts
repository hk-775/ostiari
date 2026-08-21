import assert from "node:assert/strict";
import test from "node:test";
import { safeReturnPath } from "../src/lib/sso";

test("safeReturnPath accepts application-local routes", () => {
  assert.equal(safeReturnPath("/dashboard"), "/dashboard");
  assert.equal(
    safeReturnPath("/traces?gateway_id=gateway-1#trace-9"),
    "/traces?gateway_id=gateway-1#trace-9",
  );
});

test("safeReturnPath rejects external, protocol-relative, and authentication routes", () => {
  for (const unsafe of [
    null,
    "",
    "https://attacker.example",
    "//attacker.example/path",
    "dashboard",
    "/login",
    "/auth/sso-callback",
    "/auth/sso-callback?token=leak",
  ]) {
    assert.equal(safeReturnPath(unsafe), "/dashboard");
  }
});
