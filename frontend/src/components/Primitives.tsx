import type { ReactNode } from "react";
import type { Flag, FlagSeverity, TimeSource } from "../lib/types";
import { minutes, titleCase } from "../lib/format";

/**
 * A number whose trustworthiness is visible.
 *
 * Spec section 6: "Calculator-sourced vs AI-estimated numbers look different
 * in the UI. Always." An estimator must be able to tell at a glance which
 * numbers to trust blind and which to check, so this is the only component
 * that renders an operation time.
 */
export function SourcedTime({ value, source }: { value: string | null; source: TimeSource }) {
  const label: Record<TimeSource, string> = {
    calculator: "From the time calculator",
    historical_estimate: "Estimated by AI from a similar past job — check this",
    manual: "Entered by hand",
  };
  return (
    <span className={`time-${source}`} title={label[source]}>
      {source === "historical_estimate" && "≈"}
      {minutes(value)}
    </span>
  );
}

export function TimeSourceKey() {
  return (
    <div className="source-key">
      <span><i className="swatch calculator" /> calculator</span>
      <span><i className="swatch estimated" /> AI estimate from history — check</span>
      <span><i className="swatch manual" /> entered by hand</span>
    </div>
  );
}

/**
 * A field the extractor could not read, or read too uncertainly to use.
 *
 * "Every extracted field carries a confidence; low confidence surfaces as
 * unread, not as a value." The withheld reading is offered as a question the
 * estimator can answer — never as the value itself.
 */
export function FieldValue({
  value,
  confidence,
  withheld,
}: {
  value: ReactNode;
  confidence?: number | null;
  withheld?: unknown;
}) {
  if (value === null || value === undefined || value === "") {
    return (
      <span className="unread">
        not read
        {withheld !== undefined && withheld !== null && (
          <span className="withheld-note">
            AI read “{String(withheld)}”
            {confidence != null && ` at ${(confidence * 100).toFixed(0)}% confidence`} — confirm
            before quoting
          </span>
        )}
      </span>
    );
  }
  return (
    <>
      {value}
      {confidence != null && (
        <span className={`confidence${confidence < 0.85 ? " low" : ""}`}>
          {(confidence * 100).toFixed(0)}%
        </span>
      )}
    </>
  );
}

export function SeverityBadge({ severity }: { severity: FlagSeverity }) {
  return <span className={`badge badge-${severity}`}>{severity}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "needs_attention" || status === "failed"
      ? "block"
      : status === "sent" || status === "won" || status === "approved"
        ? "ok"
        : status === "in_review" || status === "priced"
          ? "info"
          : "muted";
  return <span className={`badge badge-${tone}`}>{titleCase(status)}</span>;
}

export function FlagList({
  flags,
  onResolve,
}: {
  flags: Flag[];
  onResolve?: (flag: Flag) => void;
}) {
  if (flags.length === 0) return <p className="muted">No flags.</p>;
  const order: Record<FlagSeverity, number> = { block: 0, warn: 1, info: 2 };
  const sorted = [...flags].sort(
    (a, b) => Number(a.resolved) - Number(b.resolved) || order[a.severity] - order[b.severity],
  );
  return (
    <>
      {sorted.map((flag) => (
        <div key={flag.id} className={`flag flag-${flag.severity}${flag.resolved ? " resolved" : ""}`}>
          <p>{flag.message}</p>
          <div className="meta">
            {titleCase(flag.category)}
            {flag.field_name && ` · ${titleCase(flag.field_name)}`}
            {flag.resolved && ` · resolved by ${flag.resolved_by}`}
            {!flag.resolved && onResolve && (
              <>
                {" · "}
                <button onClick={() => onResolve(flag)} style={{ padding: "1px 8px", fontSize: 12 }}>
                  Resolve
                </button>
              </>
            )}
          </div>
        </div>
      ))}
    </>
  );
}

export function Card({
  title,
  hint,
  children,
  actions,
}: {
  title?: string;
  hint?: string;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="card">
      {title && (
        <h2 style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>
            {title}
            {hint && <span className="hint">{hint}</span>}
          </span>
          {actions}
        </h2>
      )}
      {children}
    </section>
  );
}

export function ErrorBanner({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error-banner">{error}</div>;
}
