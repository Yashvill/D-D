import { useParams } from "react-router-dom";
import { useContainerArcs, useEvidence, useMetaSignals } from "../hooks/useApi";
import { ErrorState } from "../components/ErrorState";
import { RiskBadge } from "../components/RiskBadge";
import { ArcCard } from "../components/ArcCard";
import { EvidenceTable } from "../components/EvidenceTable";
import { DisputeLetterPanel } from "../components/DisputeLetterPanel";
import { SignalForm } from "../components/SignalForm";
import { LeakLedger } from "../components/LeakLedger";
import { MoneyPanel } from "../components/MoneyPanel";
import { PhaseStrip } from "../components/PhaseStrip";
import { formatDate, formatUsd } from "../utils/format";
import { currentPhase } from "../utils/leaks";

export function ContainerDetail() {
  const { cid = "" } = useParams();
  const { data: arcs, error, isLoading } = useContainerArcs(cid);
  const { data: evidence } = useEvidence(cid);
  const { data: metaSignals } = useMetaSignals();

  if (error) {
    return (
      <div>
        <h2 className="mono">{cid}</h2>
        <ErrorState error={error} />
      </div>
    );
  }
  if (isLoading || !arcs) {
    return <p className="muted small">Loading…</p>;
  }

  const state = arcs.container.state;
  const disputeInvoiceIds = arcs.disputes.map((d) => d.invoice_id);
  const phase = currentPhase(arcs);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
        <h2 className="mono" style={{ fontSize: 20 }}>
          {cid}
        </h2>
        <RiskBadge risk={state.risk} />
        <span className="small mono muted">{arcs.container.status ?? "—"}</span>
        <span className="small muted">
          Phase {phase.n} · {phase.label} · {phase.range}
        </span>
      </div>
      <p className="small muted" style={{ marginBottom: 18 }}>
        {arcs.container.workflow_id}
      </p>

      <PhaseStrip arcs={arcs} />

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <div className="card stat">
          <div className="v sea">{formatUsd(state.spend_usd)}</div>
          <div className="l">Spend</div>
        </div>
        <div className="card stat">
          <div className="v green">{formatUsd(state.total_prevented_usd)}</div>
          <div className="l">Prevented</div>
        </div>
        <div className="card stat">
          <div className="v red">{formatUsd(state.total_contested_usd)}</div>
          <div className="l">Contested</div>
        </div>
        <div className="card stat">
          <div className="v">{state.evidence_count}</div>
          <div className="l">Evidence items</div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>
          Last free day{" "}
          {arcs.demurrage?.state.lfd_shifted && (
            <span className="badge risk-YELLOW" style={{ marginLeft: 6 }}>
              Leak 01 · shifted
            </span>
          )}
        </h3>
        <p className="small muted" style={{ marginTop: -6, marginBottom: 12 }}>
          The nominal LFD is what the carrier bills against. The effective LFD is what the agent argues — the gap is
          the days the box was discharged but not actually collectable.
        </p>
        <div className="grid cols-3">
          <div className="stat">
            <div className="v" style={{ fontSize: 16 }}>
              {state.nominal_lfd ?? "—"}
            </div>
            <div className="l">Nominal LFD — carrier's figure</div>
          </div>
          <div className="stat">
            <div className="v amber" style={{ fontSize: 16 }}>
              {state.effective_lfd ?? "—"}
            </div>
            <div className="l">Effective LFD — argued, with citations</div>
          </div>
          <div className="stat">
            <div className="v">{state.protest_held ? "held" : "none"}</div>
            <div className="l">Payment under protest (Leak 07)</div>
          </div>
        </div>
      </div>

      <MoneyPanel arcs={arcs} />

      <div style={{ marginTop: 16 }}>
        <LeakLedger arcs={arcs} evidence={evidence ?? []} />
      </div>

      {arcs.demurrage && (
        <ArcCard
          title="Demurrage arc"
          containerId={cid}
          arc="demurrage"
          risk={arcs.demurrage.state.risk}
          status={arcs.demurrage.status}
          pendingApproval={arcs.demurrage.pending_approval}
          fields={[
            { label: "Reason", value: arcs.demurrage.state.reason || "—" },
            { label: "LFD shifted", value: arcs.demurrage.state.lfd_shifted ? "yes" : "no" },
            { label: "Open holds", value: arcs.demurrage.state.holds.join(", ") || "none" },
            { label: "Spend", value: formatUsd(arcs.demurrage.state.spend_usd) },
            { label: "Counterfactual", value: formatUsd(arcs.demurrage.state.counterfactual_usd) },
            { label: "Appointment failures", value: String(arcs.demurrage.state.appointment_failures) },
          ]}
        />
      )}

      {arcs.detention && (
        <ArcCard
          title="Detention arc"
          containerId={cid}
          arc="detention"
          risk={arcs.detention.state.risk}
          status={arcs.detention.status}
          pendingApproval={arcs.detention.pending_approval}
          fields={[
            { label: "Return slot", value: formatDate(arcs.detention.state.return_slot) },
            { label: "Empty returned", value: formatDate(arcs.detention.state.empty_returned_at) },
            { label: "Detention days", value: String(arcs.detention.state.detention_days) },
            { label: "Prevented", value: formatUsd(arcs.detention.state.prevented_usd) },
            { label: "Near miss", value: arcs.detention.state.near_miss ? "yes" : "no" },
            { label: "Restriction matched", value: arcs.detention.state.restriction_matched ? "yes" : "no" },
          ]}
        />
      )}

      {arcs.disputes.map((d) => (
        <DisputeLetterPanel
          key={d.workflow_id}
          invoiceId={d.invoice_id}
          containerId={cid}
          state={d.state}
          letter={d.letter}
          pendingApproval={d.pending_approval}
        />
      ))}

      <div className="card">
        <h3>Evidence log</h3>
        <EvidenceTable rows={evidence ?? []} />
      </div>

      {metaSignals && (
        <SignalForm containerId={cid} metaSignals={metaSignals} disputeInvoiceIds={disputeInvoiceIds} />
      )}
    </div>
  );
}
