import { ApiError } from "../services/api";

/** Maps api.py's status codes onto states the operator can act on. The whole
 * point is that a stopped worker must render as "unavailable", never as zeros
 * presented as though they were real. */
export function ErrorState({ error }: { error: unknown }) {
  if (error instanceof ApiError) {
    if (error.status === 0 || error.status === 503) {
      return (
        <div className="unavailable-banner">
          Backend unreachable — the Temporal server or worker is not responding.
          <div className="small muted" style={{ marginTop: 4 }}>
            {error.detail}
          </div>
        </div>
      );
    }
    if (error.status === 404) {
      return <div className="unavailable-banner">Not found — {error.detail}</div>;
    }
    if (error.status === 409) {
      return <div className="unavailable-banner">Already closed — {error.detail}</div>;
    }
    return <div className="unavailable-banner">Error {error.status}: {error.detail}</div>;
  }
  return <div className="unavailable-banner">Something went wrong.</div>;
}
