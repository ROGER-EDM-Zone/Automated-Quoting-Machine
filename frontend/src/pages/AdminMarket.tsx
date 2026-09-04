import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { MarketRefresh, MarketSeries, MarketStatus } from "../lib/types";
import { Card, ErrorBanner } from "../components/Primitives";

/**
 * Every number in a quote that comes from outside the business, and how old
 * it is.
 *
 * The point of the page is the age column, not the value column. A price
 * being £2.40 tells an estimator nothing on its own; £2.40 read this morning
 * and £2.40 read in March are different facts, and only one of them belongs
 * in a quote going out today.
 */

const STATUS_LABEL: Record<MarketStatus, string> = {
  current: "Current",
  stale: "Out of date",
  never_read: "Never read",
  last_refresh_failed: "Last refresh failed",
  off: "Switched off",
};

const KIND_LABEL: Record<string, string> = {
  material_price: "Materials",
  labour_rate: "Labour benchmarks",
  consumable: "Consumables",
  energy: "Energy",
  subcontract: "Subcontract",
  index: "Indices",
};

function age(series: MarketSeries): string {
  if (series.age_hours === null) return "never read";
  if (series.age_hours < 1) return "just now";
  if (series.age_hours < 24) return `${Math.round(series.age_hours)}h ago`;
  const days = Math.round(series.age_hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export default function AdminMarket() {
  const [series, setSeries] = useState<MarketSeries[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [report, setReport] = useState<MarketRefresh | null>(null);

  const load = () =>
    api
      .get<MarketSeries[]>("/admin/market")
      .then(setSeries)
      .catch((e) => setError(e.message));

  useEffect(() => {
    void load();
  }, []);

  const refresh = async (key?: string) => {
    setError(null);
    setBusy(key ?? "all");
    try {
      const path = key ? `/admin/market/refresh?series_key=${encodeURIComponent(key)}` : "/admin/market/refresh";
      setReport(await api.post<MarketRefresh>(path));
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const kinds = [...new Set(series.map((s) => s.kind))];
  const needsAttention = series.filter(
    (s) => s.active && (s.status === "stale" || s.status === "never_read" || s.status === "last_refresh_failed"),
  );

  return (
    <>
      <ErrorBanner error={error} />

      <Card
        title="Live market data"
        hint="what the outside world charges, and when we last checked"
      >
        <p className="prose">
          Every figure here is read from a supplier or published page and kept with
          the line of text it was read from. Nothing on this page is remembered or
          estimated: a source that cannot be reached shows as unread rather than
          falling back to a plausible number, because a plausible number is
          indistinguishable from a real one once it is in a quote.
        </p>
        <div className="row">
          <button onClick={() => void refresh()} disabled={busy !== null}>
            {busy === "all" ? "Checking…" : "Refresh everything now"}
          </button>
          {needsAttention.length > 0 && (
            <span className="pill pill-warn">
              {needsAttention.length} need{needsAttention.length === 1 ? "s" : ""} attention
            </span>
          )}
        </div>
      </Card>

      {report && (
        <Card title="Last refresh">
          <ul className="plain-list">
            {report.results.map((r) => (
              <li key={r.series_key} className={r.ok ? "refresh-ok" : "refresh-failed"}>
                <strong>{r.source_name}</strong> — {r.detail}
                {r.stock_rows_written > 0 && ` (${r.stock_rows_written} new stock size(s))`}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {kinds.map((kind) => (
        <Card key={kind} title={KIND_LABEL[kind] ?? kind}>
          <table>
            <thead>
              <tr>
                <th>What</th>
                <th className="num">Value</th>
                <th>Last checked</th>
                <th>Read from</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {series
                .filter((s) => s.kind === kind)
                .map((s) => (
                  <tr key={s.id} className={`market-${s.status}`}>
                    <td>
                      <div>{s.name}</div>
                      <div className="subtle mono">{s.series_key}</div>
                    </td>
                    <td className="num">
                      {s.value ? (
                        <span className={`market-value market-${s.status}`}>
                          {s.value} <span className="subtle">{s.unit.replace(/_/g, " ")}</span>
                        </span>
                      ) : (
                        <span className="unread">not read</span>
                      )}
                    </td>
                    <td>
                      <span className={`market-age market-${s.status}`}>{age(s)}</span>
                      <div className="subtle">{STATUS_LABEL[s.status]}</div>
                    </td>
                    <td className="evidence">
                      {s.evidence ? (
                        <>
                          <q>{s.evidence}</q>
                          {s.url && (
                            <div>
                              <a href={s.url} target="_blank" rel="noreferrer noopener">
                                source
                              </a>
                            </div>
                          )}
                        </>
                      ) : s.last_error ? (
                        <span className="subtle">{s.last_error}</span>
                      ) : !s.url ? (
                        <span className="subtle">
                          No address set. Give this source a page that shows the
                          number, then switch it on.
                        </span>
                      ) : (
                        <span className="subtle">—</span>
                      )}
                    </td>
                    <td>
                      <button
                        onClick={() => void refresh(s.series_key)}
                        disabled={busy !== null || !s.active || !s.url}
                      >
                        {busy === s.series_key ? "…" : "Check"}
                      </button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </Card>
      ))}

      {series.length === 0 && (
        <Card title="Nothing configured yet">
          <p className="prose">
            No market sources have been set up. Run{" "}
            <code>python -m scripts.refresh_market --seed</code> in the backend to
            write the starting set, then give each one the address of a page that
            shows its number.
          </p>
        </Card>
      )}
    </>
  );
}
