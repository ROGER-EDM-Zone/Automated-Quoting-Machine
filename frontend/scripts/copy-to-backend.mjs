/**
 * Copy the built screens to where the backend serves them from.
 *
 * The office PC runs Python, not Node. Shipping the built output in the
 * repository means one thing to install there rather than two — so
 * `backend/web/` is committed on purpose, and this script is what keeps it
 * honest. Run `npm run build:app` after any change to the screens.
 */
import { cpSync, existsSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const dist = resolve(here, "../dist");
const target = resolve(here, "../../backend/web");

if (!existsSync(dist)) {
  console.error("No dist/ to copy. Run `npm run build` first.");
  process.exit(1);
}

rmSync(target, { recursive: true, force: true });
cpSync(dist, target, { recursive: true });
console.log(`Copied the built screens to ${target}`);
