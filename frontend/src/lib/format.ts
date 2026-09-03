/** Formatting helpers. Money arrives as a decimal string and stays one. */

export function money(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return value;
  return parsed.toLocaleString("en-GB", {
    style: "currency",
    currency: "GBP",
    minimumFractionDigits: 2,
  });
}

export function minutes(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const total = Number(value);
  if (Number.isNaN(total)) return value;
  if (total < 60) return `${total.toFixed(total % 1 === 0 ? 0 : 1)}m`;
  const hours = Math.floor(total / 60);
  const rest = Math.round(total % 60);
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

export function age(hours: number): string {
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 48) return `${Math.round(hours)} h`;
  return `${Math.round(hours / 24)} d`;
}

export function processLabel(process: string): string {
  const labels: Record<string, string> = {
    cnc_mill: "CNC mill",
    cnc_turn: "CNC turn",
    wire_edm: "Wire EDM",
    spark_erode: "Spark erode",
    grind: "Grind",
    manual: "Manual",
    qc: "Inspection",
    subcontract: "Subcontract",
  };
  return labels[process] ?? process.replace(/_/g, " ");
}

export function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
