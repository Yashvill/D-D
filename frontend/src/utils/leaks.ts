import type { ContainerArcs, EvidenceRow } from "../types/api";

/** The seven cash leaks from the runbook (PF-003 §11), attributed to live
 * workflow state rather than asserted.
 *
 * Every verdict below is derived from a field the workflows already expose -
 * a shifted LFD, a suppressed intervention, a count of appointment failures -
 * so the ledger says what the agent can actually evidence, and says "not yet"
 * when it cannot. That distinction is the whole point: a leak panel that
 * always shows seven greens is marketing, not a control tower. */

export type LeakStatus = "prevented" | "contested" | "closed" | "exposed" | "pending";

export interface LeakRow {
  id: string;
  name: string;
  mechanism: string;
  status: LeakStatus;
  detail: string;
  citations: string[];
}

export const LEAK_STATUS_LABEL: Record<LeakStatus, string> = {
  prevented: "Prevented",
  contested: "Contested",
  closed: "Closed",
  exposed: "Exposed",
  pending: "Not yet",
};

function citationsFor(evidence: EvidenceRow[], kinds: string[]): string[] {
  return evidence.filter((e) => kinds.includes(e.kind)).map((e) => e.source_document_id);
}

export function attributeLeaks(arcs: ContainerArcs, evidence: EvidenceRow[]): LeakRow[] {
  const c = arcs.container.state;
  const dem = arcs.demurrage?.state;
  const det = arcs.detention?.state;
  const disputes = arcs.disputes;

  const rows: LeakRow[] = [];

  // 01 - clock started before you could act
  const shifted = dem?.lfd_shifted ?? (!!c.nominal_lfd && !!c.effective_lfd && c.nominal_lfd !== c.effective_lfd);
  rows.push({
    id: "01",
    name: "Clock started before you could act",
    mechanism: "Free days consumed while the box was ungrounded, pushing the delay deeper into the tier ladder",
    status: shifted ? "contested" : c.effective_lfd ? "closed" : "pending",
    detail: shifted
      ? `Effective LFD shifted ${c.nominal_lfd} → ${c.effective_lfd}`
      : c.effective_lfd
        ? "No ungrounded gap measured; nominal LFD stands"
        : "Awaiting availability",
    citations: citationsFor(evidence, ["availability_miss", "container_available"]),
  });

  // 02 - billed while forbidden to move
  const hadHold = evidence.some((e) => e.kind === "hold_placed");
  rows.push({
    id: "02",
    name: "Billed while forbidden to move",
    mechanism: "Demurrage days accrued during a customs exam, when no party could lawfully collect",
    status: hadHold ? "contested" : "pending",
    detail: hadHold
      ? dem?.intervention_suppressed
        ? "Hold recorded; intervention suppressed rather than spending to look busy"
        : "Hold window recorded and cited"
      : "No hold recorded",
    citations: citationsFor(evidence, ["hold_placed", "hold_released"]),
  });

  // 03 - terminal capacity failure
  const failures = dem?.appointment_failures ?? 0;
  rows.push({
    id: "03",
    name: "Terminal capacity failure",
    mechanism: "Top-tier days burned with no appointment slot offered - the billing party's own failure",
    status: failures > 0 ? "contested" : "pending",
    detail: failures > 0 ? `${failures} appointment failure(s), each timestamped as it happened` : "No failed scans recorded",
    citations: citationsFor(evidence, ["appointment_unavailable"]).slice(0, 6),
  });

  // 04 - detention on a refused empty
  const prevented = Number(det?.prevented_usd ?? c.total_prevented_usd ?? 0);
  rows.push({
    id: "04",
    name: "Detention on a refused empty",
    mechanism: "Detention and chassis days accrued purely because nobody watched the second clock",
    status: det?.empty_returned_at ? (prevented > 0 ? "prevented" : "closed") : det ? "exposed" : "pending",
    detail: det?.empty_returned_at
      ? `Empty returned ${det.empty_returned_at.slice(0, 10)}, ${det.detention_days} detention day(s) accrued` +
        (det.near_miss ? " · restriction landed after the box was already back" : "")
      : det
        ? "Equipment clock running; empty not yet returned"
        : "Detention arc not started",
    citations: citationsFor(evidence, ["empty_returned", "carrier_advisory", "near_miss"]),
  });

  // 05 - deadline never known
  rows.push({
    id: "05",
    name: "Deadline never known",
    mechanism: "Multiple allowances expiring on different days, none of them triggering anything",
    status: c.terms_loaded && c.effective_lfd ? "closed" : c.terms_loaded ? "pending" : "exposed",
    detail: c.terms_loaded
      ? `Contract terms extracted (confidence ${(c.terms_confidence * 100).toFixed(0)}%)` +
        (c.effective_lfd ? "; both LFDs tracked from day one" : "; awaiting discharge")
      : "Contract terms not loaded",
    citations: [],
  });

  // 06 - dispute window closed first
  const filed = disputes.filter((d) => d.state.filed);
  rows.push({
    id: "06",
    name: "Dispute window closed first",
    mechanism: "Invoices arrive weeks late into a 30-day window nobody starts",
    status: filed.length > 0 ? "closed" : disputes.length > 0 ? "exposed" : "pending",
    detail:
      filed.length > 0
        ? `${filed.length} dispute(s) filed with evidence already attached`
        : disputes.length > 0
          ? "Dispute open, not yet filed"
          : "No invoice received yet",
    citations: filed.map((d) => d.state.case_ref).filter(Boolean),
  });

  // 07 - paid under duress, never revisited
  rows.push({
    id: "07",
    name: "Paid under duress, never revisited",
    mechanism: "Charges paid to release cargo, with the intent to dispute carried by nothing",
    status: c.protest_held ? "closed" : "pending",
    detail: c.protest_held
      ? "Protest held as workflow state; recovery still being prosecuted"
      : "No payment under protest recorded",
    citations: citationsFor(evidence, []),
  });

  return rows;
}

/** Which of the runbook's five phases this container is currently in. */
export function currentPhase(arcs: ContainerArcs): { n: number; label: string; range: string } {
  const c = arcs.container.state;
  if (c.disputes.length > 0) return { n: 4, label: "Dispute", range: "D32-45+" };
  if (c.empty_returned_at) return { n: 3, label: "Post-return", range: "D24-31" };
  if (c.gated_out_at) return { n: 3, label: "Detention", range: "D20-31" };
  if (c.discharged_at) return { n: 2, label: "Demurrage", range: "D8-20" };
  if (c.terms_loaded) return { n: 0, label: "Pre-arrival", range: "D1-7" };
  return { n: 0, label: "Starting", range: "D1" };
}
