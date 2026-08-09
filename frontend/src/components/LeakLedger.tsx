import type { ContainerArcs, EvidenceRow } from "../types/api";
import { attributeLeaks, LEAK_STATUS_LABEL } from "../utils/leaks";

/** The runbook's cash-leak ledger, but attributed live: each verdict comes
 * from workflow state, and shows "Not yet" where the agent has nothing to
 * evidence rather than claiming a win it cannot support. */
export function LeakLedger({ arcs, evidence }: { arcs: ContainerArcs; evidence: EvidenceRow[] }) {
  const rows = attributeLeaks(arcs, evidence);
  const settled = rows.filter((r) => ["prevented", "contested", "closed"].includes(r.status)).length;

  return (
    <div className="card">
      <h3>
        Cash leak ledger{" "}
        <span className="small muted mono" style={{ fontWeight: 400 }}>
          {settled}/7 addressed
        </span>
      </h3>
      <p className="small muted" style={{ marginTop: -6, marginBottom: 14 }}>
        The seven ways money leaks out of a container, each scored against what this agent can actually evidence.
      </p>
      <div className="leak-rows">
        {rows.map((r) => (
          <div className={`leak-row s-${r.status}`} key={r.id}>
            <div className="leak-id mono">{r.id}</div>
            <div className="leak-main">
              <div className="leak-name">{r.name}</div>
              <div className="leak-mech small muted">{r.mechanism}</div>
              <div className="leak-detail small">{r.detail}</div>
              {r.citations.length > 0 && (
                <div style={{ marginTop: 6 }}>
                  {r.citations.slice(0, 4).map((c, i) => (
                    <span className="chip" key={i}>
                      {c}
                    </span>
                  ))}
                  {r.citations.length > 4 && (
                    <span className="small muted">+{r.citations.length - 4} more</span>
                  )}
                </div>
              )}
            </div>
            <div className={`leak-status s-${r.status}`}>{LEAK_STATUS_LABEL[r.status]}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
