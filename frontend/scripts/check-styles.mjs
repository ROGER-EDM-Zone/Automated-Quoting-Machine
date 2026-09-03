/**
 * Guard for the spec's two visual rules.
 *
 * Both are implemented as CSS classes derived from data at runtime
 * (`time-${source}`), so a renamed enum value or a typo'd selector silently
 * removes the distinction rather than breaking anything — which is exactly
 * how the AI-estimate styling was dead on arrival the first time.
 *
 * Run with: npm run check:styles
 */
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

// Must match TimeSource in src/lib/types.ts and app/enums.py.
const TIME_SOURCES = ["calculator", "historical_estimate", "manual"];
const failures = [];

for (const source of TIME_SOURCES) {
  if (!css.includes(`.time-${source}`)) {
    failures.push(
      `No CSS rule for .time-${source}. Calculator-sourced and AI-estimated ` +
        `times must always look different (spec section 6).`,
    );
  }
}

if (!css.includes(".unread")) {
  failures.push("No .unread rule. A withheld field must not render as a value.");
}

// The estimate is the one that must stand out; plain text is not enough.
const estimateBlock = css.match(/\.time-historical_estimate\s*\{[^}]*\}/)?.[0] ?? "";
if (!/background/.test(estimateBlock) || !/border/.test(estimateBlock)) {
  failures.push(
    ".time-historical_estimate must set both a background and a border — an " +
      "AI estimate has to be visible at a glance, not on close reading.",
  );
}

if (failures.length) {
  console.error("Style guard failed:\n" + failures.map((f) => `  - ${f}`).join("\n"));
  process.exit(1);
}
console.log(`Style guard passed: ${TIME_SOURCES.length} time sources and .unread all styled.`);
