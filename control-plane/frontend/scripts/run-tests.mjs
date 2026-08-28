import { existsSync } from "node:fs";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawn } from "node:child_process";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const nodeModules = resolve(
  process.env.OSTIARI_FRONTEND_NODE_MODULES || join(frontendRoot, "node_modules"),
);
const esbuildPath = join(nodeModules, "esbuild", "lib", "main.js");

if (!existsSync(esbuildPath)) {
  throw new Error(
    "Frontend test dependencies are unavailable. Run npm ci before npm test.",
  );
}

const { build } = await import(pathToFileURL(esbuildPath).href);
const testsRoot = join(frontendRoot, "tests");
const testFiles = (await readdir(testsRoot))
  .filter((name) => /\.test\.tsx?$/.test(name))
  .map((name) => join(testsRoot, name))
  .sort();

if (testFiles.length === 0) {
  throw new Error("No frontend behavioral tests were found.");
}

const outputRoot = await mkdtemp(join(tmpdir(), "ostiari-frontend-tests-"));

try {
  await build({
    entryPoints: testFiles,
    outdir: outputRoot,
    entryNames: "[name]",
    outExtension: { ".js": ".mjs" },
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node22",
    jsx: "automatic",
    sourcemap: "inline",
    nodePaths: [nodeModules],
    banner: {
      js: 'import { createRequire } from "node:module"; const require = createRequire(import.meta.url);',
    },
    define: {
      "import.meta.env.DEV": "false",
      "import.meta.env.BASE_URL": '"/"',
      "import.meta.env.VITE_ARCHITECTURE_STEP_MS": '"1800"',
      "import.meta.env.VITE_API_URL": '"http://localhost:8400"',
      "import.meta.env.VITE_DEMO_LOGIN": '"false"',
      "import.meta.env.VITE_PUBLIC_SITE": '"false"',
    },
    logLevel: "warning",
  });

  const outputs = (await readdir(outputRoot))
    .filter((name) => name.endsWith(".test.mjs"))
    .map((name) => join(outputRoot, name))
    .sort();

  const nodeBinary = process.env.NODE_BINARY || process.execPath;
  const child = spawn(nodeBinary, ["--test", ...outputs], {
    cwd: frontendRoot,
    stdio: "inherit",
    env: process.env,
  });

  const exitCode = await new Promise((resolveExit, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      if (signal) reject(new Error(`Frontend tests terminated by ${signal}`));
      else resolveExit(code ?? 1);
    });
  });

  if (exitCode !== 0) process.exitCode = exitCode;
} finally {
  await rm(outputRoot, { recursive: true, force: true });
}
