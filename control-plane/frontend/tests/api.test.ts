import assert from "node:assert/strict";
import test from "node:test";
import { installBrowserEnvironment } from "./browserEnv";

const browser = installBrowserEnvironment();
const apiModule = await import("../src/lib/api");
const { APIError, apiFetch, fetchAPI, requireOk } = apiModule;

test.beforeEach(() => {
  browser.localStorage.clear();
  browser.assignedLocations.length = 0;
  window.location.pathname = "/dashboard";
});

test("apiFetch adds JSON and bearer headers without dropping caller headers", async () => {
  browser.localStorage.setItem("ostiari_token", "signed-token");
  let captured: RequestInit | undefined;
  globalThis.fetch = async (_input, init) => {
    captured = init;
    return new Response("{}", { status: 200 });
  };

  await apiFetch("/api/policies", {
    method: "POST",
    body: JSON.stringify({ name: "deny-dangerous-tools" }),
    headers: { "X-Request-ID": "request-1" },
  });

  const headers = new Headers(captured?.headers);
  assert.equal(headers.get("Authorization"), "Bearer signed-token");
  assert.equal(headers.get("Content-Type"), "application/json");
  assert.equal(headers.get("X-Request-ID"), "request-1");
});

test("apiFetch clears an expired session and redirects to sign in", async () => {
  browser.localStorage.setItem("ostiari_token", "expired");
  globalThis.fetch = async () => new Response("", { status: 401 });

  await assert.rejects(
    () => apiFetch("/api/users"),
    (error: unknown) => error instanceof APIError
      && error.status === 401
      && error.message === "Session expired",
  );
  assert.equal(browser.localStorage.getItem("ostiari_token"), null);
  assert.deepEqual(browser.assignedLocations, ["/login"]);
});

test("apiFetch does not redirect recursively while already on the login page", async () => {
  window.location.pathname = "/login";
  globalThis.fetch = async () => new Response("", { status: 401 });

  await assert.rejects(() => apiFetch("/api/users"), APIError);
  assert.deepEqual(browser.assignedLocations, []);
});

test("requireOk preserves structured and plain-text API errors", async () => {
  await assert.rejects(
    () => requireOk(new Response(
      JSON.stringify({ detail: "Policy version is stale" }),
      { status: 409, statusText: "Conflict" },
    )),
    (error: unknown) => error instanceof APIError
      && error.status === 409
      && error.message === "Policy version is stale",
  );
  await assert.rejects(
    () => requireOk(new Response("gateway unavailable", { status: 503 })),
    (error: unknown) => error instanceof APIError
      && error.status === 503
      && error.message === "gateway unavailable",
  );
});

test("fetchAPI decodes JSON and accepts empty successful responses", async () => {
  globalThis.fetch = async () => new Response(
    JSON.stringify({ status: "healthy" }),
    { status: 200 },
  );
  assert.deepEqual(await fetchAPI("/api/ready"), { status: "healthy" });

  globalThis.fetch = async () => new Response(null, { status: 204 });
  assert.equal(await fetchAPI("/api/gateways/gateway-1", { method: "DELETE" }), undefined);
});
