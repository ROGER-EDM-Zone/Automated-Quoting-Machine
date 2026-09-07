/**
 * The preview swaps `src/lib/api.ts` for `demo/api.ts`. If the stub is
 * missing a method the real one has, nothing fails at build time — it fails
 * in the browser as "X is not a function" the moment somebody clicks that
 * feature, which is exactly how the drop zones broke.
 *
 * Run as part of `npm run build:demo`.
 */
import { readFileSync } from "node:fs";

const methodsIn = (file) => {
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  const block = source.match(/export const api = \{([\s\S]*?)\n\};/)?.[1] ?? "";
  return new Set([...block.matchAll(/^\s{2}(\w+):/gm)].map((m) => m[1]));
};

const real = methodsIn("../src/lib/api.ts");
const stub = methodsIn("../demo/api.ts");
const missing = [...real].filter((name) => !stub.has(name));

if (real.size === 0 || stub.size === 0) {
  console.error("Could not read the api objects — has their shape changed?");
  process.exit(1);
}

if (missing.length) {
  console.error(
    `demo/api.ts is missing ${missing.map((m) => `api.${m}()`).join(", ")}.\n` +
      "The preview will throw \"not a function\" when that feature is used.",
  );
  process.exit(1);
}

console.log(`Demo API check passed: all ${real.size} methods are stubbed.`);
