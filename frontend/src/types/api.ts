/** Mirrors of the dicts returned by the workflow state()/query methods in
 * temporal_backend/workflows/*.py. Money and dates travel as strings - the
 * backend already renders Decimal/datetime to str()/isoformat() before they
 * cross the HTTP boundary, so no parsing happens here beyond display. */

export type RiskLevel = "PENDING" | "GREEN" | "YELLOW" | "RED" | "CLOSED" | "DISPUTED";

export interface ContainerState {
  risk: RiskLevel;
  terms_loaded: boolean;
  terms_confidence: number;
  discharged_at: string | null;
  gated_out_at: string | null;
  empty_returned_at: string | null;
  effective_lfd: string | null;
  nominal_lfd: string | null;
  evidence_count: number;
  demurrage_days: number;
  detention_days: number;
  spend_usd: string;
  total_billed_usd: string;
  total_prevented_usd: string;
  total_contested_usd: string;
  disputes: string[];
  protest_held: boolean;
}

export interface DemurrageState {
  risk: RiskLevel;
  reason: string;
  effective_lfd: string | null;
  nominal_lfd: string | null;
  lfd_shifted: boolean;
  holds: string[];
  intervention_suppressed: boolean;
  spend_usd: string;
  appointment_failures: number;
  evidence_count: number;
  counterfactual_usd: string;
  gated_out_at: string | null;
}

export interface DetentionState {
  risk: RiskLevel;
  return_slot: string | null;
  cargo_stripped_at: string | null;
  empty_returned_at: string | null;
  detention_days: number;
  prevented_usd: string;
  spend_usd: string;
  near_miss: boolean;
  restriction_matched: boolean;
  evidence_count: number;
}

export interface DisputeState {
  invoice_id: string;
  filed: boolean;
  filed_at: string | null;
  case_ref: string;
  findings: string[];
  voids_entire_charge: boolean;
  amount_contested_usd: string;
  claims: number;
  dropped_claims: string[];
  follow_ups_sent: number;
  offer_usd: string | null;
  recovered_usd: string;
  escalated: boolean;
  outcome: string;
}

export interface EvidenceRow {
  kind: string;
  occurred_at: string;
  source_document_id: string;
  summary: string;
}

export interface LetterDraft {
  invoice_id: string;
  container_id: string;
  subject: string;
  body: string;
  amount_contested_usd: string;
  citations: string[];
  drafted_at: string;
}

export interface PendingApproval {
  action: string;
  cost_usd: string;
  reason: string;
  detail: string;
  requested_at: string | null;
}

export interface ArcSummary<TState> {
  workflow_id: string;
  status: string | null;
  state: TState;
  pending_approval: PendingApproval | null;
}

export interface DisputeSummary {
  workflow_id: string;
  invoice_id: string;
  status: string | null;
  state: DisputeState;
  letter: LetterDraft | null;
  pending_approval: PendingApproval | null;
}

export interface ContainerArcs {
  container: {
    workflow_id: string;
    status: string | null;
    state: ContainerState;
  };
  demurrage: ArcSummary<DemurrageState> | null;
  detention: ArcSummary<DetentionState> | null;
  disputes: DisputeSummary[];
}

export interface FleetEntry {
  workflow_id: string;
  container_id: string;
  status: string | null;
  started_at: string | null;
  state: ContainerState | null;
}

export interface HealthStatus {
  server: boolean;
  workers: number;
  task_queue: string;
  readonly: boolean;
  error?: string;
}

export interface MetaSignals {
  container: string[];
  demurrage: string[];
  detention: string[];
  dispute: string[];
  readonly: boolean;
}

/** Which arc a human gate belongs to. Disputes gate on `approve_settlement`
 * rather than `approve`, so the target matters when sending the signal. */
export type ApprovalArc = "demurrage" | "detention" | "dispute";

export interface FleetApproval extends PendingApproval {
  workflow_id: string;
  container_id: string;
  arc: ApprovalArc;
  /** Present only for dispute approvals. */
  invoice_id?: string;
}

export interface DisputeDetail {
  invoice_id: string;
  workflow_id: string;
  status: string | null;
  state: DisputeState;
  findings: string[];
  letter: LetterDraft | null;
}

/** The eight required fields of agents.shared.models.ContainerInput. */
export interface ContainerInput {
  container_id: string;
  contract_id: string;
  port: string;
  terminal: string;
  carrier: string;
  consignee: string;
  return_depot: string;
  bill_of_lading: string;
}
