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

/**
 * Material price provenance. Same failure mode as the time sources: the class
 * is built from data (`price-${state}`), so a rename removes the distinction
 * silently. A live price and one typed in last year must not look alike.
 */
const PRICE_STATES = ["price-live", "price-stale", "price-typed"];
for (const state of PRICE_STATES) {
  if (!css.includes(`.${state}`)) {
    failures.push(
      `No CSS rule for .${state}. A live material price, a stale one and a ` +
        `hand-typed one must be tellable apart at a glance.`,
    );
  }
}

for (const state of ["price-stale", "price-typed"]) {
  const block = css.match(new RegExp(`\\.${state}\\s*\\{[^}]*\\}`))?.[0] ?? "";
  if (!/background/.test(block) || !/border/.test(block)) {
    failures.push(
      `.${state} must set both a background and a border. A price that is not ` +
        `current has to be visible while scanning, not on inspection.`,
    );
  }
}

// Market data statuses, which must match MarketStatus in src/lib/types.ts.
const MARKET_STATUSES = ["current", "stale", "never_read", "last_refresh_failed", "off"];
for (const status of MARKET_STATUSES) {
  if (!css.includes(`.market-${status}`)) {
    failures.push(
      `No CSS rule for .market-${status}. Every market status has to render ` +
        `differently, or the market page shows five states in one colour.`,
    );
  }
}

if (failures.length) {
  console.error("Style guard failed:\n" + failures.map((f) => `  - ${f}`).join("\n"));
  process.exit(1);
}
console.log(
  `Style guard passed: ${TIME_SOURCES.length} time sources, ` +
    `${PRICE_STATES.length} price states, ${MARKET_STATUSES.length} market ` +
    `statuses and .unread all styled.`,
);
