import type { DisputeState, LetterDraft, PendingApproval } from "../types/api";
import { formatDate, formatUsd } from "../utils/format";
import { ApprovalCard } from "./ApprovalCard";

interface Props {
  invoiceId: string;
  containerId: string;
  state: DisputeState;
  letter: LetterDraft | null;
  pendingApproval?: PendingApproval | null;
}

const OUTCOME_LABEL: Record<string, string> = {
  unresolved: "Open — chasing on cadence",
  settled_within_mandate: "Settled inside the agent's mandate",
  settled_by_human: "Settled by a human, above the agent's mandate",
};

/** "Read the letter the agent wrote": the single most demonstrable artefact in
 * the system, plus the dropped_claims the guardrail refused to plead - claims
 * the system decided not to make, shown rather than hidden. */
export function DisputeLetterPanel({ invoiceId, containerId, state, letter, pendingApproval }: Props) {
  const recovered = Number(state.recovered_usd);
  const contested = Number(state.amount_contested_usd);
  const pct = contested > 0 && recovered > 0 ? Math.round((recovered / contested) * 100) : null;

  return (
    <div className="card">
      <h3>Dispute · {invoiceId}</h3>
      <div className="grid cols-3" style={{ marginBottom: 12 }}>
        <div className="stat">
          <div className="v red">{formatUsd(state.amount_contested_usd)}</div>
          <div className="l">Contested</div>
        </div>
        <div className="stat">
          <div className="v green">{formatUsd(state.recovered_usd)}</div>
          <div className="l">Recovered{pct !== null ? ` — ${pct}% of claim` : ""}</div>
        </div>
        <div className="stat">
          <div className="v" style={{ fontSize: 14 }}>
            {OUTCOME_LABEL[state.outcome] ?? state.outcome}
          </div>
          <div className="l">
            Outcome{state.follow_ups_sent > 0 ? ` · ${state.follow_ups_sent} follow-up(s) sent` : ""}
          </div>
        </div>
      </div>

      {state.offer_usd && recovered === 0 && (
        <p className="small" style={{ color: "var(--amber)", marginBottom: 12 }}>
          Carrier offered {formatUsd(state.offer_usd)} — below the 70% mandate, so the agent may not accept it alone.
        </p>
      )}

      {pendingApproval && (
        <div style={{ marginBottom: 14 }}>
          <ApprovalCard
            containerId={containerId}
            arc="dispute"
            invoiceId={invoiceId}
            approval={pendingApproval}
          />
        </div>
      )}

      {state.findings.length > 0 && (
        <>
          <p className="small muted" style={{ marginBottom: 4 }}>
            Findings ({state.voids_entire_charge ? "voids entire charge" : "partial"})
          </p>
          <div style={{ marginBottom: 12 }}>
            {state.findings.map((f) => (
              <span key={f} className="chip">
                {f}
              </span>
            ))}
          </div>
        </>
      )}

      {state.dropped_claims.length > 0 && (
        <div className="dropped-claims" style={{ marginBottom: 12 }}>
          <p className="small" style={{ marginBottom: 4 }}>
            Claims dropped for lack of citation ({state.dropped_claims.length})
          </p>
          <ul style={{ paddingLeft: 18 }}>
            {state.dropped_claims.map((c, i) => (
              <li key={i} className="small muted">
                {c}
              </li>
            ))}
          </ul>
        </div>
      )}

      {letter ? (
        <>
          <p className="small muted">
            {letter.subject} · drafted {formatDate(letter.drafted_at)}
          </p>
          <div className="letter-body">{letter.body}</div>
          <div>
            {letter.citations.map((c, i) => (
              <span key={i} className="chip">
                {c}
              </span>
            ))}
          </div>
        </>
      ) : (
        <p className="muted small">No letter drafted yet.</p>
      )}
    </div>
  );
}
