import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../services/api";
import type { ApprovalArc, PendingApproval } from "../types/api";
import { formatDate, formatUsd, humanizeSignal } from "../utils/format";

interface Props {
  containerId: string;
  arc: ApprovalArc;
  approval: PendingApproval;
  /** Required when arc is "dispute" - that gate is keyed by invoice, not box. */
  invoiceId?: string;
  showContainerLink?: boolean;
}

/** The one action in the console that resumes a genuinely blocked workflow.
 *
 * Two different gates land here. The demurrage and detention arcs park on
 * `wait_condition(choice.action in self.approvals)` and are released by
 * `approve`. DisputeArc parks on `"settlement" in self.approvals` and is
 * released by `approve_settlement` - a different signal on a different
 * workflow, so the target is chosen from the arc rather than assumed. */
export function ApprovalCard({ containerId, arc, approval, invoiceId, showContainerLink }: Props) {
  const queryClient = useQueryClient();
  const isDispute = arc === "dispute";

  const mutation = useMutation({
    mutationFn: () =>
      isDispute
        ? api.signalDispute(invoiceId as string, "approve_settlement", { action: approval.action })
        : api.signalArc(containerId, arc, "approve", { action: approval.action }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["arcs", containerId] });
      queryClient.invalidateQueries({ queryKey: ["approvals"] });
    },
  });

  const cost = Number(approval.cost_usd);

  return (
    <div className="card approval">
      <h3>
        {humanizeSignal(approval.action)} · {arc}
        {isDispute && invoiceId ? ` · ${invoiceId}` : ""}
        {showContainerLink && containerId && (
          <>
            {" "}
            on <Link to={`/container/${containerId}`}>{containerId}</Link>
          </>
        )}
      </h3>
      <p className="reason">{approval.reason}</p>
      {approval.detail && <p className="small muted">{approval.detail}</p>}
      <div className="grid cols-2" style={{ marginBottom: 12 }}>
        <div className="stat">
          <div className="v amber">{cost > 0 ? formatUsd(approval.cost_usd) : "no spend"}</div>
          <div className="l">{cost > 0 ? "Cost of acting" : "Settlement decision, not a spend"}</div>
        </div>
        <div className="stat">
          <div className="v mono" style={{ fontSize: 13 }}>
            {formatDate(approval.requested_at)}
          </div>
          <div className="l">Parked since</div>
        </div>
      </div>
      <button
        className="btn primary"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || (isDispute && !invoiceId)}
      >
        {mutation.isPending ? "Approving…" : isDispute ? "Accept settlement" : "Approve"}
      </button>
      {mutation.isError && (
        <p className="small" style={{ color: "var(--red)", marginTop: 8 }}>
          {String(mutation.error)}
        </p>
      )}
    </div>
  );
}
