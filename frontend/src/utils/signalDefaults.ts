/** Default payloads mirroring the worked example in scripts/start_workflow.py
 * (MSKU7481920, Ningbo -> USLAX -> Fontana, dated March 2026 - deliberately in
 * the past so every wait_condition gate resolves instantly). Driving these
 * signals in order from the console reproduces the same scripted lifecycle the
 * CLI driver produces, but each one is editable JSON before it's sent. */

const noncompliantInvoice = {
  invoice_id: "INV-DEM-88431",
  charge_type: "demurrage",
  billing_party: "Maersk",
  container_id: "MSKU7481920",
  issued_at: "2026-04-04",
  last_charge_incurred_at: "2026-03-15",
  total_usd: "2475",
  lines: [
    { charge_type: "demurrage", billed_day: 1, charge_date: "2026-03-08", rate_usd: "200", amount_usd: "200" },
    { charge_type: "demurrage", billed_day: 2, charge_date: "2026-03-09", rate_usd: "200", amount_usd: "200" },
    { charge_type: "demurrage", billed_day: 3, charge_date: "2026-03-10", rate_usd: "200", amount_usd: "200" },
    { charge_type: "demurrage", billed_day: 4, charge_date: "2026-03-11", rate_usd: "325", amount_usd: "325" },
    { charge_type: "demurrage", billed_day: 5, charge_date: "2026-03-12", rate_usd: "325", amount_usd: "325" },
    { charge_type: "demurrage", billed_day: 6, charge_date: "2026-03-13", rate_usd: "325", amount_usd: "325" },
    { charge_type: "demurrage", billed_day: 7, charge_date: "2026-03-14", rate_usd: "450", amount_usd: "450" },
    { charge_type: "demurrage", billed_day: 8, charge_date: "2026-03-15", rate_usd: "450", amount_usd: "450" },
  ],
  free_time_claimed_days: 4,
  lfd_claimed: "2026-03-07",
  certification_present: false,
  source_document_id: "invoice::INV-DEM-88431",
};

export const SIGNAL_DEFAULTS: Record<string, unknown> = {
  // container
  customs_entry_filed: { at: "2026-02-27T06:00:00", source_document_id: "ace::entry::SUM-4471" },
  discharged: { at: "2026-03-03T06:00:00", source_document_id: "edi315::VA::MSKU7481920" },
  invoice_received: { invoice: noncompliantInvoice },
  paid_under_protest: { invoice_id: "INV-DEM-88431", at: "2026-04-05T00:00:00" },
  close: {},

  // demurrage
  container_available: { at: "2026-03-05T14:00:00", source_document_id: "terminal::avail::MSKU7481920" },
  hold_placed: {
    hold: {
      hold_type: "customs_exam",
      placed_at: "2026-03-04T09:30:00",
      released_at: null,
      reference: "ace::CET-88213",
    },
  },
  hold_released: {
    hold_type: "customs_exam",
    at: "2026-03-12T16:00:00",
    source_document_id: "ace::CET-88213::rel",
  },
  gate_out: { at: "2026-03-15T14:00:00", source_document_id: "eir::gateout::MSKU7481920" },

  // detention
  cargo_stripped: { at: "2026-03-17T11:00:00", source_document_id: "wms::strip::MSKU7481920" },
  carrier_advisory: {
    advisory_text: "Depot restricted for empties returning after hours",
    source_document_id: "carrier::advisory::001",
    at: "2026-03-16T08:00:00",
  },
  empty_returned: { at: "2026-03-18T09:00:00", source_document_id: "eir::MSKU7481920::D23" },

  // demurrage + detention share this
  approve: { action: "" },

  // dispute
  carrier_replied: { message: "We dispute your dispute.", offer_usd: "500" },
  approve_settlement: { action: "settlement" },
  settled: { amount_usd: "500" },
};

export function defaultBodyFor(name: string): unknown {
  return SIGNAL_DEFAULTS[name] ?? {};
}
