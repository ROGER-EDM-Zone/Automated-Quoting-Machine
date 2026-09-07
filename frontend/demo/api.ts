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

/**
 * Dropping an email cannot work without a backend to read it, so the preview
 * says so instead of failing. It answers in the shape the queue expects — the
 * same fields, no enquiries — so the page renders its "nothing came in" path
 * rather than throwing.
 */
async function inertUpload<T>(body: FormData): Promise<T> {
  window.dispatchEvent(new CustomEvent("aqm-demo-write"));
  const names = body
    .getAll("files")
    .map((f) => (f instanceof File ? f.name : String(f)));
  await new Promise((resolve) => setTimeout(resolve, 200));
  return {
    ingested: [],
    failed: names.map((filename) => ({
      filename,
      reason:
        "This is a static preview with no backend, so the email was not read. " +
        "Install the app to drop real RFQs in.",
    })),
    new_count: 0,
    already_known: 0,
  } as T;
}

/**
 * Must offer everything `src/lib/api.ts` offers. A method missing here does
 * not fail at build time — it fails in the browser as "not a function" when
 * somebody clicks the thing, which is how the drop zones broke in the
 * preview. `tests/demo-api.test.mjs` compares the two.
 */
export const api = {
  get: <T,>(path: string) => read<T>(path),
  upload: <T,>(_path: string, body: FormData) => inertUpload<T>(body),
  post: <T,>(path: string, _body?: unknown) => inert<T>(path),
  patch: <T,>(path: string, _body: unknown) => inert<T>(path),
  put: <T,>(path: string, _body: unknown) => inert<T>(path),
};
