import { useHealth } from "../hooks/useApi";

export function HealthBanner() {
  const { data, isError } = useHealth();

  const down = isError || !data?.server || data.workers === 0;
  const label = isError
    ? "API unreachable"
    : !data?.server
      ? "temporal unreachable"
      : data.workers === 0
        ? "no worker polling"
        : "live";

  return (
    <div className={`health-banner ${down ? "down" : "ok"}`}>
      <span className="b" />
      {label}
      {data?.readonly ? " · read-only" : ""}
    </div>
  );
}
