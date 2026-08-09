import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../services/api";
import type { MetaSignals } from "../types/api";
import { defaultBodyFor } from "../utils/signalDefaults";

type Target = "container" | "demurrage" | "detention" | "dispute";

interface Props {
  containerId: string;
  metaSignals: MetaSignals;
  disputeInvoiceIds: string[];
}

/** Manual/demo signal sender. Bodies default to the same worked example
 * scripts/start_workflow.py drives (MSKU7481920, March 2026), pre-filled as
 * editable JSON rather than a bespoke form per signal - the console's job here
 * is to make every signal reachable and inspectable, not to reimplement each
 * payload model as a form widget. */
export function SignalForm({ containerId, metaSignals, disputeInvoiceIds }: Props) {
  const [target, setTarget] = useState<Target>("container");
  const options = metaSignals[target];
  const [name, setName] = useState(options[0] ?? "");
  const [invoiceId, setInvoiceId] = useState(disputeInvoiceIds[0] ?? "");
  const [bodyText, setBodyText] = useState(() => JSON.stringify(defaultBodyFor(options[0] ?? ""), null, 2));
  const [result, setResult] = useState<string | null>(null);
  const queryClient = useQueryClient();

  useEffect(() => {
    const names = metaSignals[target];
    setName(names[0] ?? "");
    setBodyText(JSON.stringify(defaultBodyFor(names[0] ?? ""), null, 2));
    setResult(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  function onSelectName(n: string) {
    setName(n);
    setBodyText(JSON.stringify(defaultBodyFor(n), null, 2));
    setResult(null);
  }

  const mutation = useMutation({
    mutationFn: async () => {
      const body = bodyText.trim() ? JSON.parse(bodyText) : {};
      if (target === "container") return api.signalContainer(containerId, name, body);
      if (target === "dispute") return api.signalDispute(invoiceId, name, body);
      return api.signalArc(containerId, target, name, body);
    },
    onSuccess: () => {
      setResult("sent");
      queryClient.invalidateQueries({ queryKey: ["arcs", containerId] });
      queryClient.invalidateQueries({ queryKey: ["evidence", containerId] });
      queryClient.invalidateQueries({ queryKey: ["fleet"] });
    },
    onError: (err) => setResult(String(err)),
  });

  return (
    <div className="card">
      <h3>Journey driver</h3>
      <div className="form-row">
        <div className="field">
          <label>Target</label>
          <select value={target} onChange={(e) => setTarget(e.target.value as Target)}>
            <option value="container">container</option>
            <option value="demurrage">demurrage arc</option>
            <option value="detention">detention arc</option>
            <option value="dispute">dispute</option>
          </select>
        </div>
        <div className="field">
          <label>Signal</label>
          <select value={name} onChange={(e) => onSelectName(e.target.value)}>
            {options.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
      </div>

      {target === "dispute" && (
        <div className="field">
          <label>Invoice ID</label>
          {disputeInvoiceIds.length > 0 ? (
            <select value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)}>
              {disputeInvoiceIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          ) : (
            <input type="text" value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)} placeholder="INV-DEM-88431" />
          )}
        </div>
      )}

      <div className="field">
        <label>Body (JSON)</label>
        <textarea rows={bodyText.split("\n").length + 1} value={bodyText} onChange={(e) => setBodyText(e.target.value)} className="mono" />
      </div>

      <button className="btn primary" onClick={() => mutation.mutate()} disabled={mutation.isPending || (target === "dispute" && !invoiceId)}>
        {mutation.isPending ? "Sending…" : `Send ${name}`}
      </button>
      {result && <p className="small" style={{ marginTop: 8 }}>{result}</p>}
    </div>
  );
}
