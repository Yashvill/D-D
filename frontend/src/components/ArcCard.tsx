import type { PendingApproval } from "../types/api";
import { ApprovalCard } from "./ApprovalCard";
import { RiskBadge } from "./RiskBadge";

interface Props {
  title: string;
  containerId: string;
  arc: "demurrage" | "detention";
  risk?: string;
  status: string | null;
  fields: { label: string; value: string }[];
  pendingApproval: PendingApproval | null;
}

export function ArcCard({ title, containerId, arc, risk, status, fields, pendingApproval }: Props) {
  return (
    <div className="card">
      <h3>
        {title} {risk && <RiskBadge risk={risk} />}{" "}
        <span className="small muted mono">{status ?? "—"}</span>
      </h3>
      <div className="grid cols-3">
        {fields.map((f) => (
          <div className="stat" key={f.label}>
            <div className="v" style={{ fontSize: 15 }}>
              {f.value}
            </div>
            <div className="l">{f.label}</div>
          </div>
        ))}
      </div>
      {pendingApproval && (
        <div style={{ marginTop: 14 }}>
          <ApprovalCard containerId={containerId} arc={arc} approval={pendingApproval} />
        </div>
      )}
    </div>
  );
}
