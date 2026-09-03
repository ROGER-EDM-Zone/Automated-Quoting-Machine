import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { money, titleCase } from "../lib/format";
import { Card, ErrorBanner } from "../components/Primitives";

interface Turnaround {
  enquiries: number;
  median_hours: number | null;
  mean_hours: number | null;
  within_24h_pct: number | null;
}
interface FieldAccuracy {
  corrections: number;
  confidently_wrong: number;
  corrected_after_withholding: number;
  mean_ai_confidence: number | null;
  fields_extracted: number;
  correction_rate_pct: number | null;
}
interface Accuracy {
  total_corrections: number;
  per_field: Record<string, FieldAccuracy>;
}
interface WinRate {
  outcomes_recorded: number;
  quotes_sent: number;
  win_rate_pct: number | null;
  value_quoted: string;
  value_won: string;
}
interface TimeSourceMix {
  counts: Record<string, number>;
  total: number;
  estimated_pct: number | null;
}
interface EstimateVsActual {
  by_time_source: Record<string, { jobs: number; mean_ratio: number; over_estimate_pct: number }>;
}

export default function Reports() {
  const [turnaround, setTurnaround] = useState<Turnaround | null>(null);
  const [accuracy, setAccuracy] = useState<Accuracy | null>(null);
  const [winRate, setWinRate] = useState<WinRate | null>(null);
  const [mix, setMix] = useState<TimeSourceMix | null>(null);
  const [actual, setActual] = useState<EstimateVsActual | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      api.get<Turnaround>("/reports/turnaround"),
      api.get<Accuracy>("/reports/extraction-accuracy"),
      api.get<WinRate>("/reports/win-rate"),
      api.get<TimeSourceMix>("/reports/time-source-mix"),
      api.get<EstimateVsActual>("/reports/estimate-vs-actual"),
    ])
      .then(([t, a, w, m, e]) => {
        setTurnaround(t); setAccuracy(a); setWinRate(w); setMix(m); setActual(e);
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  return (
    <>
      <ErrorBanner error={error} />

      <div className="grid-2">
        <Card title="Turnaround" hint="received to sent">
          {turnaround?.enquiries ? (
            <table className="totals">
              <tbody>
                <tr><td>Quotes sent</td><td className="num">{turnaround.enquiries}</td></tr>
                <tr><td>Median</td><td className="num">{turnaround.median_hours?.toFixed(1)} h</td></tr>
                <tr><td>Mean</td><td className="num">{turnaround.mean_hours?.toFixed(1)} h</td></tr>
                <tr><td>Within 24 hours</td><td className="num">{turnaround.within_24h_pct}%</td></tr>
              </tbody>
            </table>
          ) : (
            <p className="muted">Nothing sent yet.</p>
          )}
        </Card>

        <Card title="Win rate">
          {winRate?.outcomes_recorded ? (
            <table className="totals">
              <tbody>
                <tr><td>Quotes sent</td><td className="num">{winRate.quotes_sent}</td></tr>
                <tr><td>Outcomes recorded</td><td className="num">{winRate.outcomes_recorded}</td></tr>
                <tr><td>Win rate</td><td className="num">{winRate.win_rate_pct ?? "—"}%</td></tr>
                <tr><td>Value quoted</td><td className="num">{money(winRate.value_quoted)}</td></tr>
                <tr><td>Value won</td><td className="num">{money(winRate.value_won)}</td></tr>
              </tbody>
            </table>
          ) : (
            <p className="muted">
              No outcomes recorded. Without them the archive knows what was quoted but never
              whether it was right.
            </p>
          )}
        </Card>
      </div>

      <Card
        title="Extraction accuracy"
        hint="from correction_log — the number that says whether this saves time or adds checking"
      >
        {accuracy && Object.keys(accuracy.per_field).length > 0 ? (
          <>
            <table>
              <thead>
                <tr>
                  <th>Field</th>
                  <th className="num">Extracted</th>
                  <th className="num">Corrected</th>
                  <th className="num">Correction rate</th>
                  <th className="num">Confidently wrong</th>
                  <th className="num">Caught by a flag</th>
                  <th className="num">Mean confidence</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(accuracy.per_field).map(([field, stats]) => (
                  <tr key={field}>
                    <td>{titleCase(field)}</td>
                    <td className="num">{stats.fields_extracted}</td>
                    <td className="num">{stats.corrections}</td>
                    <td className="num">{stats.correction_rate_pct ?? "—"}%</td>
                    <td className="num">
                      {stats.confidently_wrong > 0 ? (
                        <span className="badge badge-block">{stats.confidently_wrong}</span>
                      ) : (
                        0
                      )}
                    </td>
                    <td className="num">{stats.corrected_after_withholding}</td>
                    <td className="num">{stats.mean_ai_confidence ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted" style={{ fontSize: 13 }}>
              “Confidently wrong” is a correction to a field the extractor was sure about. Those are
              the ones that would have flowed into a price unnoticed. “Caught by a flag” means the
              value was withheld and an estimator supplied it — that is the system working.
            </p>
          </>
        ) : (
          <p className="muted">No corrections recorded yet.</p>
        )}
      </Card>

      <div className="grid-2">
        <Card title="Where quoted times come from">
          {mix?.total ? (
            <>
              <table className="totals">
                <tbody>
                  {Object.entries(mix.counts).map(([source, count]) => (
                    <tr key={source}>
                      <td>{titleCase(source)}</td>
                      <td className="num">{count}</td>
                    </tr>
                  ))}
                  <tr className="grand">
                    <td>AI-estimated share</td>
                    <td className="num">{mix.estimated_pct ?? 0}%</td>
                  </tr>
                </tbody>
              </table>
              <p className="muted" style={{ fontSize: 13 }}>
                How much of the quoted work rests on numbers that need checking.
              </p>
            </>
          ) : (
            <p className="muted">No operations recorded.</p>
          )}
        </Card>

        <Card title="Estimate vs actual" hint="by where the time came from">
          {actual && Object.keys(actual.by_time_source).length > 0 ? (
            <table>
              <thead>
                <tr><th>Time source</th><th className="num">Jobs</th><th className="num">Actual / quoted</th><th className="num">Over by &gt;10%</th></tr>
              </thead>
              <tbody>
                {Object.entries(actual.by_time_source).map(([source, stats]) => (
                  <tr key={source}>
                    <td>{titleCase(source)}</td>
                    <td className="num">{stats.jobs}</td>
                    <td className="num">{stats.mean_ratio.toFixed(2)}×</td>
                    <td className="num">{stats.over_estimate_pct}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted">
              No actual production times recorded yet. This is what calibrates future estimates.
            </p>
          )}
        </Card>
      </div>
    </>
  );
}
