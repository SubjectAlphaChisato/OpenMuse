import { useState } from "react";
import { api2, ApiError, ConnectionRequest } from "../api";

// v0.2 harness: an account change the assistant asked for. Only the owner's click here carries it out.
const MODE: Record<string, string> = { off: "Off", read_only: "Read only", read_write: "Read and propose changes" };
const STATUS: Record<string, string> = {
  completed: "Done", declined: "Declined", expired: "Expired: ask the assistant again if you still want this",
  superseded: "Replaced by a newer request", failed: "Failed", waiting_for_provider: "Waiting for you to finish signing in",
};

export function ConnectionCard({ req, onChanged }: { req: ConnectionRequest; onChanged: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const open = req.status === "pending" || req.status === "waiting_for_provider";

  const act = async (confirm: boolean) => {
    setBusy(true); setError("");
    try {
      if (!confirm) { await api2.declineRequest(req.id); onChanged(`Declined: ${req.title}.`); return; }
      const res = await api2.confirmRequest(req.id);
      if (res.authorization_url) { window.location.assign(res.authorization_url); return; } // the provider returns to the chat
      onChanged(`${req.title}: ${res.request.result ?? "done"}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "That did not work. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className={`card request r-${req.status}`} aria-label={req.title}>
      <h4>{req.title}</h4>
      {req.reason && <p className="muted">Assistant: “{req.reason}”</p>}
      {req.changes.length > 0 && (
        <ul className="changes">
          {req.changes.map((c) => (
            <li key={c.capability}>{c.label}: {req.action === "connect" ? MODE[c.after] : `${MODE[c.before]} → ${MODE[c.after]}`}</li>
          ))}
        </ul>
      )}
      <p className="muted">{req.note}</p>
      {open ? (
        <div className="actions">
          {req.status === "waiting_for_provider" && <p>{STATUS.waiting_for_provider}.</p>}
          <button className="primary" disabled={busy} onClick={() => act(true)}>{req.confirm_label}</button>
          <button disabled={busy} onClick={() => act(false)}>Not now</button>
        </div>
      ) : (
        <p className="status" role="status">{STATUS[req.status] ?? req.status}{req.result && req.status === "completed" ? `: ${req.result}` : ""}</p>
      )}
      {error && <p role="alert" className="error">{error}</p>}
    </article>
  );
}
