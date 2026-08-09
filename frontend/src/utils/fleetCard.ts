import type { FleetEntry } from "../types/api";
import { formatUsd } from "./format";

/** Maps live workflow/state onto the same five-state vocabulary index.html's
 * illustrative mockup used (sleeping/watching/acting/disputing/recovered), but
 * derived from the real ContainerWorkflow.state() dict instead of a random
 * generator. */
export function cardState(entry: FleetEntry): { key: string; label: string } {
  const s = entry.state;
  if (!s) return { key: "st-sleep", label: "Unknown" };

  // Execution status wins over risk. A terminated or failed run is not
  // "watching" anything - labelling it from risk alone would show a dead
  // workflow as though it were still on duty, which is exactly the kind of
  // reassuring-but-false readout this console exists to avoid.
  if (entry.status && entry.status !== "RUNNING") {
    if (entry.status === "FAILED") return { key: "st-dispute", label: "Failed" };
    if (Number(s.total_prevented_usd) > 0) return { key: "st-won", label: "Closed" };
    return { key: "st-sleep", label: entry.status === "COMPLETED" ? "Completed" : "Stopped" };
  }

  if (s.disputes.length > 0) return { key: "st-dispute", label: "Disputing" };
  if (s.risk === "RED") return { key: "st-act", label: "Acting" };
  if (s.risk === "GREEN" || s.risk === "YELLOW") return { key: "st-watch", label: "Watching" };
  if (s.risk === "PENDING") return { key: "st-sleep", label: s.discharged_at ? "Watching" : "Sleeping" };
  return { key: "st-sleep", label: "Sleeping" };
}

export function phaseLine(entry: FleetEntry): string {
  const s = entry.state;
  if (!s) return "state unavailable";
  if (s.disputes.length > 0) {
    return `${s.disputes.length} dispute${s.disputes.length > 1 ? "s" : ""} filed · ${formatUsd(s.total_contested_usd)} contested`;
  }
  if (s.effective_lfd && s.effective_lfd !== s.nominal_lfd) {
    return `LFD shifted ${s.nominal_lfd} → ${s.effective_lfd}`;
  }
  if (s.demurrage_days > 0) return `${s.demurrage_days} demurrage day(s) accrued`;
  if (s.detention_days > 0) return `${s.detention_days} detention day(s) accrued`;
  return `${s.evidence_count} evidence item${s.evidence_count === 1 ? "" : "s"} logged`;
}

/** Coarse phase label for a fleet card, from the same timestamps the detail
 * page's PhaseStrip uses. */
export function phaseLabel(entry: FleetEntry): string {
  const s = entry.state;
  if (!s) return "—";
  if (s.disputes.length > 0) return "Dispute · D32-45";
  if (s.empty_returned_at) return "Returned · D24-31";
  if (s.gated_out_at) return "Detention · D20-31";
  if (s.discharged_at) return "Demurrage · D8-20";
  if (s.terms_loaded) return "Pre-arrival · D1-7";
  return "Starting";
}

export function footLfd(entry: FleetEntry): string {
  const s = entry.state;
  if (!s) return "—";
  return s.effective_lfd ? `LFD ${s.effective_lfd}` : "LFD pending";
}

export function footAmount(entry: FleetEntry): { text: string; cls: string } {
  const s = entry.state;
  if (!s) return { text: "—", cls: "" };
  if (Number(s.total_prevented_usd) > 0) return { text: `+${formatUsd(s.total_prevented_usd)}`, cls: "green" };
  if (Number(s.total_contested_usd) > 0) return { text: formatUsd(s.total_contested_usd), cls: "red" };
  return { text: "—", cls: "" };
}
