import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../services/api";
import type { ContainerInput } from "../types/api";

// The worked example from scripts/start_workflow.py: MSKU 748192-0,
// Ningbo -> USLAX -> Fontana. Defaulting the form to it means "Start" here
// reproduces the same run the CLI driver produces.
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

const FIELD_LABELS: { key: keyof ContainerInput; label: string }[] = [
  { key: "container_id", label: "Container ID" },
  { key: "contract_id", label: "Contract ID" },
  { key: "port", label: "Port" },
  { key: "terminal", label: "Terminal" },
  { key: "carrier", label: "Carrier" },
  { key: "consignee", label: "Consignee" },
  { key: "return_depot", label: "Return depot" },
  { key: "bill_of_lading", label: "Bill of lading" },
];

export function StartContainerForm() {
  const [open, setOpen] = useState(false);
  const [container, setContainer] = useState<ContainerInput>(DEMO_CONTAINER);
  const [fresh, setFresh] = useState(false);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const mutation = useMutation({
    mutationFn: () =>
      api.startContainer({
        container,
        contract_text: SAMPLE_CONTRACT,
        auto_approve_limit_usd: "250",
        slot_scarcity_prior: 0.8,
        depot_restriction_prior: 0.6,
        fresh,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["fleet"] });
      navigate(`/container/${container.container_id}`);
    },
  });

  if (!open) {
    return (
      <button className="btn primary" onClick={() => setOpen(true)}>
        + Start container
      </button>
    );
  }

  return (
    <div className="card">
      <h3>Start a container</h3>
      <div className="form-row">
        {FIELD_LABELS.map(({ key, label }) => (
          <div className="field" key={key}>
            <label>{label}</label>
            <input
              type="text"
              value={container[key]}
              onChange={(e) => setContainer({ ...container, [key]: e.target.value })}
            />
          </div>
        ))}
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="fresh"
          checked={fresh}
          onChange={(e) => setFresh(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="fresh" style={{ marginBottom: 0 }}>
          Fresh (terminate any previous run for this container id first)
        </label>
      </div>
      <div style={{ display: "flex", gap: 10 }}>
        <button className="btn primary" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
          {mutation.isPending ? "Starting…" : "Start"}
        </button>
        <button className="btn" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
      {mutation.isError && (
        <p className="small" style={{ color: "var(--red)", marginTop: 8 }}>
          {String(mutation.error)}
        </p>
      )}
    </div>
  );
}
