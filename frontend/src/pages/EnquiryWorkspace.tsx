import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type { Breakdown, Flag, Part, Quote, SimilarResponse, Workspace } from "../lib/types";
import { dateTime, minutes, money, processLabel, titleCase } from "../lib/format";
import {
  Card,
  ErrorBanner,
  FieldValue,
  FlagList,
  SourcedTime,
  StatusBadge,
  TimeSourceKey,
} from "../components/Primitives";

const DATA_FIELDS: { key: keyof Part; label: string }[] = [
  { key: "drawing_number", label: "Drawing" },
  { key: "revision", label: "Revision" },
  { key: "description", label: "Description" },
  { key: "quantity", label: "Quantity" },
  { key: "material", label: "Material" },
  { key: "heat_treatment", label: "Heat treatment" },
  { key: "surface_coat", label: "Surface coat" },
  { key: "finish_spec", label: "Finish" },
  { key: "tightest_tolerance", label: "Tightest tolerance" },
];

export default function EnquiryWorkspace() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.get<Workspace>(`/enquiries/${id}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await fn();
      await load();
    } catch (e) {
      const err = e as ApiError;
      setError(
        err.detail && typeof err.detail === "object" && "reasons" in (err.detail as object)
          ? (err.detail as { reasons: string[] }).reasons.join("; ")
          : err.message,
      );
    } finally {
      setBusy(null);
    }
  };

  if (!data) return <>{error ? <ErrorBanner error={error} /> : <p className="empty">Loading…</p>}</>;

  const { enquiry, current_quote: quote, breakdown } = data;

  return (
    <>
      <ErrorBanner error={error} />
      {notice && <div className="notice">{notice}</div>}

      <Card
        title={enquiry.subject ?? `Enquiry ${enquiry.id}`}
        actions={<StatusBadge status={enquiry.status} />}
      >
        <div className="grid-2">
          <div>
            <p style={{ margin: "0 0 8px" }}>
              {enquiry.customer ? (
                <Link to={`/customer/${enquiry.customer.id}`}>{enquiry.customer.name}</Link>
              ) : (
                <span className="muted">Unrecognised sender — no customer record</span>
              )}
              {enquiry.sender_email && <span className="muted"> · {enquiry.sender_email}</span>}
            </p>
            <p className="muted" style={{ margin: 0, fontSize: 13 }}>
              Received {dateTime(enquiry.received_at)}
              {enquiry.due_date && ` · due ${enquiry.due_date}`}
              {enquiry.customer_reference && ` · their ref ${enquiry.customer_reference}`}
              {enquiry.anchor_quote_id && ` · anchored to quote ${enquiry.anchor_quote_id}`}
            </p>
            {enquiry.error_detail && <p className="error-banner" style={{ marginTop: 12 }}>{enquiry.error_detail}</p>}
          </div>
          <div className="button-row">
            <button onClick={() => run("extract", () => api.post(`/enquiries/${enquiry.id}/extract`))} disabled={!!busy}>
              {busy === "extract" ? "Extracting…" : "Re-run extraction"}
            </button>
            <button onClick={() => run("classify", () => api.post(`/enquiries/${enquiry.id}/classify`))} disabled={!!busy}>
              {busy === "classify" ? "Classifying…" : "Re-run classification"}
            </button>
            <button
              className="primary"
              onClick={() => run("price", () => api.post(`/enquiries/${enquiry.id}/price`, {}))}
              disabled={!!busy}
            >
              {busy === "price" ? "Pricing…" : "Recompute price"}
            </button>
          </div>
        </div>
      </Card>

      <div className="grid-main">
        <div>
          {enquiry.parts.map((part) => (
            <PartPanel
              key={part.id}
              part={part}
              breakdown={breakdown}
              ambiguousPaths={data.ambiguous_paths[part.id]}
              onSaved={load}
              onError={setError}
            />
          ))}
          {enquiry.parts.length === 0 && (
            <Card title="Parts">
              <p className="empty">Nothing extracted yet. Run extraction to read the drawings.</p>
            </Card>
          )}

          {quote && breakdown && <CostBuildUp quote={quote} breakdown={breakdown} />}
          {quote && <NotesThread quote={quote} onPosted={load} onError={setError} />}
        </div>

        <div>
          <Card title="Flags" hint="blocking flags must be cleared before approval">
            <FlagList
              flags={[
                ...data.enquiry_flags,
                ...enquiry.parts.flatMap((p) => p.flags),
                ...(quote?.flags ?? []),
              ]}
              onResolve={(flag: Flag) =>
                run("resolve", () => api.post(`/flags/${flag.id}/resolve`, { note: null }))
              }
            />
          </Card>

          {quote && (
            <ApprovalPanel
              quote={quote}
              canApprove={data.can_approve}
              blockingCount={data.blocking_flag_count}
              busy={busy}
              onAction={run}
              onNotice={setNotice}
            />
          )}

          <Card title="Attachments">
            {enquiry.attachments.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 14 }}>
                {enquiry.attachments.map((a) => (
                  <li key={a.id}>
                    {a.filename}{" "}
                    <span className={`badge badge-${a.kind === "drawing" ? "info" : "muted"}`}>{a.kind}</span>
                    {a.kind === "step" && <div className="muted" style={{ fontSize: 12 }}>Stored, not read — 3D reading is deferred.</div>}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {enquiry.parts[0] && <SimilarJobs partId={enquiry.parts[0].id} />}
        </div>
      </div>
    </>
  );
}

function PartPanel({
  part,
  breakdown,
  ambiguousPaths,
  onSaved,
  onError,
}: {
  part: Part;
  breakdown: Breakdown | null;
  ambiguousPaths?: Record<string, { value: string; material_total: string; labour_total: string }>;
  onSaved: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const confidences = part.extraction_confidence ?? {};
  const withheld = (part.withheld_fields ?? {}) as Record<string, unknown>;
  const partCosts = breakdown?.parts.find((p) => p.part_id === part.id);

  const save = async (field: string) => {
    try {
      const value = field === "quantity" ? Number(draft) : draft;
      await api.patch(`/parts/${part.id}`, { [field]: value });
      setEditing(null);
      await onSaved();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <Card
      title={`${part.drawing_number ?? "Drawing not read"}${part.revision ? ` rev ${part.revision}` : ""}`}
      hint={part.description ?? undefined}
      actions={
        <span className={`badge badge-${part.job_type === "ambiguous" ? "block" : "muted"}`}>
          {titleCase(part.job_type)}
        </span>
      }
    >
      {part.job_type === "ambiguous" && ambiguousPaths && (
        <>
          <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
            Service-only or full supply is unresolved, so both cost paths are shown. Nothing has been
            chosen — set the job type to pick one.
          </p>
          <div className="paths" style={{ marginBottom: 16 }}>
            {Object.entries(ambiguousPaths).map(([name, price]) => (
              <div className="path" key={name}>
                <h4>{titleCase(name)}</h4>
                <div className="num" style={{ fontSize: 18 }}>{money(price.value)}</div>
                <div className="muted" style={{ fontSize: 12 }}>
                  labour {money(price.labour_total)} · material {money(price.material_total)}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <table style={{ marginBottom: 16 }}>
        <tbody>
          {DATA_FIELDS.map(({ key, label }) => {
            const value = part[key] as string | number | null;
            return (
              <tr key={String(key)}>
                <th style={{ width: 170, borderBottom: "1px solid #eef1f4" }}>{label}</th>
                <td>
                  {editing === key ? (
                    <div className="button-row">
                      <input value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus />
                      <button className="primary" onClick={() => void save(String(key))}>Save</button>
                      <button onClick={() => setEditing(null)}>Cancel</button>
                    </div>
                  ) : (
                    <span
                      onDoubleClick={() => {
                        setEditing(String(key));
                        setDraft(value == null ? "" : String(value));
                      }}
                      title="Double-click to correct"
                      style={{ cursor: "text" }}
                    >
                      <FieldValue
                        value={value}
                        confidence={confidences[key as string]}
                        withheld={withheld[key as string]}
                      />
                      {key === "quantity" && part.quantity_source && (
                        <span className="muted" style={{ fontSize: 12, marginLeft: 6 }}>
                          from the {part.quantity_source}
                        </span>
                      )}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
          <tr>
            <th style={{ borderBottom: "none" }}>Envelope</th>
            <td>
              {part.envelope_x ? (
                `${part.envelope_x} × ${part.envelope_y} × ${part.envelope_z}`
              ) : (
                <span className="unread">not read</span>
              )}
            </td>
          </tr>
        </tbody>
      </table>

      <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: ".04em", color: "var(--muted)" }}>
        Operations
      </h3>
      {part.process_mix_constrained && (
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Routing constrained to the processes the customer named.
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>Op</th>
            <th>Process</th>
            <th>Description</th>
            <th className="num">Set</th>
            <th className="num">Run/unit</th>
            <th className="num">Rate</th>
            <th className="num">Cost</th>
          </tr>
        </thead>
        <tbody>
          {part.operations.map((op) => (
            <tr key={op.id}>
              <td className="num">{op.op_number}</td>
              <td>{processLabel(op.process)}</td>
              <td>{op.description ?? <span className="muted">—</span>}</td>
              <td className="num"><SourcedTime value={op.set_time_mins} source={op.time_source} /></td>
              <td className="num"><SourcedTime value={op.run_time_mins_per_unit} source={op.time_source} /></td>
              <td className="num">
                {op.process === "subcontract" ? `${money(op.subcontract_unit_cost)}/ea` : money(op.hourly_rate)}
              </td>
              <td className="num">{money(op.computed_cost)}</td>
            </tr>
          ))}
          {part.operations.length === 0 && (
            <tr><td colSpan={7} className="empty">No operations yet.</td></tr>
          )}
        </tbody>
      </table>
      <div style={{ marginTop: 8 }}><TimeSourceKey /></div>
      {partCosts?.uses_untrusted_times && (
        <p className="notice" style={{ marginTop: 12 }}>
          This part uses times the AI estimated from past jobs. Check them before approving.
        </p>
      )}

      {part.material_requirements.length > 0 && (
        <>
          <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: ".04em", color: "var(--muted)" }}>
            Material
          </h3>
          <table>
            <thead>
              <tr>
                <th>Spec</th><th>Stock</th><th className="num">Blanks/stock</th>
                <th className="num">Qty</th><th className="num">Utilisation</th><th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {part.material_requirements.map((m) => (
                <tr key={m.id}>
                  <td>{m.spec}</td>
                  <td>{m.stock_size}</td>
                  <td className="num">{m.blanks_per_unit_stock ?? "—"}</td>
                  <td className="num">{m.qty_required}</td>
                  <td className="num">{m.utilisation_pct ? `${m.utilisation_pct}%` : "—"}</td>
                  <td className="num">{money(m.total_cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Card>
  );
}

function CostBuildUp({ quote, breakdown }: { quote: Quote; breakdown: Breakdown }) {
  return (
    <Card title="Cost build-up" hint={`quote ${quote.id} v${quote.version}`}>
      {!breakdown.reconciles && (
        <div className="error-banner">
          The build-up does not reconcile with the quoted figure. Do not send this — raise it.
        </div>
      )}
      <table className="totals">
        <tbody>
          <tr><td>Labour</td><td className="num">{money(breakdown.labour_total)}</td></tr>
          <tr><td>Material</td><td className="num">{money(breakdown.material_total)}</td></tr>
          <tr><td>Subtotal</td><td className="num">{money(breakdown.subtotal)}</td></tr>
          <tr>
            <td>Margin at {Number(breakdown.margin_pct).toFixed(1)}%</td>
            <td className="num">{money(breakdown.margin_value)}</td>
          </tr>
          {breakdown.adjustments.map((adj, index) => (
            <tr key={index}>
              <td>
                {adj.description ?? titleCase(adj.rule_key)}{" "}
                <span className="muted" style={{ fontSize: 12 }}>
                  ({adj.adjustment_type === "pct" ? `${adj.adjustment_value}%` : "fixed"}, rule {adj.rule_id})
                </span>
              </td>
              <td className="num">{money(adj.effect)}</td>
            </tr>
          ))}
          {Number(breakdown.rounding_adjustment) !== 0 && (
            <tr>
              <td className="muted">Rounding to whole pence on the unit price</td>
              <td className="num muted">{money(breakdown.rounding_adjustment)}</td>
            </tr>
          )}
          <tr className="grand"><td>Quote value</td><td className="num">{money(breakdown.quote_value)}</td></tr>
        </tbody>
      </table>

      <table style={{ marginTop: 16 }}>
        <thead>
          <tr><th>Drawing</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Line total</th></tr>
        </thead>
        <tbody>
          {quote.lines.map((line) => (
            <tr key={line.id}>
              <td>{line.drawing_number}{line.revision && ` rev ${line.revision}`}</td>
              <td className="num">{line.quantity}</td>
              <td className="num">{money(line.unit_price)}</td>
              <td className="num">{money(line.line_total)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {breakdown.min_value_applied && (
        <p className="notice" style={{ marginTop: 12 }}>
          The minimum quote value rule lifted this quote. The work itself costs less.
        </p>
      )}
    </Card>
  );
}

function NotesThread({
  quote,
  onPosted,
  onError,
}: {
  quote: Quote;
  onPosted: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [text, setText] = useState("");
  const [posting, setPosting] = useState(false);

  const post = async () => {
    if (!text.trim()) return;
    setPosting(true);
    try {
      await api.post(`/quotes/${quote.id}/notes`, { note_text: text });
      setText("");
      await onPosted();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setPosting(false);
    }
  };

  return (
    <Card title="Notes" hint="context the AI could not know — it changes the inputs, the engine reprices">
      {quote.notes.map((note) => (
        <div className="note" key={note.id}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
            <strong style={{ fontSize: 13 }}>{note.author}</strong>
            <span className="muted" style={{ fontSize: 12 }}>{dateTime(note.created_at)}</span>
          </div>
          <p style={{ margin: "6px 0" }}>{note.note_text}</p>
          {note.note_kind && (
            <span className="badge badge-muted">{titleCase(note.note_kind)}</span>
          )}
          {note.adjustment_summary && <p style={{ margin: "6px 0", fontSize: 14 }}>{note.adjustment_summary}</p>}
          {note.price_before !== null && (
            <div className="price-change">
              {money(note.price_before)} → {money(note.price_after)}
              {note.price_before === note.price_after && <span className="muted"> (no change)</span>}
              {note.applied_rule_id && <span className="muted"> · rule {note.applied_rule_id}</span>}
            </div>
          )}
          {note.awaiting_answer && note.question && (
            <div className="rejected">Needs an answer: {note.question}</div>
          )}
          {note.proposed_change?.rejected?.length ? (
            <div className="rejected">
              Not applied: {note.proposed_change.rejected.map((r) => r.reason).join("; ")}
            </div>
          ) : null}
        </div>
      ))}
      {quote.notes.length === 0 && <p className="muted">No notes yet.</p>}

      <div className="field" style={{ marginTop: 12 }}>
        <label htmlFor="note">Add a note</label>
        <textarea
          id="note"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="e.g. We already have the electrode for this — drop 15 minutes of setup."
        />
      </div>
      <button className="primary" onClick={() => void post()} disabled={posting || !text.trim()}>
        {posting ? "Interpreting…" : "Add note and reprice"}
      </button>
    </Card>
  );
}

function ApprovalPanel({
  quote,
  canApprove,
  blockingCount,
  busy,
  onAction,
  onNotice,
}: {
  quote: Quote;
  canApprove: boolean;
  blockingCount: number;
  busy: string | null;
  onAction: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  onNotice: (message: string) => void;
}) {
  return (
    <Card title="Approval" hint="nothing sends without a person">
      <p style={{ marginTop: 0 }}>
        <StatusBadge status={quote.status} />{" "}
        <span className="num" style={{ fontSize: 18, marginLeft: 8 }}>{money(quote.quote_value)}</span>
      </p>
      {quote.approved_by && (
        <p className="muted" style={{ fontSize: 13 }}>
          Approved by {quote.approved_by} on {dateTime(quote.approved_at)}
        </p>
      )}
      {blockingCount > 0 && (
        <p className="error-banner">
          {blockingCount} blocking flag{blockingCount === 1 ? "" : "s"} must be resolved first.
        </p>
      )}
      <div className="button-row">
        <button
          className="primary"
          disabled={!canApprove || !!busy || quote.status !== "draft"}
          onClick={() => onAction("approve", () => api.post(`/quotes/${quote.id}/approve`, {}))}
        >
          Approve
        </button>
        <button
          disabled={quote.status !== "approved" || !!busy}
          onClick={() =>
            onAction("draft", async () => {
              const result = await api.post<{ draft_created: boolean; reason?: string }>(
                `/quotes/${quote.id}/draft-reply`,
              );
              onNotice(
                result.draft_created
                  ? "Draft created in Outlook. Review it and press send there."
                  : `Draft rendered but not created in Outlook: ${result.reason}`,
              );
            })
          }
        >
          Create draft reply
        </button>
        <button
          disabled={quote.status !== "approved" || !!busy}
          onClick={() => onAction("sent", () => api.post(`/quotes/${quote.id}/mark-sent`))}
        >
          Mark as sent
        </button>
      </div>
      {quote.status === "sent" && !quote.outcome && (
        <div style={{ marginTop: 16 }}>
          <label>Outcome</label>
          <div className="button-row">
            {(["won", "lost", "no_response"] as const).map((result) => (
              <button
                key={result}
                onClick={() => onAction("outcome", () => api.post(`/quotes/${quote.id}/outcome`, { result }))}
              >
                {titleCase(result)}
              </button>
            ))}
          </div>
        </div>
      )}
      {quote.outcome && (
        <p style={{ marginTop: 12 }}>
          <span className="badge badge-ok">{titleCase(quote.outcome.result)}</span>
          {quote.outcome.actual_production_mins && (
            <span className="muted"> · actual {minutes(quote.outcome.actual_production_mins)}</span>
          )}
        </p>
      )}
    </Card>
  );
}

function SimilarJobs({ partId }: { partId: number }) {
  const [data, setData] = useState<SimilarResponse | null>(null);

  useEffect(() => {
    api.get<SimilarResponse>(`/search/similar?part_id=${partId}`).then(setData).catch(() => setData(null));
  }, [partId]);

  if (!data) return null;
  const lanes: [string, string, typeof data.geometry][] = [
    ["Similar shapes", "envelope, material, features", data.geometry],
    ["Similar problems", "tolerance band, hardness, prior flags", data.problem],
  ];

  return (
    <Card title="Past jobs" hint="two lanes, kept separate">
      {lanes.map(([title, hint, matches]) => (
        <div key={title} style={{ marginBottom: 12 }}>
          <strong style={{ fontSize: 13 }}>{title}</strong>{" "}
          <span className="muted" style={{ fontSize: 12 }}>({hint})</span>
          {matches.length === 0 ? (
            <p className="muted" style={{ fontSize: 13, margin: "4px 0" }}>Nothing comparable in the archive.</p>
          ) : (
            <ul style={{ margin: "4px 0", paddingLeft: 18, fontSize: 13 }}>
              {matches.map((m) => (
                <li key={`${title}-${m.part_id}`}>
                  <Link to={`/enquiry/${m.enquiry_id}`}>{m.drawing_number ?? `part ${m.part_id}`}</Link>{" "}
                  {m.unit_price && <span className="num">{money(m.unit_price)}/ea</span>}
                  {m.result && <span className={`badge badge-${m.result === "won" ? "ok" : "muted"}`}>{m.result}</span>}
                  <div className="muted" style={{ fontSize: 12 }}>{m.reasons.join("; ")}</div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </Card>
  );
}
