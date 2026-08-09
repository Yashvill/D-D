import { useApprovals } from "../hooks/useApi";
import { ApprovalCard } from "../components/ApprovalCard";
import { ErrorState } from "../components/ErrorState";
import { ForceApprovalButton } from "../components/ForceApprovalButton";

export function ApprovalInbox() {
  const { data, error, isLoading } = useApprovals();

  return (
    <div>
      <div className="page-kicker">Human gates</div>
      <h1 className="page-title">Approvals</h1>
      <p className="muted small" style={{ marginTop: 8, marginBottom: 20 }}>
        Every arc currently parked on <code>wait_condition(choice.action in self.approvals)</code> - a real block,
        not a simulated one.
      </p>

      <ForceApprovalButton />

      {error && <ErrorState error={error} />}
      {isLoading && <p className="muted small">Loading…</p>}

      {data && data.length === 0 && !error && (
        <div className="card">
          <p className="muted">Nothing waiting on a decision right now.</p>
        </div>
      )}

      {data?.map((a) => (
        <ApprovalCard
          key={`${a.workflow_id}-${a.action}`}
          containerId={a.container_id}
          arc={a.arc}
          invoiceId={a.invoice_id}
          approval={a}
          showContainerLink
        />
      ))}
    </div>
  );
}
