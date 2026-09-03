/** Response shapes, mirroring backend/app/schemas.py. */

export type TimeSource = "calculator" | "historical_estimate" | "manual";
export type FlagSeverity = "info" | "warn" | "block";
export type JobType = "service_only" | "full_supply" | "ambiguous";

export interface Flag {
  id: number;
  category: string;
  severity: FlagSeverity;
  message: string;
  field_name: string | null;
  part_id: number | null;
  quote_id: number | null;
  enquiry_id: number | null;
  related_quote_id: number | null;
  related_enquiry_id: number | null;
  resolved: boolean;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  created_at: string;
}

export interface Operation {
  id: number;
  op_number: number;
  process: string;
  description: string | null;
  set_time_mins: string;
  run_time_mins_per_unit: string;
  hourly_rate: string | null;
  subcontract_unit_cost: string | null;
  computed_cost: string | null;
  time_source: TimeSource;
  rate_table_id: number | null;
}

export interface MaterialRequirement {
  id: number;
  spec: string | null;
  stock_form: string | null;
  stock_size: string | null;
  qty_required: string;
  unit_cost: string;
  blanks_per_unit_stock: number | null;
  utilisation_pct: string | null;
  total_cost: string | null;
}

export interface Part {
  id: number;
  attachment_id: number | null;
  drawing_number: string | null;
  revision: string | null;
  description: string | null;
  /** null means nobody has stated one. It is never defaulted to 1. */
  quantity: number | null;
  quantity_source: string | null;
  material: string | null;
  heat_treatment: string | null;
  surface_coat: string | null;
  finish_spec: string | null;
  envelope_x: string | null;
  envelope_y: string | null;
  envelope_z: string | null;
  tightest_tolerance: string | null;
  features: Record<string, unknown> | null;
  job_type: JobType;
  extraction_confidence: Record<string, number> | null;
  /** Read but too uncertain to price. Shown as unread, offered for confirmation. */
  withheld_fields: Record<string, unknown> | null;
  process_mix: string[] | null;
  process_mix_constrained: boolean;
  operations: Operation[];
  material_requirements: MaterialRequirement[];
  flags: Flag[];
}

export interface Attachment {
  id: number;
  filename: string;
  mime_type: string | null;
  kind: string;
  drawing_number: string | null;
  revision: string | null;
  page_count: number | null;
  size_bytes: number | null;
}

export interface QuoteLine {
  id: number;
  part_id: number | null;
  drawing_number: string | null;
  revision: string | null;
  description: string | null;
  quantity: number;
  unit_price: string;
  line_total: string;
}

export interface QuoteNote {
  id: number;
  author: string;
  note_text: string;
  created_at: string;
  note_kind: string | null;
  adjustment_summary: string | null;
  price_before: string | null;
  price_after: string | null;
  applied_rule_id: number | null;
  proposed_change: { applied?: unknown[]; rejected?: { action: unknown; reason: string }[] } | null;
  awaiting_answer: boolean;
  question: string | null;
}

export interface Adjustment {
  rule_id: number | null;
  rule_key: string;
  adjustment_type: string;
  adjustment_value: string;
  effect: string;
  description: string | null;
}

export interface Quote {
  id: number;
  version: number;
  status: string;
  material_total: string;
  labour_total: string;
  subtotal: string;
  margin_pct: string;
  margin_value: string;
  quote_value: string;
  adjustments: Adjustment[] | null;
  applied_rule_ids: number[] | null;
  min_value_applied: boolean;
  lead_time_days: number | null;
  approved_by: string | null;
  approved_at: string | null;
  sent_at: string | null;
  outlook_draft_id: string | null;
  lines: QuoteLine[];
  notes: QuoteNote[];
  flags: Flag[];
  outcome: { result: string; actual_production_mins: string | null } | null;
}

export interface Customer {
  id: number;
  name: string;
  domain: string | null;
  default_margin_pct: string;
  default_lead_days: number;
  is_material_supplied_default: boolean;
  requires_cert: boolean;
  notes: string | null;
}

export interface OperationCost {
  op_number: number;
  process: string;
  time_source: TimeSource;
  total_mins: string;
  hourly_rate: string | null;
  computed_cost: string;
  is_subcontract: boolean;
}

export interface PartPrice {
  part_id: number | null;
  quantity: number;
  labour_total: string;
  material_total: string;
  subtotal: string;
  margin_value: string;
  value: string;
  unit_price: string;
  line_total: string;
  uses_untrusted_times: boolean;
  operation_costs: OperationCost[];
}

export interface Breakdown {
  labour_total: string;
  material_total: string;
  subtotal: string;
  margin_pct: string;
  margin_value: string;
  quote_value: string;
  rounding_adjustment: string;
  min_value_applied: boolean;
  uses_untrusted_times: boolean;
  reconciles: boolean;
  adjustments: Adjustment[];
  parts: PartPrice[];
}

export interface Enquiry {
  id: number;
  customer_id: number | null;
  customer: Customer | null;
  subject: string | null;
  body_text: string | null;
  sender_email: string | null;
  received_at: string | null;
  status: string;
  customer_reference: string | null;
  anchor_quote_id: number | null;
  due_date: string | null;
  turnaround_seconds: number | null;
  error_detail: string | null;
  attachments: Attachment[];
  parts: Part[];
  quotes: Quote[];
}

export interface Workspace {
  enquiry: Enquiry;
  current_quote: Quote | null;
  breakdown: Breakdown | null;
  enquiry_flags: Flag[];
  blocking_flag_count: number;
  can_approve: boolean;
  ambiguous_paths: Record<number, Record<string, PartPrice>>;
}

export interface QueueItem {
  enquiry_id: number;
  customer_name: string | null;
  subject: string | null;
  status: string;
  received_at: string | null;
  age_hours: number;
  part_count: number;
  job_types: string[];
  process_mix: string[];
  total_quantity: number;
  quote_id: number | null;
  quote_value: string | null;
  flag_count: number;
  blocking_flag_count: number;
  lowest_confidence: number | null;
  due_date: string | null;
}

export interface Rate {
  id: number;
  process: string;
  machine_group: string | null;
  hourly_rate: string;
  effective_from: string;
  effective_to: string | null;
}

export interface Rule {
  id: number;
  rule_key: string;
  trigger_description: string | null;
  adjustment_type: string;
  adjustment_value: string;
  active: boolean;
  promoted_from_note_id: number | null;
  promoted_by: string | null;
  last_reviewed_at: string | null;
}

export interface Match {
  part_id: number;
  quote_id: number | null;
  enquiry_id: number;
  drawing_number: string | null;
  revision: string | null;
  description: string | null;
  quantity: number | null;
  quote_value: string | null;
  unit_price: string | null;
  score: number;
  reasons: string[];
  result: string | null;
  actual_production_mins: string | null;
}

export interface SimilarResponse {
  geometry: Match[];
  problem: Match[];
}
