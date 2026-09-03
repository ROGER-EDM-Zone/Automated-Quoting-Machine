import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import type { Customer } from "../lib/types";
import { dateTime, money, titleCase } from "../lib/format";
import { Card, ErrorBanner, StatusBadge } from "../components/Primitives";

interface HistoryRow {
  enquiry_id: number;
  subject: string | null;
  received_at: string | null;
  status: string;
  drawing_numbers: string[];
  quote_id: number | null;
  quote_value: string | null;
  outcome: string | null;
}
interface CustomerDetail {
  customer: Customer;
  enquiry_count: number;
  quotes_sent: number;
  win_rate_pct: number | null;
  value_won: string;
  mean_turnaround_hours: number | null;
  history: HistoryRow[];
}

export default function CustomerPage() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<CustomerDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.get<CustomerDetail>(`/customers/${id}`).then(setData).catch((e) => setError(e.message));
  }, [id]);

  if (!data) return <>{error ? <ErrorBanner error={error} /> : <p className="empty">Loading…</p>}</>;
  const { customer } = data;

  return (
    <>
      <ErrorBanner error={error} />
      <div className="grid-2">
        <Card title={customer.name} hint={customer.domain ?? undefined}>
          <table className="totals">
            <tbody>
              <tr><td>Standard margin</td><td className="num">{Number(customer.default_margin_pct).toFixed(1)}%</td></tr>
              <tr><td>Standard lead time</td><td className="num">{customer.default_lead_days} days</td></tr>
              <tr>
                <td>Normally supplies material</td>
                <td className="num">{customer.is_material_supplied_default ? "Yes" : "No"}</td>
              </tr>
              <tr><td>Requires certification</td><td className="num">{customer.requires_cert ? "Yes" : "No"}</td></tr>
            </tbody>
          </table>
          {customer.notes && <p style={{ marginBottom: 0 }}>{customer.notes}</p>}
        </Card>

        <Card title="Record">
          <table className="totals">
            <tbody>
              <tr><td>Enquiries</td><td className="num">{data.enquiry_count}</td></tr>
              <tr><td>Quotes sent</td><td className="num">{data.quotes_sent}</td></tr>
              <tr><td>Win rate</td><td className="num">{data.win_rate_pct ?? "—"}%</td></tr>
              <tr><td>Value won</td><td className="num">{money(data.value_won)}</td></tr>
              <tr>
                <td>Mean turnaround</td>
                <td className="num">{data.mean_turnaround_hours ? `${data.mean_turnaround_hours} h` : "—"}</td>
              </tr>
            </tbody>
          </table>
        </Card>
      </div>

      <Card title="Quote history">
        <table>
          <thead>
            <tr><th>Received</th><th>Subject</th><th>Drawings</th><th>Status</th><th className="num">Value</th><th>Outcome</th></tr>
          </thead>
          <tbody>
            {data.history.map((row) => (
              <tr key={row.enquiry_id}>
                <td>{dateTime(row.received_at)}</td>
                <td><Link to={`/enquiry/${row.enquiry_id}`}>{row.subject ?? `Enquiry ${row.enquiry_id}`}</Link></td>
                <td>{row.drawing_numbers.join(", ") || <span className="muted">—</span>}</td>
                <td><StatusBadge status={row.status} /></td>
                <td className="num">{money(row.quote_value)}</td>
                <td>{row.outcome ? <span className="badge badge-muted">{titleCase(row.outcome)}</span> : <span className="muted">—</span>}</td>
              </tr>
            ))}
            {data.history.length === 0 && <tr><td colSpan={6} className="empty">No enquiries yet.</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  );
}
