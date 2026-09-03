import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Rate } from "../lib/types";
import { money, processLabel } from "../lib/format";
import { Card, ErrorBanner } from "../components/Primitives";

const PROCESSES = ["cnc_mill", "cnc_turn", "wire_edm", "spark_erode", "grind", "manual", "qc"];

/**
 * "Rates and rules live in the database. A rate change is a data edit, not a
 * deployment." This page is what makes that literally true.
 */
export default function AdminRates() {
  const [rates, setRates] = useState<Rate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    process: PROCESSES[0],
    machine_group: "",
    hourly_rate: "",
    effective_from: new Date().toISOString().slice(0, 10),
  });

  const load = () => api.get<Rate[]>("/admin/rates").then(setRates).catch((e) => setError(e.message));
  useEffect(() => { void load(); }, []);

  const submit = async () => {
    setError(null);
    try {
      await api.post("/admin/rates", {
        process: form.process,
        machine_group: form.machine_group || null,
        hourly_rate: form.hourly_rate,
        effective_from: form.effective_from,
      });
      setForm({ ...form, hourly_rate: "", machine_group: "" });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const today = new Date().toISOString().slice(0, 10);
  const isCurrent = (r: Rate) => r.effective_from <= today && (!r.effective_to || r.effective_to > today);

  return (
    <>
      <ErrorBanner error={error} />
      <Card title="Add a rate" hint="a new rate end-dates the one it replaces; nothing is deleted">
        <div className="grid-2">
          <div>
            <div className="field">
              <label htmlFor="process">Process</label>
              <select id="process" value={form.process} onChange={(e) => setForm({ ...form, process: e.target.value })}>
                {PROCESSES.map((p) => <option key={p} value={p}>{processLabel(p)}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="group">Machine group (optional)</label>
              <input id="group" value={form.machine_group}
                     onChange={(e) => setForm({ ...form, machine_group: e.target.value })}
                     placeholder="e.g. 5-axis" />
            </div>
          </div>
          <div>
            <div className="field">
              <label htmlFor="rate">Hourly rate (£)</label>
              <input id="rate" value={form.hourly_rate} inputMode="decimal"
                     onChange={(e) => setForm({ ...form, hourly_rate: e.target.value })} placeholder="55.00" />
            </div>
            <div className="field">
              <label htmlFor="from">Effective from</label>
              <input id="from" type="date" value={form.effective_from}
                     onChange={(e) => setForm({ ...form, effective_from: e.target.value })} />
            </div>
          </div>
        </div>
        <button className="primary" onClick={() => void submit()} disabled={!form.hourly_rate}>
          Add rate
        </button>
      </Card>

      <Card title="Rates" hint="effective_to is exclusive — the old rate stops the day the new one starts">
        <table>
          <thead>
            <tr>
              <th>Process</th><th>Machine group</th><th className="num">Rate</th>
              <th>From</th><th>To</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rates.map((rate) => (
              <tr key={rate.id}>
                <td>{processLabel(rate.process)}</td>
                <td>{rate.machine_group ?? <span className="muted">any</span>}</td>
                <td className="num">{money(rate.hourly_rate)}</td>
                <td>{rate.effective_from}</td>
                <td>{rate.effective_to ?? <span className="muted">open</span>}</td>
                <td>
                  <span className={`badge badge-${isCurrent(rate) ? "ok" : "muted"}`}>
                    {isCurrent(rate) ? "in force" : "historic"}
                  </span>
                </td>
              </tr>
            ))}
            {rates.length === 0 && (
              <tr><td colSpan={6} className="empty">No rates yet. Nothing can be quoted until there are.</td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </>
  );
}
