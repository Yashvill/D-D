import type {
  ContainerArcs,
  ContainerInput,
  ContainerState,
  DisputeDetail,
  EvidenceRow,
  FleetApproval,
  FleetEntry,
  HealthStatus,
  MetaSignals,
  PendingApproval,
} from "../types/api";

/** Preserves the status code api.py already maps (404/409/503/502) so pages
 * can distinguish "not found" from "backend unreachable" instead of both
 * collapsing into an empty/zero state. */
export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(0, "network error - is the API reachable?");
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // body wasn't JSON; fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  health: () => get<HealthStatus>("/api/health"),
  fleet: () => get<FleetEntry[]>("/api/fleet"),
  container: (cid: string) =>
    get<{ container_id: string; workflow_id: string; status: string | null; state: ContainerState }>(
      `/api/container/${encodeURIComponent(cid)}`
    ),
  evidence: (cid: string) => get<EvidenceRow[]>(`/api/container/${encodeURIComponent(cid)}/evidence`),
  arcs: (cid: string) => get<ContainerArcs>(`/api/container/${encodeURIComponent(cid)}/arcs`),
  pendingApproval: (cid: string, arc: "demurrage" | "detention") =>
    get<PendingApproval | null>(
      `/api/container/${encodeURIComponent(cid)}/arc/${arc}/pending_approval`
    ),
  disputePendingApproval: (invoiceId: string) =>
    get<PendingApproval | null>(`/api/dispute/${encodeURIComponent(invoiceId)}/pending_approval`),
  approvals: () => get<FleetApproval[]>("/api/approvals"),
  dispute: (invoiceId: string) => get<DisputeDetail>(`/api/dispute/${encodeURIComponent(invoiceId)}`),
  metaSignals: () => get<MetaSignals>("/api/meta/signals"),

  signalContainer: (cid: string, name: string, body: Record<string, unknown>) =>
    post(`/api/container/${encodeURIComponent(cid)}/signal/${name}`, body),
  signalArc: (cid: string, arc: "demurrage" | "detention", name: string, body: Record<string, unknown>) =>
    post(`/api/container/${encodeURIComponent(cid)}/arc/${arc}/signal/${name}`, body),
  signalDispute: (invoiceId: string, name: string, body: Record<string, unknown>) =>
    post(`/api/dispute/${encodeURIComponent(invoiceId)}/signal/${name}`, body),

  startContainer: (payload: {
    container: ContainerInput;
    contract_text?: string;
    auto_approve_limit_usd?: string;
    slot_scarcity_prior?: number;
    depot_restriction_prior?: number;
    fresh?: boolean;
  }) => post<{ ok: boolean; workflow_id: string; already_running: boolean }>("/api/container/start", payload),

  terminateContainer: (cid: string) =>
    post<{ ok: boolean; terminated: string[] }>(`/api/container/${encodeURIComponent(cid)}/terminate`),
};
