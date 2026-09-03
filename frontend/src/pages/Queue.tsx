import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import type { QueueItem } from "../lib/types";
import { age, dateTime, money, processLabel } from "../lib/format";
import { Card, ErrorBanner, StatusBadge } from "../components/Primitives";

type Sort = "age" | "value" | "flags" | "confidence";

const SORT_LABELS: Record<Sort, string> = {
  age: "Oldest first",
  value: "Highest value",
  flags: "Most blocking flags",
  confidence: "Lowest confidence",
};

export default function Queue() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [sort, setSort] = useState<Sort>("flags");
  const [includeClosed, setIncludeClosed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .get<QueueItem[]>(`/queue?sort=${sort}&include_closed=${includeClosed}`)
      .then(setItems)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [sort, includeClosed]);

  return (
    <>
      <ErrorBanner error={error} />
      <Card
        title="Triage queue"
        hint={`${items.length} enquiries`}
        actions={
          <div className="button-row">
            <select value={sort} onChange={(e) => setSort(e.target.value as Sort)} style={{ width: 200 }}>
              {Object.entries(SORT_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <label style={{ display: "flex", gap: 6, alignItems: "center", margin: 0 }}>
              <input
                type="checkbox"
                checked={includeClosed}
                onChange={(e) => setIncludeClosed(e.target.checked)}
                style={{ width: "auto" }}
              />
              Include sent and closed
            </label>
          </div>
        }
      >
        {loading ? (
          <p className="empty">Loading…</p>
        ) : items.length === 0 ? (
          <p className="empty">Nothing waiting.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Customer</th>
                <th>Subject</th>
                <th>Status</th>
                <th>Parts</th>
                <th>Process</th>
                <th className="num">Qty</th>
                <th className="num">Value</th>
                <th>Flags</th>
                <th className="num">Lowest conf.</th>
                <th className="num">Age</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.enquiry_id}>
                  <td>{item.customer_name ?? <span className="muted">Unknown</span>}</td>
                  <td>
                    <Link to={`/enquiry/${item.enquiry_id}`}>{item.subject ?? `Enquiry ${item.enquiry_id}`}</Link>
                    <div className="muted" style={{ fontSize: 12 }}>
                      {dateTime(item.received_at)}
                      {item.due_date && ` · due ${item.due_date}`}
                    </div>
                  </td>
                  <td><StatusBadge status={item.status} /></td>
                  <td className="num">{item.part_count}</td>
                  <td style={{ fontSize: 13 }}>
                    {item.process_mix.length ? item.process_mix.map(processLabel).join(", ") : <span className="muted">—</span>}
                  </td>
                  <td className="num">{item.total_quantity || "—"}</td>
                  <td className="num">{money(item.quote_value)}</td>
                  <td>
                    {item.blocking_flag_count > 0 && (
                      <span className="badge badge-block">{item.blocking_flag_count} blocking</span>
                    )}{" "}
                    {item.flag_count - item.blocking_flag_count > 0 && (
                      <span className="badge badge-muted">
                        {item.flag_count - item.blocking_flag_count} other
                      </span>
                    )}
                    {item.flag_count === 0 && <span className="muted">—</span>}
                  </td>
                  <td className="num">
                    {item.lowest_confidence == null ? (
                      <span className="muted">—</span>
                    ) : (
                      <span className={item.lowest_confidence < 0.85 ? "confidence low" : "confidence"}>
                        {(item.lowest_confidence * 100).toFixed(0)}%
                      </span>
                    )}
                  </td>
                  <td className="num">{age(item.age_hours)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
