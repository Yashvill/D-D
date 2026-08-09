import { Link } from "react-router-dom";
import { useFleet, useHealth } from "../hooks/useApi";
import { ErrorState } from "../components/ErrorState";
import { StartContainerForm } from "../components/StartContainerForm";
import { DriveDemoButton } from "../components/DriveDemoButton";
import { useClock } from "../hooks/useClock";
import { cardState, footAmount, footLfd, phaseLabel, phaseLine } from "../utils/fleetCard";

/** The same "Fleet Control Tower" the pitch page mocks up in index.html - same
 * markup and CSS classes - except every card, KPI and the clock come from
 * GET /api/fleet against the real workflows rather than a client-side
 * random generator. */
export function FleetDashboard() {
  const { data, error, isLoading } = useFleet();
  const { data: health } = useHealth();
  const clock = useClock();

  const active = data?.filter((e) => e.status === "RUNNING").length ?? 0;
  const acting = data?.filter((e) => e.state?.risk === "RED").length ?? 0;
  const disputing = data?.filter((e) => (e.state?.disputes.length ?? 0) > 0).length ?? 0;
  const prevented = (data ?? []).reduce((sum, e) => sum + Number(e.state?.total_prevented_usd ?? 0), 0);
  const contested = (data ?? []).reduce((sum, e) => sum + Number(e.state?.total_contested_usd ?? 0), 0);

  const down = !health?.server || health.workers === 0;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 24 }}>
        <div>
          <div className="page-kicker">Control tower</div>
          <h1 className="page-title">Fleet</h1>
          <p className="muted small" style={{ marginTop: 8 }}>
            Every card below is a live Temporal workflow, queried straight off the running agent.
          </p>
        </div>
        <StartContainerForm />
      </div>

      {error && <ErrorState error={error} />}

      <DriveDemoButton />

      <div className="dash">
        <div className="dash-top">
          <div className="dots">
            <i /><i /><i />
          </div>
          <div className="title">persistent-fleet · control-tower</div>
          <div className="clock mono">{clock}</div>
        </div>

        <div className="kpi">
          <div className="k">
            <div className="kv sea">{active}</div>
            <div className="kl">Active agents</div>
          </div>
          <div className="k">
            <div className="kv amber">{acting}</div>
            <div className="kl">At risk (RED)</div>
          </div>
          <div className="k">
            <div className="kv red">{disputing}</div>
            <div className="kl">In dispute</div>
          </div>
          <div className="k">
            <div className="kv green">${(prevented / 1000).toFixed(1)}k</div>
            <div className="kl">Prevented</div>
          </div>
          <div className="k">
            <div className="kv green">${(contested / 1000).toFixed(1)}k</div>
            <div className="kl">Contested</div>
          </div>
        </div>

        {isLoading && (
          <div style={{ padding: 24 }}>
            <p className="muted small">Loading fleet…</p>
          </div>
        )}

        {data && data.length === 0 && !error && (
          <div style={{ padding: 24 }}>
            <p className="muted">No containers running. Start one above.</p>
          </div>
        )}

        {data && data.length > 0 && (
          <div className="fleet-grid">
            {data.map((entry) => {
              const st = cardState(entry);
              const amt = footAmount(entry);
              return (
                <Link key={entry.workflow_id} to={`/container/${entry.container_id}`} className="agent">
                  <div className="row1">
                    <span className="cid">{entry.container_id}</span>
                    <span className={`state ${st.key}`}>{st.label}</span>
                  </div>
                  <div className="lane">
                    {phaseLabel(entry)} · {entry.status ?? "—"}
                  </div>
                  <div className="phase">{phaseLine(entry)}</div>
                  <div className="foot">
                    <span className="lfd">{footLfd(entry)}</span>
                    <span className={`amt ${amt.cls}`}>{amt.text}</span>
                  </div>
                </Link>
              );
            })}
            {/* Fillers so the last row's cell borders line up instead of
                stopping mid-grid. */}
            {Array.from({ length: (4 - (data.length % 4)) % 4 }).map((_, i) => (
              <div className="agent filler" key={`filler-${i}`} />
            ))}
          </div>
        )}

        <div className={`dash-note ${down ? "down" : ""}`}>
          <span className="b" />
          Signals dispatch into sleeping workflows · queries stream state back out · {data?.length ?? 0} container
          {data?.length === 1 ? "" : "s"} tracked{down ? " · backend unreachable" : ""}
        </div>
      </div>
    </div>
  );
}
