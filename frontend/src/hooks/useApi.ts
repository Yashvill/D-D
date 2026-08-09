import { useQuery } from "@tanstack/react-query";
import { api } from "../services/api";

/** Poll intervals are deliberately short and per-endpoint: the whole premise
 * of this console is that the workflow *is* the read model, so staleness
 * should be seconds, not a manual refresh button. */

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 3000,
  });
}

export function useFleet() {
  return useQuery({
    queryKey: ["fleet"],
    queryFn: api.fleet,
    refetchInterval: 5000,
  });
}

export function useContainerArcs(cid: string | undefined) {
  return useQuery({
    queryKey: ["arcs", cid],
    queryFn: () => api.arcs(cid as string),
    enabled: !!cid,
    refetchInterval: 4000,
  });
}

export function useEvidence(cid: string | undefined) {
  return useQuery({
    queryKey: ["evidence", cid],
    queryFn: () => api.evidence(cid as string),
    enabled: !!cid,
    refetchInterval: 8000,
  });
}

export function useApprovals() {
  return useQuery({
    queryKey: ["approvals"],
    queryFn: api.approvals,
    refetchInterval: 5000,
  });
}

export function useMetaSignals() {
  return useQuery({
    queryKey: ["meta-signals"],
    queryFn: api.metaSignals,
    staleTime: Infinity,
  });
}
