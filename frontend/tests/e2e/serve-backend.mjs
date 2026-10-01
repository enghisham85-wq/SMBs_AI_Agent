// Seeds a fresh sample cafe into backend/var/e2e and starts the API (used by playwright.config.ts).
// Usage: node serve-backend.mjs <port> KEY=VALUE ...
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const [port, ...pairs] = process.argv.slice(2);
const backend = resolve(dirname(fileURLToPath(import.meta.url)), "../../../backend");
const env = { ...process.env };
for (const p of pairs) {
  const i = p.indexOf("=");
  env[p.slice(0, i)] = p.slice(i + 1);
}
rmSync(resolve(backend, "var/e2e"), { recursive: true, force: true });
mkdirSync(resolve(backend, "var/e2e/files"), { recursive: true });
const seed = spawnSync("uv", ["run", "python", "-m", "app.seed", "--sample-cafe", "--start", "2026-10-04"], {
  cwd: backend,
  env,
  stdio: ["ignore", "ignore", "inherit"],
  shell: process.platform === "win32",
});
if (seed.status !== 0) process.exit(seed.status ?? 1);
const api = spawn("uv", ["run", "uvicorn", "app.main:app", "--port", port], {
  cwd: backend,
  env,
  stdio: "inherit",
  shell: process.platform === "win32",
});
const stop = () => api.kill();
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
api.on("exit", (code) => process.exit(code ?? 0));
