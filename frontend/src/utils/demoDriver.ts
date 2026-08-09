import { api } from "../services/api";
import type { ContainerInput, PendingApproval } from "../types/api";
import { SIGNAL_DEFAULTS } from "./signalDefaults";

const DEMO_CONTAINER: ContainerInput = {
  container_id: "MSKU7481920",
  contract_id: "SC-2026-0042",
  port: "USLAX",
  terminal: "Pier400",
  carrier: "Maersk",
  consignee: "Acme Garden Retail",
  return_depot: "Fontana Empty Depot",
  bill_of_lading: "MAEU123456789",
};

// A separate container id from the main journey demo, so forcing an approval
// doesn't reset (or get reset by) whichever run someone is looking at.
const APPROVAL_DEMO_CONTAINER: ContainerInput = {
  ...DEMO_CONTAINER,
  container_id: "MSKUAPR00001",
};

const SAMPLE_CONTRACT = `SERVICE CONTRACT SC-2026-0042 - PORT OF LOS ANGELES / LONG BEACH
SECTION 7. FREE TIME AND DEMURRAGE
7.1 Merchant is allowed four (4) calendar days of free time for import
    demurrage, counted from the day after discharge and availability.
7.2 After free time, demurrage accrues per container per day: days 1-3 at
    $200, days 4-6 at $325, day 7 onward at $450.
SECTION 8. DETENTION
8.1 Five (5) calendar days of free time on equipment after gate-out.
8.2 Detention accrues at $150/day for days 1-3, $225/day thereafter.
8.3 Chassis usage is billed at $45/day with no free time.`;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(
  check: () => Promise<boolean>,
  label: string,
  timeoutMs = 90_000,
  throwOnTimeout = true
): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await check()) return true;
    await sleep(1000);
  }
  if (throwOnTimeout) throw new Error(`timed out waiting for ${label}`);
  return false;
}

/** Reproduces scripts/start_workflow.py's drive() sequence from the browser -
 * same container, same fixture dates, same signal order - so the full 45-day
 * story is one click instead of a terminal. */
export async function driveDemo(onProgress: (msg: string) => void, stepMs = 800): Promise<void> {
  const cid = DEMO_CONTAINER.container_id;
  const pause = () => sleep(stepMs);

  onProgress(`Starting ${cid} (fresh)…`);
  await api.startContainer({
    container: DEMO_CONTAINER,
    contract_text: SAMPLE_CONTRACT,
    auto_approve_limit_usd: "250",
    slot_scarcity_prior: 0.8,
    depot_restriction_prior: 0.6,
    fresh: true,
  });
  await pause();

  onProgress("D4  customs_entry_filed");
  await api.signalContainer(cid, "customs_entry_filed", SIGNAL_DEFAULTS.customs_entry_filed as Record<string, unknown>);
  await pause();

  onProgress("D8  discharged -> spawns DemurrageArc");
  await api.signalContainer(cid, "discharged", SIGNAL_DEFAULTS.discharged as Record<string, unknown>);
  await pause();

  onProgress("waiting for DemurrageArc to spawn…");
  await waitFor(async () => (await api.arcs(cid)).demurrage !== null, "demurrage arc");

  onProgress("D9  hold_placed (customs exam) -> intervention suppressed");
  await api.signalArc(cid, "demurrage", "hold_placed", SIGNAL_DEFAULTS.hold_placed as Record<string, unknown>);
  await pause();

  onProgress("D10 container_available (late) -> effective LFD shifts");
  await api.signalArc(cid, "demurrage", "container_available", SIGNAL_DEFAULTS.container_available as Record<string, unknown>);
  await pause();

  onProgress("D17 hold_released -> re-rank, resume action");
  await api.signalArc(cid, "demurrage", "hold_released", SIGNAL_DEFAULTS.hold_released as Record<string, unknown>);
  await pause();

  onProgress("D20 gate_out -> DemurrageArc closes, DetentionArc spawns");
  await api.signalArc(cid, "demurrage", "gate_out", SIGNAL_DEFAULTS.gate_out as Record<string, unknown>);
  await pause();

  onProgress("waiting for DetentionArc to spawn…");
  await waitFor(async () => (await api.arcs(cid)).detention !== null, "detention arc");

  onProgress("D22 cargo_stripped");
  await api.signalArc(cid, "detention", "cargo_stripped", SIGNAL_DEFAULTS.cargo_stripped as Record<string, unknown>);
  await pause();

  onProgress("D23 empty_returned -> DetentionArc closes ($1,530 prevented)");
  await api.signalArc(cid, "detention", "empty_returned", SIGNAL_DEFAULTS.empty_returned as Record<string, unknown>);
  await pause();

  onProgress("D40 invoice_received (non-compliant) -> spawns DisputeArc");
  await api.signalContainer(cid, "invoice_received", SIGNAL_DEFAULTS.invoice_received as Record<string, unknown>);

  onProgress("waiting for DisputeArc to spawn…");
  await waitFor(async () => {
    try {
      await api.dispute("INV-DEM-88431");
      return true;
    } catch {
      return false;
    }
  }, "dispute arc");
  await pause();

  // Leak 07: cargo released only on payment, with the intent to dispute held
  // as workflow state rather than lost in somebody's inbox.
  onProgress("D43 paid_under_protest -> protest carried forward as state");
  await api.signalContainer(cid, "paid_under_protest", SIGNAL_DEFAULTS.paid_under_protest as Record<string, unknown>);
  await pause();

  // The tail (runbook days 46-90). This is where the Temporal argument is
  // strongest: the box went back on day 23 and the money arrives on day 65,
  // six weeks after the physical event no session-based system outlives.
  onProgress("D64 carrier_replied - offers $1,534.50, only 62% of the $2,475 claim");
  await api.signalDispute("INV-DEM-88431", "carrier_replied", {
    message: "Without prejudice, we offer partial settlement.",
    offer_usd: "1534.50",
  });

  onProgress("below the 70% mandate -> the agent may not accept alone; escalating…");
  const parked = await waitFor(
    async () => (await api.disputePendingApproval("INV-DEM-88431")) !== null,
    "settlement escalation",
    60_000,
    false
  );

  if (parked) {
    onProgress("D65 parked on a human. Accept it from the Approvals inbox to book the recovery.");
  } else {
    onProgress("Settlement did not escalate within 60s - check the worker is running.");
  }

  onProgress("Done. Full lifecycle reproduced, through to the settlement gate.");
}

async function waitForApproval(cid: string, timeoutMs: number): Promise<PendingApproval | null> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const pending = await api.pendingApproval(cid, "demurrage");
    if (pending) return pending;
    await sleep(1500);
  }
  return null;
}

/** Forces a real notify_human gate: no hold (so intervention isn't
 * suppressed) and a spend cap low enough that the checkpoint's cheapest real
 * option still needs a human. Requires the worker to be running with
 * PF_DEMO_MODE=1 - otherwise the checkpoint is anchored to a real calendar
 * date and this can take up to ~24h, which this function will not wait for. */
export async function forceApproval(onProgress: (msg: string) => void): Promise<PendingApproval | null> {
  const cid = APPROVAL_DEMO_CONTAINER.container_id;

  onProgress(`Starting ${cid} fresh - no hold, $50 spend cap…`);
  await api.startContainer({
    container: APPROVAL_DEMO_CONTAINER,
    contract_text: SAMPLE_CONTRACT,
    auto_approve_limit_usd: "50",
    slot_scarcity_prior: 0.8,
    depot_restriction_prior: 0.6,
    fresh: true,
  });

  onProgress("discharged -> spawns DemurrageArc");
  await api.signalContainer(cid, "discharged", SIGNAL_DEFAULTS.discharged as Record<string, unknown>);

  onProgress("waiting for DemurrageArc to spawn…");
  await waitFor(async () => (await api.arcs(cid)).demurrage !== null, "demurrage arc");

  onProgress("container_available (on time, no hold placed) -> checkpoint loop starts");
  await api.signalArc(cid, "demurrage", "container_available", SIGNAL_DEFAULTS.container_available as Record<string, unknown>);

  onProgress("waiting for the first checkpoint to price an option above the cap…");
  const pending = await waitForApproval(cid, 60_000);
  if (pending) {
    onProgress(`Parked: ${pending.action} ($${pending.cost_usd}) - ${pending.reason}. Approve it below.`);
  } else {
    onProgress(
      "No approval appeared within 60s. This requires the worker started with PF_DEMO_MODE=1 - " +
        "without it, the checkpoint is anchored to a real calendar date and could be hours away."
    );
  }
  return pending;
}

export { APPROVAL_DEMO_CONTAINER };
