import type { EvidenceRow } from "../types/api";
import { formatDate } from "../utils/format";

/** Every row renders its citation as a chip - source_document_id is mandatory
 * on the Evidence model (agents/shared/models.py), so an empty chip here would
 * mean the backend contract broke, not that the field is optional. */
export function EvidenceTable({ rows }: { rows: EvidenceRow[] }) {
  if (rows.length === 0) {
    return <p className="muted small">No evidence recorded yet.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>When</th>
          <th>Kind</th>
          <th>Summary</th>
          <th>Citation</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={`${row.source_document_id}-${i}`}>
            <td className="mono small">{formatDate(row.occurred_at)}</td>
            <td className="small">{row.kind}</td>
            <td>{row.summary}</td>
            <td>
              <span className="chip">{row.source_document_id}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
