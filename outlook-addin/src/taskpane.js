/**
 * Outlook triage card (spec section 4).
 *
 * Deliberately read-only. It answers one question — is this enquiry routine or
 * does it need attention — and hands off to the workspace for everything else.
 * No pricing, no approval, no send.
 */

/** Where the API and web app live. Set at deploy time. */
const API_BASE = "https://quoting.example.internal/api";
const APP_BASE = "https://quoting.example.internal";

const $ = (id) => document.getElementById(id);

Office.onReady(() => {
  const item = Office.context.mailbox.item;
  if (!item) {
    showState("No message selected.");
    return;
  }
  loadTriage(item.itemId).catch((error) => {
    showState(`Could not load the quote: ${error.message}`);
  });
});

function showState(message) {
  $("state").textContent = message;
  $("state").hidden = false;
  $("summary").hidden = true;
}

async function loadTriage(outlookMessageId) {
  const lookup = await fetch(
    `${API_BASE}/enquiries/by-message/${encodeURIComponent(outlookMessageId)}`,
    { headers: { Accept: "application/json" }, credentials: "include" },
  );
  if (lookup.status === 404) {
    showState(
      "This message has not been picked up for quoting yet. Tag it for the " +
        "quoting mailbox and it will appear here shortly.",
    );
    return;
  }
  if (!lookup.ok) throw new Error(`API returned ${lookup.status}`);

  render(await lookup.json());
}

function render(item) {
  $("state").hidden = true;
  $("summary").hidden = false;

  $("customer").textContent = item.customer_name ?? "Unrecognised sender";
  $("subject").textContent = item.subject ?? "";
  $("status").innerHTML = badge(
    titleCase(item.status),
    item.status === "needs_attention" || item.status === "failed"
      ? "block"
      : item.status === "sent" || item.status === "approved"
        ? "ok"
        : "muted",
  );
  $("parts").textContent = String(item.part_count);
  $("jobType").textContent = item.job_types.map(titleCase).join(", ") || "—";
  $("process").textContent = item.process_mix.map(titleCase).join(", ") || "—";
  $("quantity").textContent = item.total_quantity || "not stated";
  $("due").textContent = item.due_date ?? "not stated";
  $("value").textContent = item.quote_value ? `£${Number(item.quote_value).toFixed(2)}` : "not priced";

  // Flags as one line, as the spec asks — the detail lives in the workspace.
  $("flags").innerHTML =
    item.blocking_flag_count > 0
      ? badge(`${item.blocking_flag_count} blocking`, "block") +
        (item.flag_count > item.blocking_flag_count
          ? ` ${badge(`${item.flag_count - item.blocking_flag_count} other`, "muted")}`
          : "")
      : item.flag_count > 0
        ? badge(`${item.flag_count} to review`, "muted")
        : badge("none", "ok");

  $("open").href = `${APP_BASE}/enquiry/${item.enquiry_id}`;
}

function badge(text, tone) {
  return `<span class="badge badge-${tone}">${escapeHtml(text)}</span>`;
}

function titleCase(value) {
  return String(value).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}
