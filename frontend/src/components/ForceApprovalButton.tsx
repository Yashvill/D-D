import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { forceApproval } from "../utils/demoDriver";

export function ForceApprovalButton() {
  const [running, setRunning] = useState(false);
  const [log, setLog] = useState<string[]>([]);
  const queryClient = useQueryClient();

  async function run() {
    setRunning(true);
    setLog([]);
    try {
      await forceApproval((msg) => setLog((l) => [...l, msg]));
    } catch (err) {
      setLog((l) => [...l, `error: ${String(err)}`]);
    } finally {
      setRunning(false);
      queryClient.invalidateQueries({ queryKey: ["approvals"] });
      queryClient.invalidateQueries({ queryKey: ["fleet"] });
    }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3>Force a live approval</h3>
      <p className="small muted" style={{ marginBottom: 12 }}>
        Starts a container with no hold and a $50 spend cap, so the pre-LFD checkpoint's cheapest real option still
        needs a human - a genuine <code className="mono">notify_human</code> parking the workflow, not a simulated
        one. Requires the worker running with <code className="mono">PF_DEMO_MODE=1</code>.
      </p>
      <button className="btn primary" onClick={run} disabled={running}>
        {running ? "Working…" : "▶ Force a live approval"}
      </button>
      {log.length > 0 && (
        <div className="mono small" style={{ marginTop: 12, color: "var(--ink-2)", lineHeight: 1.7 }}>
          {log.map((l, i) => (
            <div key={i}>{l}</div>
          ))}
        </div>
      )}
    </div>
  );
}
