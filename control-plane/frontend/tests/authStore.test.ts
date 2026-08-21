import assert from "node:assert/strict";
import test from "node:test";
import { installBrowserEnvironment } from "./browserEnv";

const browser = installBrowserEnvironment();
const { useAuthStore } = await import("../src/stores/authStore");

const admin = {
  id: "user-1",
  email: "admin@example.com",
  name: "Admin",
  role: "admin" as const,
};

function resetStore(): void {
  browser.localStorage.clear();
  useAuthStore.setState({
    token: null,
    user: null,
    isAuthenticated: false,
  });
}

test.beforeEach(resetStore);

test("login persists the access token and authenticated user", async () => {
  globalThis.fetch = async (_input, init) => {
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      email: "admin@example.com",
      password: "correct horse",
    });
    return new Response(JSON.stringify({
      access_token: "access-token",
      user: admin,
    }), { status: 200 });
  };

  await useAuthStore.getState().login("admin@example.com", "correct horse");

  assert.equal(browser.localStorage.getItem("ostiari_token"), "access-token");
  assert.deepEqual(useAuthStore.getState().user, admin);
  assert.equal(useAuthStore.getState().isAuthenticated, true);
});

test("failed login does not create a browser session", async () => {
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: "Invalid credentials" }),
    { status: 401 },
  );

  await assert.rejects(
    () => useAuthStore.getState().login("admin@example.com", "wrong"),
    /Invalid credentials/,
  );
  assert.equal(browser.localStorage.getItem("ostiari_token"), null);
  assert.equal(useAuthStore.getState().isAuthenticated, false);
});

test("completeSSO validates the callback token before persisting it", async () => {
  globalThis.fetch = async (_input, init) => {
    assert.equal(new Headers(init?.headers).get("Authorization"), "Bearer sso-token");
    return new Response(JSON.stringify(admin), { status: 200 });
  };

  await useAuthStore.getState().completeSSO("sso-token");

  assert.equal(browser.localStorage.getItem("ostiari_token"), "sso-token");
  assert.deepEqual(useAuthStore.getState().user, admin);
});

test("invalid SSO and fetchMe responses clear authentication state", async () => {
  browser.localStorage.setItem("ostiari_token", "old-token");
  useAuthStore.setState({
    token: "old-token",
    user: admin,
    isAuthenticated: true,
  });
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: "Token expired" }),
    { status: 401 },
  );

  await assert.rejects(
    () => useAuthStore.getState().completeSSO("invalid-token"),
    /Token expired/,
  );
  assert.equal(browser.localStorage.getItem("ostiari_token"), null);
  assert.equal(useAuthStore.getState().user, null);

  browser.localStorage.setItem("ostiari_token", "old-token");
  useAuthStore.setState({
    token: "old-token",
    user: admin,
    isAuthenticated: true,
  });
  await useAuthStore.getState().fetchMe();
  assert.equal(browser.localStorage.getItem("ostiari_token"), null);
  assert.equal(useAuthStore.getState().isAuthenticated, false);
});

test("logout and role checks reflect the active user", () => {
  browser.localStorage.setItem("ostiari_token", "token");
  useAuthStore.setState({ token: "token", user: admin, isAuthenticated: true });

  assert.equal(useAuthStore.getState().hasRole("admin"), true);
  assert.equal(useAuthStore.getState().hasRole("operator"), false);
  useAuthStore.getState().logout();

  assert.equal(browser.localStorage.getItem("ostiari_token"), null);
  assert.equal(useAuthStore.getState().isAuthenticated, false);
});
