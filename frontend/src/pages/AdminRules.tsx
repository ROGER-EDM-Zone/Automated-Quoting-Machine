import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Rule } from "../lib/types";
import { titleCase } from "../lib/format";
import { Card, ErrorBanner } from "../components/Primitives";

interface Candidate {
  summary: string;
  occurrences: number;
  note_ids: number[];
}

/**
 * Adjustment sizes live here, not in the AI. If a rule does not exist, the
 * note loop asks rather than picking a percentage.
 */
export default function AdminRules() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    rule_key: "",
    trigger_description: "",
    adjustment_type: "pct",
    adjustment_value: "",
  });

  const load = async () => {
    try {
      setRules(await api.get<Rule[]>("/admin/rules"));
      setCandidates(await api.get<Candidate[]>("/admin/rules/promotion-candidates"));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => { void load(); }, []);

  const submit = async () => {
    setError(null);
    try {
      await api.post("/admin/rules", { ...form, adjustment_value: form.adjustment_value || "0" });
      setForm({ rule_key: "", trigger_description: "", adjustment_type: "pct", adjustment_value: "" });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggle = async (rule: Rule) => {
    await api.patch(`/admin/rules/${rule.id}`, {
      rule_key: rule.rule_key,
      trigger_description: rule.trigger_description,
      adjustment_type: rule.adjustment_type,
      adjustment_value: rule.adjustment_value,
      active: !rule.active,
    });
    await load();
  };

  return (
    <>
      <ErrorBanner error={error} />
      <Card title="Adjustment rules" hint="the only percentages the system may apply">
        <table>
          <thead>
            <tr>
              <th>Key</th><th>When it applies</th><th>Type</th>
              <th className="num">Value</th><th>Origin</th><th></th>
            </tr>
          </thead>
          <tbody>
            {rules.map((rule) => (
              <tr key={rule.id}>
                <td><code>{rule.rule_key}</code></td>
                <td>{rule.trigger_description ?? <span className="muted">not described</span>}</td>
                <td>{rule.adjustment_type}</td>
                <td className="num">
                  {rule.adjustment_type === "pct" ? `${Number(rule.adjustment_value)}%` : rule.adjustment_value}
                </td>
                <td>
                  {rule.promoted_from_note_id ? (
                    <span className="badge badge-info" title={`Promoted by ${rule.promoted_by}`}>
                      promoted from a note
                    </span>
                  ) : (
                    <span className="muted">entered directly</span>
                  )}
                </td>
                <td>
                  <button onClick={() => void toggle(rule)}>
                    {rule.active ? "Deactivate" : "Activate"}
                  </button>
                </td>
              </tr>
            ))}
            {rules.length === 0 && (
              <tr><td colSpan={6} className="empty">No rules defined.</td></tr>
            )}
          </tbody>
        </table>
      </Card>

      <Card title="Add a rule">
        <div className="grid-2">
          <div>
            <div className="field">
              <label htmlFor="key">Rule key</label>
              <input id="key" value={form.rule_key} onChange={(e) => setForm({ ...form, rule_key: e.target.value })}
                     placeholder="rush_uplift" />
            </div>
            <div className="field">
              <label htmlFor="desc">When it applies</label>
              <input id="desc" value={form.trigger_description}
                     onChange={(e) => setForm({ ...form, trigger_description: e.target.value })}
                     placeholder="Delivery inside 5 working days" />
            </div>
          </div>
          <div>
            <div className="field">
              <label htmlFor="type">Type</label>
              <select id="type" value={form.adjustment_type}
                      onChange={(e) => setForm({ ...form, adjustment_type: e.target.value })}>
                <option value="pct">Percentage</option>
                <option value="fixed">Fixed amount</option>
                <option value="flag_only">Flag only (no price change)</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="value">Value</label>
              <input id="value" value={form.adjustment_value} inputMode="decimal"
                     onChange={(e) => setForm({ ...form, adjustment_value: e.target.value })} placeholder="15" />
            </div>
          </div>
        </div>
        <button className="primary" onClick={() => void submit()} disabled={!form.rule_key}>Add rule</button>
      </Card>

      <Card
        title="Recurring notes"
        hint="candidates for promotion — a review decision, never automatic"
      >
        {candidates.length === 0 ? (
          <p className="muted">
            No note has recurred often enough to suggest a standing rule.
          </p>
        ) : (
          <table>
            <thead><tr><th>Note</th><th className="num">Times seen</th></tr></thead>
            <tbody>
              {candidates.map((c) => (
                <tr key={c.summary}>
                  <td>{titleCase(c.summary)}</td>
                  <td className="num">{c.occurrences}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
