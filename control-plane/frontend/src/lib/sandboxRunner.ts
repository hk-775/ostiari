export type SandboxExecutionStatus = "completed" | "error" | "cancelled" | "timed_out";
export type SandboxOutputStream = "stdout" | "stderr" | "result" | "system";

export interface SandboxToolResult {
  status: number;
  ok: boolean;
  body: unknown;
}

export interface SandboxExecutionResult {
  status: SandboxExecutionStatus;
  durationMs: number;
  outputBytes: number;
  toolCalls: number;
  error: string;
}

export interface SandboxExecution {
  result: Promise<SandboxExecutionResult>;
  cancel: () => void;
}

interface SandboxRunnerOptions {
  code: string;
  timeoutMs: number;
  maxToolCalls: number;
  maxOutputBytes: number;
  maxToolPayloadBytes: number;
  executeTool: (
    name: string,
    params: Record<string, unknown>,
    signal: AbortSignal,
  ) => Promise<SandboxToolResult>;
  onOutput: (stream: SandboxOutputStream, text: string) => void;
}

interface FrameMessage {
  type?: string;
  capability?: string;
  stream?: SandboxOutputStream;
  text?: string;
  requestId?: string;
  name?: string;
  params?: unknown;
  status?: SandboxExecutionStatus;
  durationMs?: number;
  outputBytes?: number;
  toolCalls?: number;
  error?: string;
}

const WORKER_SOURCE = String.raw`
(() => {
  "use strict";
  const sendNative = self.postMessage.bind(self);
  const listenNative = self.addEventListener.bind(self);
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const encoder = new TextEncoder();
  const pendingTools = new Map();
  let maxOutputBytes = 0;
  let maxToolCalls = 0;
  let maxToolPayloadBytes = 0;
  let outputBytes = 0;
  let toolCalls = 0;
  let truncated = false;
  let running = false;

  const send = (message) => sendNative(message);
  const byteLength = (value) => encoder.encode(value).byteLength;

  const formatValue = (value) => {
    if (typeof value === "string") return value;
    if (value instanceof Error) return value.stack || value.message;
    try {
      const seen = new WeakSet();
      const rendered = JSON.stringify(value, (_key, item) => {
        if (typeof item === "bigint") return item.toString() + "n";
        if (typeof item === "object" && item !== null) {
          if (seen.has(item)) return "[Circular]";
          seen.add(item);
        }
        return item;
      }, 2);
      return rendered === undefined ? String(value) : rendered;
    } catch {
      return String(value);
    }
  };

  const write = (stream, values) => {
    if (truncated) return;
    let text = values.map(formatValue).join(" ");
    if (!text.endsWith("\n")) text += "\n";
    const remaining = maxOutputBytes - outputBytes;
    if (remaining <= 0) {
      truncated = true;
      return;
    }
    if (byteLength(text) > remaining) {
      while (text && byteLength(text) > remaining) text = text.slice(0, -1);
      truncated = true;
    }
    if (text) {
      outputBytes += byteLength(text);
      send({ type: "output", stream, text });
    }
    if (truncated) {
      const notice = "\n[output truncated]\n";
      if (outputBytes + byteLength(notice) <= maxOutputBytes) {
        outputBytes += byteLength(notice);
        send({ type: "output", stream: "system", text: notice });
      }
    }
  };

  const safeConsole = Object.freeze({
    log: (...values) => write("stdout", values),
    info: (...values) => write("stdout", values),
    debug: (...values) => write("stdout", values),
    warn: (...values) => write("stderr", values),
    error: (...values) => write("stderr", values),
  });

  const ostiari = Object.freeze({
    tool: async (name, params = {}) => {
      if (typeof name !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(name)) {
        throw new Error("Invalid tool name");
      }
      if (!params || typeof params !== "object" || Array.isArray(params)) {
        throw new Error("Tool parameters must be an object");
      }
      if (toolCalls >= maxToolCalls) {
        throw new Error("Sandbox tool-call limit reached");
      }
      let serialized;
      try {
        serialized = JSON.stringify(params);
      } catch {
        throw new Error("Tool parameters must be JSON serializable");
      }
      if (byteLength(serialized) > maxToolPayloadBytes) {
        throw new Error("Tool payload exceeds the run limit");
      }
      toolCalls += 1;
      const requestId = String(toolCalls) + "-" + String(Math.round(performance.now() * 1000));
      return await new Promise((resolve, reject) => {
        pendingTools.set(requestId, { resolve, reject });
        send({ type: "tool", requestId, name, params });
      });
    },
  });

  for (const name of [
    "fetch", "XMLHttpRequest", "WebSocket", "EventSource", "Worker",
    "SharedWorker", "BroadcastChannel", "indexedDB", "caches", "importScripts",
    "postMessage", "close", "RTCPeerConnection", "WebTransport",
    "WebSocketStream",
  ]) {
    try {
      Object.defineProperty(self, name, {
        value: undefined,
        writable: false,
        configurable: false,
      });
    } catch {
      try { self[name] = undefined; } catch {}
    }
  }

  listenNative("message", async (event) => {
    const message = event.data || {};
    if (message.type === "start" && !running) {
      running = true;
      maxOutputBytes = message.maxOutputBytes;
      maxToolCalls = message.maxToolCalls;
      maxToolPayloadBytes = message.maxToolPayloadBytes;
      const started = performance.now();
      try {
        const execute = new AsyncFunction(
          "console",
          "ostiari",
          "fetch",
          "XMLHttpRequest",
          "WebSocket",
          "EventSource",
          "Worker",
          "SharedWorker",
          "BroadcastChannel",
          "indexedDB",
          "caches",
          '"use strict";\n' + message.code + '\n//# sourceURL=ostiari-sandbox.js'
        );
        const result = await execute(
          safeConsole,
          ostiari,
          undefined,
          undefined,
          undefined,
          undefined,
          undefined,
          undefined,
          undefined,
          undefined,
          undefined,
        );
        if (result !== undefined) write("result", [result]);
        send({
          type: "complete",
          status: "completed",
          durationMs: Math.round(performance.now() - started),
          outputBytes,
          toolCalls,
          error: "",
        });
      } catch (error) {
        const detail = error instanceof Error ? (error.stack || error.message) : String(error);
        write("stderr", [detail]);
        send({
          type: "complete",
          status: "error",
          durationMs: Math.round(performance.now() - started),
          outputBytes,
          toolCalls,
          error: detail.slice(0, 512),
        });
      }
      return;
    }

    if (message.type !== "tool-result") return;
    const pending = pendingTools.get(message.requestId);
    if (!pending) return;
    pendingTools.delete(message.requestId);
    if (message.error) pending.reject(new Error(message.error));
    else pending.resolve(message.result);
  });
})();
`;

const RUNNER_HTML = `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' blob:; connect-src 'none'; img-src 'none'; media-src 'none'; font-src 'none'; style-src 'none'; object-src 'none'; frame-src 'none'; child-src 'none'; worker-src blob:; base-uri 'none'; form-action 'none'">
</head>
<body>
<script>
(() => {
  "use strict";
  const workerSource = ${JSON.stringify(WORKER_SOURCE)};
  const sendToParent = window.parent.postMessage.bind(window.parent);
  const encoder = new TextEncoder();
  let capability = "";
  let worker = null;
  let workerUrl = "";
  let maxOutputBytes = 0;
  let maxToolCalls = 0;
  let maxToolPayloadBytes = 0;
  let outputBytes = 0;
  let toolCalls = 0;
  let running = false;

  const byteLength = (value) => encoder.encode(value).byteLength;
  const send = (message) => sendToParent({ ...message, capability }, "*");

  const forwardWorkerMessage = (event) => {
    const message = event.data || {};
    if (
      message.type === "output"
      && typeof message.text === "string"
      && ["stdout", "stderr", "result", "system"].includes(message.stream)
    ) {
      if (outputBytes >= maxOutputBytes) return;
      let text = message.text;
      const remaining = maxOutputBytes - outputBytes;
      while (text && byteLength(text) > remaining) text = text.slice(0, -1);
      if (!text) return;
      outputBytes += byteLength(text);
      send({ type: "output", stream: message.stream, text });
      return;
    }
    if (
      message.type === "tool"
      && typeof message.requestId === "string"
      && typeof message.name === "string"
      && message.params
      && typeof message.params === "object"
      && !Array.isArray(message.params)
    ) {
      let serialized = "";
      try { serialized = JSON.stringify(message.params); } catch {}
      if (
        toolCalls >= maxToolCalls
        || byteLength(serialized) > maxToolPayloadBytes
        || !/^[A-Za-z0-9_.:-]{1,128}$/.test(message.name)
      ) {
        worker.postMessage({
          type: "tool-result",
          requestId: message.requestId,
          error: "Sandbox tool request rejected",
        });
        return;
      }
      toolCalls += 1;
      send({
        type: "tool",
        requestId: message.requestId,
        name: message.name,
        params: message.params,
      });
      return;
    }
    if (
      message.type === "complete"
      && ["completed", "error"].includes(message.status)
    ) {
      send({
        type: "complete",
        status: message.status,
        durationMs: message.durationMs,
        outputBytes,
        toolCalls,
        error: typeof message.error === "string" ? message.error.slice(0, 512) : "",
      });
      worker.terminate();
      URL.revokeObjectURL(workerUrl);
    }
  };

  window.addEventListener("message", (event) => {
    const message = event.data || {};
    if (message.type === "start" && !running) {
      running = true;
      capability = message.capability;
      maxOutputBytes = message.maxOutputBytes;
      maxToolCalls = message.maxToolCalls;
      maxToolPayloadBytes = message.maxToolPayloadBytes;
      workerUrl = URL.createObjectURL(new Blob([workerSource], { type: "text/javascript" }));
      worker = new Worker(workerUrl);
      worker.addEventListener("message", forwardWorkerMessage);
      worker.addEventListener("error", (error) => {
        send({
          type: "complete",
          status: "error",
          durationMs: 0,
          outputBytes,
          toolCalls,
          error: String(error.message || "Sandbox worker failed").slice(0, 512),
        });
      });
      worker.postMessage({
        type: "start",
        code: message.code,
        maxOutputBytes,
        maxToolCalls,
        maxToolPayloadBytes,
      });
      return;
    }

    if (
      message.type === "tool-result"
      && message.capability === capability
      && worker
    ) {
      worker.postMessage({
        type: "tool-result",
        requestId: message.requestId,
        result: message.result,
        error: message.error,
      });
    }
  });

  sendToParent({ type: "sandbox-ready" }, "*");
})();
</script>
</body>
</html>`;

function boundedText(text: string, remainingBytes: number): string {
  const encoder = new TextEncoder();
  if (encoder.encode(text).byteLength <= remainingBytes) return text;
  let low = 0;
  let high = text.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (encoder.encode(text.slice(0, middle)).byteLength <= remainingBytes) low = middle;
    else high = middle - 1;
  }
  return text.slice(0, low);
}

export function startSandboxExecution(options: SandboxRunnerOptions): SandboxExecution {
  const iframe = document.createElement("iframe");
  iframe.hidden = true;
  iframe.sandbox.add("allow-scripts");
  iframe.referrerPolicy = "no-referrer";
  iframe.setAttribute("aria-hidden", "true");
  iframe.srcdoc = RUNNER_HTML;

  const capability = crypto.randomUUID();
  const abortController = new AbortController();
  const encoder = new TextEncoder();
  const startedAt = performance.now();
  let outputBytes = 0;
  let toolCalls = 0;
  let settled = false;
  let readyTimeoutId = 0;
  let timeoutId = 0;
  let resolveResult: (result: SandboxExecutionResult) => void;

  const result = new Promise<SandboxExecutionResult>((resolve) => {
    resolveResult = resolve;
  });

  const cleanup = () => {
    window.removeEventListener("message", onMessage);
    window.clearTimeout(timeoutId);
    window.clearTimeout(readyTimeoutId);
    abortController.abort();
    iframe.remove();
  };

  const finish = (finalResult: SandboxExecutionResult) => {
    if (settled) return;
    settled = true;
    cleanup();
    resolveResult(finalResult);
  };

  const emitOutput = (stream: SandboxOutputStream, rawText: string) => {
    if (settled || outputBytes >= options.maxOutputBytes) return;
    const remaining = options.maxOutputBytes - outputBytes;
    const text = boundedText(rawText, remaining);
    if (!text) return;
    outputBytes += encoder.encode(text).byteLength;
    options.onOutput(stream, text);
  };

  const postToolResult = (requestId: string, payload: Record<string, unknown>) => {
    if (settled || !iframe.contentWindow) return;
    iframe.contentWindow.postMessage(
      { type: "tool-result", capability, requestId, ...payload },
      "*",
    );
  };

  const onMessage = (event: MessageEvent<FrameMessage>) => {
    if (event.source !== iframe.contentWindow || settled) return;
    const message = event.data;
    if (!message || typeof message !== "object") return;

    if (message.type === "sandbox-ready") {
      window.clearTimeout(readyTimeoutId);
      iframe.contentWindow?.postMessage(
        {
          type: "start",
          capability,
          code: options.code,
          maxOutputBytes: options.maxOutputBytes,
          maxToolCalls: options.maxToolCalls,
          maxToolPayloadBytes: options.maxToolPayloadBytes,
        },
        "*",
      );
      return;
    }

    if (message.capability !== capability) return;
    if (
      message.type === "output"
      && typeof message.text === "string"
      && ["stdout", "stderr", "result", "system"].includes(message.stream || "")
    ) {
      emitOutput(message.stream as SandboxOutputStream, message.text);
      return;
    }

    if (
      message.type === "tool"
      && typeof message.requestId === "string"
      && typeof message.name === "string"
      && message.params
      && typeof message.params === "object"
      && !Array.isArray(message.params)
    ) {
      toolCalls += 1;
      if (toolCalls > options.maxToolCalls) {
        postToolResult(message.requestId, { error: "Sandbox tool-call limit reached" });
        return;
      }
      void options.executeTool(
        message.name,
        message.params as Record<string, unknown>,
        abortController.signal,
      ).then(
        (toolResult) => postToolResult(message.requestId!, { result: toolResult }),
        (error: unknown) => postToolResult(message.requestId!, {
          error: error instanceof Error ? error.message : String(error),
        }),
      );
      return;
    }

    if (
      message.type === "complete"
      && ["completed", "error"].includes(message.status || "")
    ) {
      finish({
        status: message.status as SandboxExecutionStatus,
        durationMs: Math.max(0, Math.round(message.durationMs || 0)),
        outputBytes,
        toolCalls: Math.min(toolCalls, options.maxToolCalls),
        error: (message.error || "").slice(0, 512),
      });
    }
  };

  window.addEventListener("message", onMessage);
  readyTimeoutId = window.setTimeout(() => {
    emitOutput("system", "Sandbox runtime failed to initialize.\n");
    finish({
      status: "error",
      durationMs: Math.round(performance.now() - startedAt),
      outputBytes,
      toolCalls,
      error: "Sandbox runtime failed to initialize",
    });
  }, 2_000);

  timeoutId = window.setTimeout(() => {
    emitOutput("system", `Execution stopped after ${options.timeoutMs} ms.\n`);
    finish({
      status: "timed_out",
      durationMs: Math.round(performance.now() - startedAt),
      outputBytes,
      toolCalls,
      error: "Execution timed out",
    });
  }, options.timeoutMs);
  document.body.appendChild(iframe);

  return {
    result,
    cancel: () => {
      emitOutput("system", "Execution cancelled.\n");
      finish({
        status: "cancelled",
        durationMs: Math.round(performance.now() - startedAt),
        outputBytes,
        toolCalls,
        error: "Cancelled by operator",
      });
    },
  };
}

export async function sha256Source(source: string): Promise<{ digest: string; bytes: number }> {
  const encoded = new TextEncoder().encode(source);
  const digest = await crypto.subtle.digest("SHA-256", encoded);
  return {
    digest: Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join(""),
    bytes: encoded.byteLength,
  };
}
