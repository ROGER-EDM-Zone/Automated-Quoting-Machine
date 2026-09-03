/**
 * Fixture-backed stand-in for `src/lib/api.ts`, used only by the shareable
 * static preview (`npm run build:demo`).
 *
 * The pages, components and styles are the real ones — only the transport is
 * swapped, so what a viewer sees is genuinely this app's UI rendered from
 * genuine API responses, recorded from a running backend. Nothing is mocked
 * up by hand.
 *
 * Writes cannot work without a backend, so they resolve unchanged rather than
 * pretending to succeed; the banner in `main.tsx` says so plainly.
 */

import fixtures from "./fixtures.json";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(message);
  }
}

const RECORDED = fixtures as Record<string, unknown>;

/** Resolve a request path against what was recorded, ignoring query order. */
function lookup(path: string): unknown {
  if (path in RECORDED) return RECORDED[path];

  const [base] = path.split("?");
  const candidates = Object.keys(RECORDED).filter((key) => key.split("?")[0] === base);
  if (candidates.length === 0) return undefined;

  // Prefer a recording whose query string matches; otherwise any for this path.
  const query = path.includes("?") ? path.slice(path.indexOf("?")) : "";
  return RECORDED[candidates.find((key) => key.endsWith(query)) ?? candidates[0]];
}

async function read<T>(path: string): Promise<T> {
  const found = lookup(path);
  if (found === undefined) {
    throw new ApiError(
      `This preview has no recorded response for ${path}. Run the real app to see it.`,
      404,
      null,
    );
  }
  // A tick of delay so loading states render as they do against a real API.
  await new Promise((resolve) => setTimeout(resolve, 60));
  return structuredClone(found) as T;
}

/** Writes are inert here. The banner explains why, so nothing looks broken. */
async function inert<T>(path: string): Promise<T> {
  window.dispatchEvent(new CustomEvent("aqm-demo-write"));
  const base = path.replace(/\/(approve|mark-sent|notes|outcome|revise|resolve|price|extract|classify|draft-reply)$/, "");
  const found = lookup(base) ?? lookup(path);
  await new Promise((resolve) => setTimeout(resolve, 60));
  return structuredClone(found ?? {}) as T;
}

export const api = {
  get: <T,>(path: string) => read<T>(path),
  post: <T,>(path: string, _body?: unknown) => inert<T>(path),
  patch: <T,>(path: string, _body: unknown) => inert<T>(path),
  put: <T,>(path: string, _body: unknown) => inert<T>(path),
};
