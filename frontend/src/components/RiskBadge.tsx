import type { RiskLevel } from "../types/api";

export function RiskBadge({ risk }: { risk: RiskLevel | string }) {
  return <span className={`badge risk-${risk}`}>{risk}</span>;
}
