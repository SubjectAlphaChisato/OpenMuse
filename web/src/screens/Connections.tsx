import { useCallback, useEffect, useState } from "react";
import { api2, ApiError, Connection, Connector, Me, Mode } from "../api";

const MODE_LABEL: Record<Mode, string> = { off: "Off", read_only: "Read only", read_write: "Read and propose changes" };
const CAP_LABEL: Record<string, string> = { gmail: "Gmail", calendar: "Calendar" };

export function Connections({ me, tick, onMe, announce }: { me: Me; tick: number; onMe: (m: Me) => void; announce: (s: string) => void }) {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [conns, setConns] = useState<Connection[]>([]);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => { setConnectors(await api2.connectors()); setConns(await api2.connections()); }, []);
  useEffect(() => { void refresh(); }, [refresh, tick]);
  const run = async (fn: () => Promise<string>) => {
    setError("");
    try { announce(await fn()); await refresh(); } catch (e) { setError(e instanceof ApiError ? e.message : "Action failed"); await refresh(); }
  };

  return (
    <section aria-labelledby="conn-h">
      <h2 id="conn-h">Connections and permissions</h2>
      {error && <p role="alert" className="error">{error}</p>}
      <label>When should the assistant ask first?{" "}
        <select value={me.approval_mode} onChange={(e) => {
          const mode = e.target.value as "ask_writes" | "always_ask";
          void run(async () => { onMe(await api2.setApprovalMode(mode)); return "Approval setting saved."; });
        }}>
          <option value="ask_writes">Before any change (sending, creating, editing)</option>
          <option value="always_ask">Before every read and every change</option>
        </select>
      </label>

      <h3>Connected accounts</h3>
      {conns.length === 0 && <p>No accounts connected.</p>}
      {conns.map((c) => (
        <article key={c.id} className="card" aria-label={`${c.account_label}${c.is_demo ? " (demo)" : ""}`}>
          <p><strong>{c.account_label}</strong>{c.is_demo && <span className="badge">DEMO</span>} <span className="muted">{c.status}</span></p>
          {Object.keys(CAP_LABEL).map((cap) => (
            <label key={cap} className="block">{CAP_LABEL[cap]}{" "}
              <select value={c.modes[cap] ?? "off"} onChange={(e) => {
                const mode = e.target.value as Mode; // read now: the controlled select resets before the request returns
                void run(async () => { await api2.setModes(c, { [cap]: mode }); return `${CAP_LABEL[cap]} set to ${MODE_LABEL[mode]}.`; });
              }}>
                {(Object.keys(MODE_LABEL) as Mode[]).map((m) => <option key={m} value={m}>{MODE_LABEL[m]}</option>)}
              </select>
            </label>
          ))}
          <p className="muted">Changes always need your approval, even in “Read and propose changes”.</p>
          <button onClick={() => { if (confirm(`Disconnect ${c.account_label}? Pending approvals for it will be cancelled.`)) void run(async () => { await api2.disconnect(c); return "Account disconnected."; }); }}>Disconnect</button>
        </article>
      ))}

      <h3>Add an account</h3>
      <ul className="plain">
        {connectors.map((k) => (
          <li key={k.provider}>
            {k.label}{" "}
            {k.available ? <button onClick={() => run(async () => { await api2.connect(k.provider); return `${k.label} connected (read only).`; })}>Connect</button>
              : <span className="muted">{k.reason}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}
