import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import type { LaneCount, LaneName, QueueItem } from "../lib/types";
import { age, dateTime, money, processLabel } from "../lib/format";
import { Card, ErrorBanner, StatusBadge } from "../components/Primitives";

type Sort = "age" | "value" | "flags" | "confidence";

/**
 * What an empty lane means. "Nothing waiting" is ambiguous — an empty Needs
 * attention is good news and an empty Ready to send is not the same news at
 * all, so each lane says its own thing.
 */
const EMPTY_MESSAGES: Record<LaneName, string> = {
  needs_attention: "Nothing is blocked. Everything is moving.",
  coming_in: "No new enquiries being read right now.",
  to_check: "Nothing waiting to be checked.",
  ready_to_send: "Nothing approved and waiting to go out.",
  awaiting_feedback: "No quotes out with customers.",
  closed: "No won or lost jobs recorded yet.",
};

const SORT_LABELS: Record<Sort, string> = {
  age: "Oldest first",
  value: "Highest value",
  flags: "Most blocking flags",
  confidence: "Lowest confidence",
};

interface PollResult {
  checked: number;
  new_enquiries: number[];
  already_known: number;
  failed: string[];
}

/**
 * The queue, split into the lists an estimator actually works.
 *
 * One sorted list answers "what is in the system". It does not answer "what
 * do I have to do next", which is the question people open this screen with —
 * and mixing a blocked enquiry in with one that is finished and waiting on
 * the customer is how quotes get forgotten.
 *
 * The lanes and their counts are decided on the server, so a tab's badge and
 * its contents cannot disagree.
 */
export default function Queue() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [lanes, setLanes] = useState<LaneCount[]>([]);
  const [lane, setLane] = useState<LaneName>(
    // Come back to the list you were working, not to the top of the pile.
    (localStorage.getItem("aqm.lane") as LaneName | null) ?? "needs_attention",
  );
  const [sort, setSort] = useState<Sort>("flags");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [pollNotice, setPollNotice] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    setLoading(true);
    api
      .get<QueueItem[]>(`/queue?sort=${sort}&lane=${lane}`)
      .then(setItems)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [sort, lane, reloadKey]);

  // Counts reload alongside the rows: acting on an enquiry moves it between
  // lanes, and a stale badge is worse than no badge.
  useEffect(() => {
    api
      .get<LaneCount[]>("/queue/lanes")
      .then(setLanes)
      .catch((e) => setError(e.message));
  }, [reloadKey, lane]);

  const chooseLane = (next: LaneName) => {
    setLane(next);
    try {
      localStorage.setItem("aqm.lane", next);
    } catch {
      // A browser refusing storage is not a reason to fail to change tab.
    }
  };

  const current = lanes.find((l) => l.lane === lane);

  /** Pull the mailbox on demand, rather than waiting for a notification. */
  const checkMailbox = async () => {
    setChecking(true);
    setError(null);
    setPollNotice(null);
    try {
      const result = await api.post<PollResult>("/intake/poll");
      const found = result.new_enquiries.length;
      setPollNotice(
        found === 0
          ? `Nothing new. Checked ${result.checked} tagged message${result.checked === 1 ? "" : "s"}.`
          : `${found} new enquir${found === 1 ? "y" : "ies"} pulled in.`,
      );
      if (result.failed.length > 0) {
        setError(`Some messages could not be read: ${result.failed.join("; ")}`);
      }
      if (found > 0) setReloadKey((k) => k + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setChecking(false);
    }
  };

  return (
    <>
      <ErrorBanner error={error} />
      {pollNotice && <div className="notice">{pollNotice}</div>}
      <nav className="lane-tabs" aria-label="Working lists">
        {lanes.map((entry) => (
          <button
            key={entry.lane}
            className={`lane-tab lane-${entry.lane}${entry.lane === lane ? " selected" : ""}`}
            aria-current={entry.lane === lane ? "page" : undefined}
            onClick={() => chooseLane(entry.lane)}
          >
            <span className="lane-label">{entry.label}</span>
            {/* A zero is shown, not hidden: "nothing to send" is information. */}
            <span className="lane-count">{entry.count}</span>
          </button>
        ))}
      </nav>

      <Card
        title={current?.label ?? "Queue"}
        hint={current?.hint}
        actions={
          <div className="button-row">
            <button className="primary" onClick={() => void checkMailbox()} disabled={checking}>
              {checking ? "Checking…" : "Check for new enquiries"}
            </button>
            <select value={sort} onChange={(e) => setSort(e.target.value as Sort)} style={{ width: 200 }}>
              {Object.entries(SORT_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </div>
        }
      >
        {loading ? (
          <p className="empty">Loading…</p>
        ) : items.length === 0 ? (
          <p className="empty">{EMPTY_MESSAGES[lane]}</p>
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
