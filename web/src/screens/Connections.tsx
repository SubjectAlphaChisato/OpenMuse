import { useCallback, useEffect, useState } from "react";
import { api2, ApiError, Connection, Connector, Me, Mode } from "../api";

const MODE_LABEL: Record<Mode, string> = { off: "Off", read_only: "Read only", read_write: "Read and propose changes" };
const CAP_LABEL: Record<string, string> = { gmail: "Gmail", calendar: "Calendar" };
const STATUS_LABEL: Record<string, string> = { connected: "Connected", reauth_required: "Needs reconnecting", error: "Error", disconnected: "Disconnected" };

async function goToGoogle(modes: Record<string, Mode>, connectionId?: string) {
  const { authorization_url } = await api2.googleAuthorize(modes, connectionId);
  window.location.assign(authorization_url); // Google returns to /api/oauth/google/callback
}

export function Connections({ me, tick, onMe, announce, notice }: {
  me: Me; tick: number; onMe: (m: Me) => void; announce: (s: string) => void; notice?: { ok: boolean; text: string } | null;
}) {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [conns, setConns] = useState<Connection[]>([]);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => { setConnectors(await api2.connectors()); setConns(await api2.connections()); }, []);
  useEffect(() => { void refresh(); }, [refresh, tick]);
  const run = async (fn: () => Promise<string | void>) => {
    setError("");
    try { const m = await fn(); if (m) announce(m); await refresh(); } catch (e) { setError(e instanceof ApiError ? e.message : "Action failed"); await refresh(); }
  };

  const changeMode = (c: Connection, cap: string, mode: Mode) => run(async () => {
    try {
      await api2.setModes(c, { [cap]: mode });
      return `${CAP_LABEL[cap]} set to ${MODE_LABEL[mode]}.`;
    } catch (e) {
      if (e instanceof ApiError && e.code === "scope_required") {
        if (confirm(`${e.message}\n\nContinue to Google to grant it?`)) await goToGoogle({ [cap]: mode }, c.id);
        return;
      }
      throw e;
    }
  });

  return (
    <section aria-labelledby="conn-h">
      <h2 id="conn-h">Connections and permissions</h2>
      {notice && <p role={notice.ok ? "status" : "alert"} className={notice.ok ? "notice" : "error"}>{notice.text}</p>}
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
          <p><strong>{c.account_label}</strong>{c.is_demo && <span className="badge">DEMO</span>}{" "}
            <span className={c.status === "connected" ? "muted" : "error"}>{STATUS_LABEL[c.status] ?? c.status}</span></p>
          {c.status === "reauth_required" && (
            <p role="alert" className="error">Google access expired or was removed. Reconnect to continue.{" "}
              <button onClick={() => run(() => goToGoogle(c.modes, c.id))}>Reconnect</button></p>
          )}
          {c.warning && <p className="notice">{c.warning} <button onClick={() => run(() => goToGoogle(c.modes, c.id))}>Reconnect now</button></p>}
          {Object.keys(CAP_LABEL).map((cap) => (
            <label key={cap} className="block">{CAP_LABEL[cap]}{" "}
              <select value={c.modes[cap] ?? "off"} disabled={c.status !== "connected"}
                onChange={(e) => { const mode = e.target.value as Mode; void changeMode(c, cap, mode); }}>
                {(Object.keys(MODE_LABEL) as Mode[]).map((m) => <option key={m} value={m}>{MODE_LABEL[m]}</option>)}
              </select>
            </label>
          ))}
          <p className="muted">Changes always need your approval, even in “Read and propose changes”.
            {c.provider === "google" && " Turning a capability off here stops the assistant from using it; Google keeps the grant until you disconnect."}</p>
          <div className="row">
            {!c.is_demo && <button onClick={() => run(async () => {
              const h = await api2.health(c); return h.status === "connected" ? "Connection is working." : `Connection problem: ${h.detail ?? h.status}.`;
            })}>Check connection</button>}
            <button onClick={() => {
              const shared = c.provider === "google" ? " Gmail and Calendar access are removed together, because Google grants them as one." : "";
              if (confirm(`Disconnect ${c.account_label}? Pending approvals for it will be cancelled.${shared}`)) void run(async () => {
                const r = await api2.disconnect(c) as Connection & { revoked?: boolean; revocation_error?: string | null };
                return r.revoked === false && r.revocation_error ? `Disconnected here, but Google revocation failed: ${r.revocation_error}` : "Account disconnected.";
              });
            }}>Disconnect</button>
          </div>
        </article>
      ))}

      <h3>Add an account</h3>
      <ul className="plain">
        {connectors.map((k) => (
          <li key={k.provider}>
            {k.label}{" "}
            {k.available
              ? <button onClick={() => run(async () => {
                  if (k.provider === "google") { await goToGoogle({ gmail: "read_only", calendar: "read_only" }); return; }
                  await api2.connect(k.provider); return `${k.label} connected (read only).`;
                })}>{k.provider === "google" ? "Connect with Google" : "Connect"}</button>
              : <span className="muted">{k.reason}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}
