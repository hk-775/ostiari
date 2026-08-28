import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const distRoot = resolve(process.argv[2] || join(frontendRoot, "dist"));
const publicBase = "/ostiari/";
const screenshotDir = process.env.OSTIARI_E2E_SCREENSHOT_DIR;

if (typeof WebSocket !== "function") {
  throw new Error("The public-site browser test requires Node.js 22 or newer.");
}
if (!existsSync(join(distRoot, "index.html"))) {
  throw new Error(`Public-site build not found at ${distRoot}. Build it before testing.`);
}

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".mp3": "audio/mpeg",
  ".svg": "image/svg+xml",
};

function findChrome() {
  const candidates = [
    process.env.CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }

  for (const command of ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]) {
    const found = spawnSync("which", [command], { encoding: "utf8" });
    if (found.status === 0 && found.stdout.trim()) return found.stdout.trim();
  }

  throw new Error("Chrome or Chromium is required for the public-site browser test.");
}

async function startStaticServer() {
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url || "/", "http://127.0.0.1");
      let pathname = decodeURIComponent(url.pathname);
      if (pathname === publicBase.slice(0, -1) || pathname === publicBase) {
        pathname = `${publicBase}index.html`;
      }
      if (!pathname.startsWith(publicBase)) {
        response.writeHead(404).end("Not found");
        return;
      }

      const relativePath = pathname.slice(publicBase.length);
      const filePath = resolve(distRoot, relativePath);
      if (filePath !== distRoot && !filePath.startsWith(`${distRoot}${sep}`)) {
        response.writeHead(403).end("Forbidden");
        return;
      }

      const body = await readFile(filePath);
      response.writeHead(200, {
        "cache-control": "no-store",
        "content-type": contentTypes[extname(filePath)] || "application/octet-stream",
      });
      if (request.method === "HEAD") response.end();
      else response.end(body);
    } catch {
      response.writeHead(404).end("Not found");
    }
  });

  await new Promise((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  return { server, origin: `http://127.0.0.1:${port}` };
}

function requestJson(url, method = "GET") {
  return new Promise((resolveRequest, reject) => {
    const request = httpRequest(url, { method }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () => {
        if (!response.statusCode || response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error(`HTTP ${response.statusCode || "unknown"}: ${body}`));
          return;
        }
        try {
          resolveRequest(JSON.parse(body));
        } catch (error) {
          reject(new Error(`Invalid JSON from ${url}: ${error}`));
        }
      });
    });
    request.setTimeout(2_000, () => {
      request.destroy(new Error(`Timed out requesting ${url}`));
    });
    request.once("error", reject);
    request.end();
  });
}

async function pollJson(url, chrome) {
  let lastError;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (chrome.exitCode !== null) {
      throw new Error(`Chrome exited before DevTools became available (code ${chrome.exitCode}).`);
    }
    try {
      return await requestJson(url);
    } catch (error) {
      lastError = error;
    }
    await delay(100);
  }
  throw new Error(`Timed out waiting for Chrome DevTools: ${lastError}`);
}

async function waitForDevToolsUrl(chrome, getOutput) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (chrome.exitCode !== null) {
      throw new Error(
        `Chrome exited before DevTools became available (code ${chrome.exitCode}).`,
      );
    }
    const match = getOutput().match(/DevTools listening on (ws:\/\/\S+)/);
    if (match) return match[1];
    await delay(100);
  }
  throw new Error("Timed out waiting for Chrome to announce its DevTools endpoint.");
}

class CdpSession {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();

    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result || {});
        return;
      }
      const listeners = this.listeners.get(message.method);
      if (!listeners) return;
      for (const listener of [...listeners]) listener(message.params || {});
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolveOpen, reject) => {
      socket.addEventListener("open", resolveOpen, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    return new CdpSession(socket);
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolveResult, reject) => {
      this.pending.set(id, { resolve: resolveResult, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, listener) {
    const listeners = this.listeners.get(method) || new Set();
    listeners.add(listener);
    this.listeners.set(method, listeners);
    return () => listeners.delete(listener);
  }

  once(method, timeoutMs = 10_000) {
    return new Promise((resolveEvent, reject) => {
      const timer = setTimeout(() => {
        unsubscribe();
        reject(new Error(`Timed out waiting for Chrome event ${method}`));
      }, timeoutMs);
      const unsubscribe = this.on(method, (params) => {
        clearTimeout(timer);
        unsubscribe();
        resolveEvent(params);
      });
    });
  }

  close() {
    this.socket.close();
  }
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    const description = result.exceptionDetails.exception?.description
      || result.exceptionDetails.text
      || "Browser evaluation failed";
    throw new Error(description);
  }
  return result.result?.value;
}

async function waitFor(cdp, expression, description, timeoutMs = 8_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const value = await evaluate(cdp, expression);
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await delay(40);
  }
  throw new Error(`Timed out waiting for ${description}${lastError ? `: ${lastError}` : ""}`);
}

async function click(cdp, selector) {
  const serialized = JSON.stringify(selector);
  await evaluate(cdp, `(() => {
    const element = document.querySelector(${serialized});
    if (!element) return false;
    element.scrollIntoView({ block: "center", inline: "center" });
    return true;
  })()`);
  await delay(25);
  const rect = await evaluate(cdp, `(() => {
    const element = document.querySelector(${serialized});
    if (!element) return null;
    const bounds = element.getBoundingClientRect();
    return {
      x: bounds.left + bounds.width / 2,
      y: bounds.top + bounds.height / 2,
      disabled: Boolean(element.disabled),
    };
  })()`);
  assert.ok(rect, `Missing clickable element ${selector}`);
  assert.equal(rect.disabled, false, `Element is disabled: ${selector}`);
  await cdp.send("Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: rect.x,
    y: rect.y,
    button: "left",
    clickCount: 1,
  });
  await cdp.send("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: rect.x,
    y: rect.y,
    button: "left",
    clickCount: 1,
  });
  await delay(30);
}

async function captureScreenshot(cdp, name) {
  if (!screenshotDir) return;
  const result = await cdp.send("Page.captureScreenshot", {
    captureBeyondViewport: false,
    format: "png",
    fromSurface: true,
  });
  await writeFile(join(screenshotDir, name), Buffer.from(result.data, "base64"));
}

const speechHarness = `
(() => {
  const spoken = [];
  const audioEvents = [];
  class TestSpeechSynthesisUtterance {
    constructor(text) {
      this.text = String(text);
      this.lang = "";
      this.pitch = 1;
      this.rate = 1;
      this.volume = 1;
      this.voice = null;
      this.onend = null;
      this.onerror = null;
    }
  }
  const synthesis = {
    paused: false,
    pending: false,
    speaking: false,
    cancel() {
      this.speaking = false;
    },
    getVoices() {
      return [{ lang: "en-US", localService: true, name: "Ostiari test voice" }];
    },
    pause() {
      this.paused = true;
    },
    resume() {
      this.paused = false;
    },
    speak(utterance) {
      this.speaking = true;
      spoken.push(utterance.text);
      window.setTimeout(() => {
        this.speaking = false;
        if (typeof utterance.onend === "function") utterance.onend({ type: "end" });
      }, 70);
    },
  };
  Object.defineProperty(window, "SpeechSynthesisUtterance", {
    configurable: true,
    value: TestSpeechSynthesisUtterance,
  });
  Object.defineProperty(window, "speechSynthesis", {
    configurable: true,
    value: synthesis,
  });
  Object.defineProperty(window, "__ostiariSpoken", {
    configurable: true,
    value: spoken,
  });
  Object.defineProperty(window, "__ostiariAudioEvents", {
    configurable: true,
    value: audioEvents,
  });
  document.addEventListener("ostiari:architecture-audio", (event) => {
    audioEvents.push(event.detail);
  });
})();
`;

const { server, origin } = await startStaticServer();
const profileDir = await mkdtemp(join(tmpdir(), "ostiari-pages-chrome-"));
const chromePath = findChrome();
let chromeOutput = "";
const chromeArgs = [
  "--headless",
  "--disable-background-networking",
  "--disable-component-update",
  "--disable-default-apps",
  "--disable-dev-shm-usage",
  "--disable-extensions",
  "--disable-gpu",
  "--disable-sync",
  "--metrics-recording-only",
  "--mute-audio",
  "--no-default-browser-check",
  "--no-first-run",
  "--remote-debugging-address=127.0.0.1",
  "--remote-debugging-port=0",
  `--user-data-dir=${profileDir}`,
  "--window-size=1440,1000",
  "about:blank",
];
if (process.platform === "linux") chromeArgs.unshift("--no-sandbox");

const chrome = spawn(chromePath, chromeArgs, {
  stdio: ["ignore", "pipe", "pipe"],
});
for (const stream of [chrome.stdout, chrome.stderr]) {
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    chromeOutput = `${chromeOutput}${chunk}`.slice(-12_000);
  });
}

let cdp;
const browserExceptions = [];
const requestedUrls = [];

try {
  const browserWebSocketUrl = await waitForDevToolsUrl(chrome, () => chromeOutput);
  const devToolsOrigin = `http://${new URL(browserWebSocketUrl).host}`;
  await pollJson(`${devToolsOrigin}/json/version`, chrome);
  const target = await requestJson(
    `${devToolsOrigin}/json/new?${encodeURIComponent("about:blank")}`,
    "PUT",
  );
  cdp = await CdpSession.connect(target.webSocketDebuggerUrl);

  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await cdp.send("Network.setBlockedURLs", {
    urls: ["https://fonts.googleapis.com/*", "https://fonts.gstatic.com/*"],
  });
  await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: speechHarness });
  cdp.on("Runtime.exceptionThrown", ({ exceptionDetails }) => {
    browserExceptions.push(
      exceptionDetails?.exception?.description || exceptionDetails?.text || "Unknown exception",
    );
  });
  cdp.on("Network.requestWillBeSent", ({ request }) => {
    if (request?.url) requestedUrls.push(request.url);
  });

  const loaded = cdp.once("Page.loadEventFired");
  await cdp.send("Page.navigate", { url: `${origin}${publicBase}` });
  await loaded;

  await waitFor(
    cdp,
    `Boolean(document.querySelector('[data-testid="canonical-landing"]'))`,
    "the canonical React landing page",
  );
  assert.equal(
    await evaluate(cdp, "document.title"),
    "Ostiari — Runtime Governance for AI Agents",
  );
  const landing = await evaluate(cdp, `(() => {
    const logo = document.querySelector('img[alt="Ostiari"]');
    return {
      copy: document.body.textContent,
      logoComplete: Boolean(logo?.complete && logo?.naturalWidth > 0),
      logoUrl: logo?.src || "",
    };
  })()`);
  assert.match(landing.copy, /AI agents are autonomous/);
  assert.match(landing.copy, /Open Control Plane/);
  assert.match(landing.copy, /Watch Architecture Demo/);
  assert.match(landing.copy, /Fleet status \(connecting…\)/);
  assert.match(landing.copy, /Enter Control Plane/);
  assert.doesNotMatch(landing.copy, /Run the demo|Run Ostiari|available in the running demo/);
  assert.equal(landing.logoComplete, true);
  assert.match(landing.logoUrl, /\/ostiari\/logo\.svg$/);
  assert.equal(
    requestedUrls.some((url) => url.includes("localhost:8400/api/")),
    false,
    "The public landing page attempted to contact the private control-plane API.",
  );
  await captureScreenshot(cdp, "ostiari-canonical-landing.png");

  await click(cdp, '[data-testid="architecture-demo-link"]');
  await waitFor(
    cdp,
    `Boolean(document.querySelector('[data-testid="architecture-runtime"]'))`,
    "the public architecture experience",
  );
  assert.equal(await evaluate(cdp, "window.location.hash"), "#/architecture");
  assert.equal(
    await evaluate(cdp, "document.querySelectorAll('[data-scenario-id]').length"),
    5,
  );
  assert.equal(
    await evaluate(cdp, "document.querySelector('button[aria-pressed]')?.getAttribute('aria-pressed')"),
    "true",
  );

  const runtimeStepCount = await evaluate(
    cdp,
    "document.querySelectorAll('[data-testid=\"architecture-runtime\"] [data-step-index]').length",
  );
  await click(cdp, 'button[aria-label="Play flow"]');
  await waitFor(
    cdp,
    `document.querySelector('[data-testid="architecture-runtime"]')?.dataset.playing === "true"`,
    "runtime playback to start",
  );
  const animationName = await evaluate(
    cdp,
    "getComputedStyle(document.querySelector('[aria-current=\"step\"]')).animationName",
  );
  assert.match(animationName, /architecture-flow-pulse/);
  await waitFor(cdp, "window.__ostiariSpoken.length > 0", "spoken narration");
  await waitFor(cdp, "window.__ostiariAudioEvents.length > 0", "audio event delivery");
  await waitFor(
    cdp,
    `(() => {
      const runtime = document.querySelector('[data-testid="architecture-runtime"]');
      const active = document.querySelector('[aria-current="step"]');
      return runtime?.dataset.playing === "false"
        && Number(active?.dataset.stepIndex) === ${runtimeStepCount - 1};
    })()`,
    "the complete narrated runtime flow",
  );
  assert.equal(
    await evaluate(cdp, "window.__ostiariSpoken.length"),
    runtimeStepCount,
    "Every runtime step should be narrated once.",
  );
  await captureScreenshot(cdp, "ostiari-canonical-architecture.png");

  await click(cdp, 'button[aria-pressed="true"]');
  assert.equal(
    await evaluate(cdp, "document.querySelector('button[aria-pressed]')?.getAttribute('aria-pressed')"),
    "false",
  );
  const spokenBeforeMutedPlayback = await evaluate(cdp, "window.__ostiariSpoken.length");
  await click(cdp, 'button[aria-label="Reset flow"]');
  await click(cdp, 'button[aria-label="Play flow"]');
  await delay(200);
  assert.equal(
    await evaluate(cdp, "window.__ostiariSpoken.length"),
    spokenBeforeMutedPlayback,
    "Muted playback unexpectedly produced narration.",
  );
  await click(cdp, 'button[aria-label="Pause flow"]');
  await click(cdp, 'button[aria-pressed="false"]');

  for (const scenarioId of ["direct-tool", "hitl", "stdio-mcp", "a2a", "llm-intent"]) {
    await click(cdp, `[data-scenario-id="${scenarioId}"]`);
    assert.equal(
      await evaluate(
        cdp,
        `document.querySelector('[data-scenario-id="${scenarioId}"]')?.getAttribute('aria-selected')`,
      ),
      "true",
    );
    assert.equal(
      await evaluate(cdp, "document.querySelector('[aria-current=\"step\"]')?.dataset.stepIndex"),
      "0",
    );
  }

  await click(cdp, '[data-testid="architecture-view-deployment"]');
  await waitFor(
    cdp,
    `Boolean(document.querySelector('[data-testid="architecture-deployment"]'))`,
    "the deployment topology view",
  );
  assert.equal(
    await evaluate(cdp, "document.querySelectorAll('[data-deployment-id]').length"),
    5,
  );
  for (const deploymentId of ["local", "source-demo", "aws", "agentcore", "production"]) {
    await click(cdp, `[data-deployment-id="${deploymentId}"]`);
    assert.equal(
      await evaluate(
        cdp,
        `document.querySelector('[data-deployment-id="${deploymentId}"]')?.getAttribute('aria-selected')`,
      ),
      "true",
    );
    assert.equal(
      await evaluate(cdp, "document.querySelectorAll('.architecture-topology-enter').length"),
      4,
    );
  }
  const topologyAnimation = await evaluate(
    cdp,
    "getComputedStyle(document.querySelector('.architecture-topology-enter')).animationName",
  );
  assert.match(topologyAnimation, /architecture-topology-enter/);

  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  await delay(100);
  assert.equal(
    await evaluate(
      cdp,
      "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1",
    ),
    true,
    "The public architecture page overflows the mobile viewport.",
  );

  assert.deepEqual(browserExceptions, []);
  console.log(
    `public site e2e OK: canonical landing, ${runtimeStepCount} narrated runtime steps, `
      + "5 scenarios, animations, and 5 deployment views",
  );
} catch (error) {
  if (chromeOutput) {
    console.error("Chrome output (tail):\n", chromeOutput);
  }
  throw error;
} finally {
  cdp?.close();
  await new Promise((resolveClose) => server.close(resolveClose));
  if (chrome.exitCode === null) chrome.kill("SIGTERM");
  await Promise.race([
    new Promise((resolveExit) => chrome.once("exit", resolveExit)),
    delay(2_000),
  ]);
  if (chrome.exitCode === null) {
    chrome.kill("SIGKILL");
    await Promise.race([
      new Promise((resolveExit) => chrome.once("exit", resolveExit)),
      delay(2_000),
    ]);
  }
  await rm(profileDir, {
    recursive: true,
    force: true,
    maxRetries: 5,
    retryDelay: 100,
  });
}
