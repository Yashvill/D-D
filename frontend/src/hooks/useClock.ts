import { useEffect, useState } from "react";

/** Ticking wall clock for the control-tower header - decorative, but real. */
export function useClock(): string {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now.toLocaleTimeString("en-US", { hour12: false });
}
