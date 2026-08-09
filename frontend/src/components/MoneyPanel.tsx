import type { ContainerArcs } from "../types/api";
import { formatUsd } from "../utils/format";

/** The counterfactual ledger, made legible.
 *
 * counterfactual_usd is what the agent predicted would be billed if it did
 * nothing - stored at decision time, before the outcome was known. Showing it
 * beside what was actually spent and prevented is what makes a savings claim
 * auditable rather than asserted, which is the entire argument of the
 * runbook's burn curve. */
export function MoneyPanel({ arcs }: { arcs: ContainerArcs }) {
  const c = arcs.container.state;
  const dem = arcs.demurrage?.state;
  const det = arcs.detention?.state;

  const billed = Number(c.total_billed_usd);
  const prevented = Number(c.total_prevented_usd);
  const contested = Number(c.total_contested_usd);
  const spend = Number(c.spend_usd);
  const counterfactual = Number(dem?.counterfactual_usd ?? 0);

  const recovered = arcs.disputes.reduce((s, d) => s + Number(d.state.recovered_usd), 0);
  const exposure = billed + prevented;
  const netAtRisk = Math.max(0, billed - contested - recovered);

  const bars = [
    { label: "Would have been billed", value: exposure, cls: "bill", note: "unmanaged path" },
    { label: "Prevented outright", value: prevented, cls: "prev", note: det?.empty_returned_at ? "empty returned inside free time" : "" },
    { label: "Contested with evidence", value: contested, cls: "cont", note: `${arcs.disputes.length} dispute(s) filed` },
    { label: "Recovered so far", value: recovered, cls: "rec", note: recovered > 0 ? "" : "settlement pending" },
  ];
  const max = Math.max(...bars.map((b) => b.value), 1);

  return (
    <div className="card">
      <h3>The money</h3>
      <p className="small muted" style={{ marginTop: -6, marginBottom: 16 }}>
        Every figure below is workflow state, not a projection. The counterfactual was stored at decision time and is
        graded against what actually happened.
      </p>

      <div className="burn">
        {bars.map((b) => (
          <div className="burn-row" key={b.label}>
            <div className="burn-label small">{b.label}</div>
            <div className="burn-track">
              <i className={b.cls} style={{ width: `${(b.value / max) * 100}%` }} />
            </div>
            <div className="burn-value mono">{formatUsd(String(b.value))}</div>
          </div>
        ))}
      </div>

      <div className="grid cols-3" style={{ marginTop: 18 }}>
        <div className="stat">
          <div className="v amber">{formatUsd(String(counterfactual))}</div>
          <div className="l">Counterfactual — predicted if the agent did nothing</div>
        </div>
        <div className="stat">
          <div className="v sea">{formatUsd(String(spend))}</div>
          <div className="l">Spent intervening {spend === 0 ? "— nothing was worth buying" : ""}</div>
        </div>
        <div className="stat">
          <div className="v red">{formatUsd(String(netAtRisk))}</div>
          <div className="l">Still at risk, uncontested</div>
        </div>
      </div>

      {dem?.intervention_suppressed && (
        <p className="small" style={{ marginTop: 14, color: "var(--amber)" }}>
          Intervention suppressed while the box was under hold — the agent recorded the window instead of spending to
          look busy, and the counterfactual is what proves that was correct.
        </p>
      )}
    </div>
  );
}
