// One command to run the app locally in demo mode: `npm run dev` (or `node scripts/dev.mjs`).
// Builds the web app when its sources changed, then starts the database and every service (server/scripts/dev.py).
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const web = join(root, "web");
const python = join(root, "server", ".venv", "bin", "python");

function fail(msg) {
  console.error(`\n${msg}\n\nFirst-time setup (see README, "Run locally without Docker"):\n  cd server && uv sync --frozen --extra dev --extra browser && .venv/bin/playwright install chromium\n  cd ../web && npm ci\n`);
  process.exit(1);
}
if (!existsSync(python)) fail("The Python environment is missing (server/.venv).");
if (!existsSync(join(web, "node_modules"))) fail("The web app's packages are missing (web/node_modules).");

const newest = (dir) => readdirSync(dir, { withFileTypes: true })
  .reduce((m, e) => Math.max(m, e.isDirectory() ? newest(join(dir, e.name)) : statSync(join(dir, e.name)).mtimeMs), 0);
const built = join(web, "dist", "index.html");
if (!existsSync(built) || newest(join(web, "src")) > statSync(built).mtimeMs) {
  console.log("Building the web app…");
  const r = spawnSync(process.execPath, [join(web, "node_modules", "vite", "bin", "vite.js"), "build"], { cwd: web, stdio: "inherit" });
  if (r.status !== 0) process.exit(r.status ?? 1);
}

const child = spawn(python, [join(root, "server", "scripts", "dev.py")], { cwd: join(root, "server"), stdio: "inherit" });
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => child.kill(sig));
child.on("exit", (code) => process.exit(code ?? 0));
