import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { driveDemo } from "../utils/demoDriver";

const DEMO_CID = "MSKU7481920";

export function DriveDemoButton() {
  const [running, setRunning] = useState(false);
  const [done, setDone] = useState(false);
  const [log, setLog] = useState<string[]>([]);
  const queryClient = useQueryClient();

  async function run() {
    setRunning(true);
    setDone(false);
    setLog([]);
    try {
      await driveDemo((msg) => setLog((l) => [...l, msg]));
      setDone(true);
    } catch (err) {
      setLog((l) => [...l, `error: ${String(err)}`]);
    } finally {
      setRunning(false);
      queryClient.invalidateQueries({ queryKey: ["fleet"] });
      queryClient.invalidateQueries({ queryKey: ["arcs", DEMO_CID] });
    }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3>Drive the 45-day demo</h3>
      <p className="small muted" style={{ marginBottom: 12 }}>
        Fires the same signal sequence as <code className="mono">scripts/start_workflow.py</code> against{" "}
        <span className="mono">{DEMO_CID}</span>: a suppressed hold, a late-availability LFD shift, gate-out, an
        early empty return, and a non-compliant invoice that spawns a cited dispute letter. About 10 seconds.
      </p>
      <button className="btn primary" onClick={run} disabled={running}>
        {running ? "Driving…" : "▶ Drive the demo"}
      </button>
      {done && (
        <span className="small" style={{ marginLeft: 12 }}>
          <Link to={`/container/${DEMO_CID}`}>Open {DEMO_CID} →</Link>
        </span>
      )}
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
