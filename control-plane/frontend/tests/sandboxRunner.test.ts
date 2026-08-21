import assert from "node:assert/strict";
import test from "node:test";

interface MessageListener {
  (event: MessageEvent): void;
}

class FakeContentWindow {
  messages: unknown[] = [];

  postMessage(message: unknown): void {
    this.messages.push(message);
  }
}

class FakeIframe {
  hidden = false;
  referrerPolicy = "";
  srcdoc = "";
  removed = false;
  attributes = new Map<string, string>();
  contentWindow = new FakeContentWindow();
  sandbox = { add: (_value: string) => undefined };

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value);
  }

  remove(): void {
    this.removed = true;
  }
}

function installSandboxEnvironment() {
  const listeners = new Set<MessageListener>();
  const timers = new Map<number, () => void>();
  const iframe = new FakeIframe();
  let nextTimer = 1;

  Object.defineProperties(globalThis, {
    window: {
      value: {
        parent: { postMessage: () => undefined },
        addEventListener: (_name: string, listener: MessageListener) => listeners.add(listener),
        removeEventListener: (_name: string, listener: MessageListener) => listeners.delete(listener),
        setTimeout: (callback: () => void) => {
          const id = nextTimer++;
          timers.set(id, callback);
          return id;
        },
        clearTimeout: (id: number) => timers.delete(id),
      },
      configurable: true,
    },
    document: {
      value: {
        createElement: (name: string) => {
          assert.equal(name, "iframe");
          return iframe;
        },
        body: {
          appendChild: (element: unknown) => assert.equal(element, iframe),
        },
      },
      configurable: true,
    },
  });

  return {
    iframe,
    dispatch(data: unknown, source: unknown = iframe.contentWindow) {
      for (const listener of listeners) {
        listener({ data, source } as MessageEvent);
      }
    },
    runLatestTimer() {
      const [id, callback] = Array.from(timers.entries()).at(-1) ?? [];
      assert.ok(id);
      timers.delete(id);
      callback();
    },
    runFirstTimer() {
      const [id, callback] = Array.from(timers.entries())[0] ?? [];
      assert.ok(id);
      timers.delete(id);
      callback();
    },
  };
}

test("sandbox execution uses a capability token and returns bounded output", async () => {
  const environment = installSandboxEnvironment();
  const { startSandboxExecution } = await import("../src/lib/sandboxRunner");
  const output: string[] = [];
  const execution = startSandboxExecution({
    code: "return 42",
    timeoutMs: 5_000,
    maxToolCalls: 2,
    maxOutputBytes: 5,
    maxToolPayloadBytes: 128,
    executeTool: async () => ({ status: 200, ok: true, body: {} }),
    onOutput: (_stream, text) => output.push(text),
  });

  environment.dispatch({ type: "sandbox-ready" });
  const start = environment.iframe.contentWindow.messages[0] as {
    capability: string;
    type: string;
  };
  assert.equal(start.type, "start");
  assert.match(start.capability, /^[0-9a-f-]{36}$/i);

  environment.dispatch({
    type: "output",
    capability: "wrong-capability",
    stream: "stdout",
    text: "ignored",
  });
  environment.dispatch({
    type: "output",
    capability: start.capability,
    stream: "stdout",
    text: "123456789",
  });
  environment.dispatch({
    type: "complete",
    capability: start.capability,
    status: "completed",
    durationMs: 12,
    outputBytes: 9,
    toolCalls: 0,
    error: "",
  });

  assert.deepEqual(output, ["12345"]);
  assert.deepEqual(await execution.result, {
    status: "completed",
    durationMs: 12,
    outputBytes: 5,
    toolCalls: 0,
    error: "",
  });
  assert.equal(environment.iframe.removed, true);
});

test("sandbox tool requests are bridged and respect the call limit", async () => {
  const environment = installSandboxEnvironment();
  const { startSandboxExecution } = await import("../src/lib/sandboxRunner");
  const calls: string[] = [];
  startSandboxExecution({
    code: "",
    timeoutMs: 5_000,
    maxToolCalls: 1,
    maxOutputBytes: 100,
    maxToolPayloadBytes: 128,
    executeTool: async (name) => {
      calls.push(name);
      return { status: 200, ok: true, body: { approved: true } };
    },
    onOutput: () => undefined,
  });

  environment.dispatch({ type: "sandbox-ready" });
  const start = environment.iframe.contentWindow.messages[0] as { capability: string };
  environment.dispatch({
    type: "tool",
    capability: start.capability,
    requestId: "request-1",
    name: "payments.quote",
    params: { amount: 1 },
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  environment.dispatch({
    type: "tool",
    capability: start.capability,
    requestId: "request-2",
    name: "payments.charge",
    params: { amount: 1 },
  });

  assert.deepEqual(calls, ["payments.quote"]);
  const toolMessages = environment.iframe.contentWindow.messages.slice(1) as Array<{
    requestId: string;
    result?: unknown;
    error?: string;
  }>;
  assert.deepEqual(toolMessages[0], {
    type: "tool-result",
    capability: start.capability,
    requestId: "request-1",
    result: { status: 200, ok: true, body: { approved: true } },
  });
  assert.match(toolMessages[1].error ?? "", /limit reached/);
});

test("sandbox cancellation and timeouts clean up the iframe", async () => {
  const environment = installSandboxEnvironment();
  const { startSandboxExecution } = await import("../src/lib/sandboxRunner");
  const cancelled = startSandboxExecution({
    code: "",
    timeoutMs: 5_000,
    maxToolCalls: 1,
    maxOutputBytes: 100,
    maxToolPayloadBytes: 128,
    executeTool: async () => ({ status: 200, ok: true, body: {} }),
    onOutput: () => undefined,
  });
  cancelled.cancel();
  assert.equal((await cancelled.result).status, "cancelled");
  assert.equal(environment.iframe.removed, true);

  const timeoutEnvironment = installSandboxEnvironment();
  const timedOut = startSandboxExecution({
    code: "",
    timeoutMs: 5_000,
    maxToolCalls: 1,
    maxOutputBytes: 100,
    maxToolPayloadBytes: 128,
    executeTool: async () => ({ status: 200, ok: true, body: {} }),
    onOutput: () => undefined,
  });
  timeoutEnvironment.runLatestTimer();
  assert.equal((await timedOut.result).status, "timed_out");

  const initializationEnvironment = installSandboxEnvironment();
  const initializationTimedOut = startSandboxExecution({
    code: "",
    timeoutMs: 5_000,
    maxToolCalls: 1,
    maxOutputBytes: 100,
    maxToolPayloadBytes: 128,
    executeTool: async () => ({ status: 200, ok: true, body: {} }),
    onOutput: () => undefined,
  });
  initializationEnvironment.runFirstTimer();
  const initializationResult = await initializationTimedOut.result;
  assert.equal(initializationResult.status, "error");
  assert.match(initializationResult.error, /failed to initialize/i);
});

test("sha256Source reports UTF-8 bytes and a stable digest", async () => {
  const { sha256Source } = await import("../src/lib/sandboxRunner");
  assert.deepEqual(await sha256Source("hello"), {
    digest: "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    bytes: 5,
  });
  assert.equal((await sha256Source("é")).bytes, 2);
});
