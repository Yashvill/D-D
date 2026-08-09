import type { ContainerArcs } from "../types/api";
import { currentPhase } from "../utils/leaks";

const PHASES = [
  { n: 0, label: "Pre-arrival", range: "D1-7" },
  { n: 1, label: "Free time", range: "D8-12" },
  { n: 2, label: "Demurrage", range: "D13-20" },
  { n: 3, label: "Detention", range: "D20-31" },
  { n: 4, label: "Dispute", range: "D32-45+" },
];

/** Where this container sits in the 45-day journey, derived from which
 * timestamps its workflow has actually recorded. */
export function PhaseStrip({ arcs }: { arcs: ContainerArcs }) {
  const now = currentPhase(arcs);
  const c = arcs.container.state;

  const reached = (n: number): boolean => {
    if (n === 0) return c.terms_loaded;
    if (n === 1) return !!c.discharged_at;
    if (n === 2) return !!c.discharged_at;
    if (n === 3) return !!c.gated_out_at;
    return c.disputes.length > 0;
  };

  return (
    <div className="phase-strip">
      {PHASES.map((p) => {
        const done = reached(p.n);
        const active = p.n === now.n;
        return (
          <div className={`phase p${p.n} ${done ? "done" : ""} ${active ? "active" : ""}`} key={p.n}>
            <div className="pt">{p.label}</div>
            <div className="pr mono">{p.range}</div>
          </div>
        );
      })}
    </div>
  );
}
