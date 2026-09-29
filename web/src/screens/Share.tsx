import { useEffect, useRef, useState } from "react";
import { api2, ApiError, Message, Share } from "../api";

// v0.2 sharing: a read-only snapshot of chosen messages, readable by anyone with the link.
const shareable = (m: Message) => (m.role === "user" || m.role === "assistant") && !m.connection_request && m.content.trim() !== "";
const when = (iso: string) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

export function ShareDialog({ conversationId, title, messages, onClose, announce }: {
  conversationId: string; title: string; messages: Message[]; onClose: () => void; announce: (s: string) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const candidates = messages.filter(shareable);
  const [picked, setPicked] = useState<Set<string>>(() => new Set(candidates.map((m) => m.id)));
  const [name, setName] = useState(title);
  const [days, setDays] = useState<1 | 7 | 30>(7);
  const [created, setCreated] = useState<{ share: Share; url: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { ref.current?.showModal(); }, []);

  const create = async () => {
    setError("");
    try {
      const all = picked.size === candidates.length;
      const res = await api2.createShare(conversationId, { title: name.trim() || null, message_ids: all ? null : [...picked], expires_in_days: days });
      setCreated(res);
      announce("Share link created.");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not create the link.");
    }
  };
  const copy = async () => {
    try { await navigator.clipboard.writeText(created!.url); setCopied(true); }
    catch { (document.getElementById("share-url") as HTMLInputElement | null)?.select(); }
  };
  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  return (
    <dialog ref={ref} className="share-dialog" aria-labelledby="share-h" onClose={onClose}>
      <h2 id="share-h">Share conversation</h2>
      {!created ? (
        <>
          <p className="muted">Anyone with the link can read the messages you choose, as they are now. Check them for private details first.</p>
          <label className="block">Title <input value={name} maxLength={200} onChange={(e) => setName(e.target.value)} /></label>
          <fieldset>
            <legend>Messages ({picked.size} of {candidates.length})</legend>
            <ul className="share-pick">
              {candidates.map((m) => (
                <li key={m.id}>
                  <label>
                    <input type="checkbox" checked={picked.has(m.id)} onChange={() => toggle(m.id)} />
                    <span className="who">{m.role === "user" ? "You" : "Assistant"}</span>
                    <span className="text">{m.content.length > 140 ? m.content.slice(0, 140) + "…" : m.content}</span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>
          <p className="muted small">Account cards and attached files are never included.</p>
          <label className="block">Link works for{" "}
            <select value={days} onChange={(e) => setDays(Number(e.target.value) as 1 | 7 | 30)}>
              <option value={1}>1 day</option><option value={7}>7 days</option><option value={30}>30 days</option>
            </select>
          </label>
          {error && <p role="alert" className="error">{error}</p>}
          <div className="row end">
            <button onClick={() => ref.current?.close()}>Cancel</button>
            <button className="primary" disabled={picked.size === 0} onClick={create}>Create link</button>
          </div>
        </>
      ) : (
        <>
          <p>Anyone with this link can read {created.share.message_count} messages until {when(created.share.expires_at)}.</p>
          <div className="row">
            <label className="grow"><span className="sr-only">Share link</span>
              <input id="share-url" readOnly value={created.url} onFocus={(e) => e.currentTarget.select()} />
            </label>
            <button className="primary" onClick={copy}>{copied ? "Copied" : "Copy link"}</button>
          </div>
          <p className="muted small">The link is shown only now. You can turn it off any time in Menu → Shared links.</p>
          <div className="row end"><button onClick={() => ref.current?.close()}>Done</button></div>
        </>
      )}
    </dialog>
  );
}

export function SharedLinks({ tick, announce }: { tick: number; announce: (s: string) => void }) {
  const [items, setItems] = useState<Share[]>([]);
  const [error, setError] = useState("");
  const load = () => api2.shares().then(setItems).catch(() => setError("Could not load shared links."));
  useEffect(() => { void load(); }, [tick]);
  const STATUS: Record<Share["status"], string> = { active: "Active", expired: "Expired", revoked: "Turned off" };

  return (
    <section aria-labelledby="shares-h">
      <h2 id="shares-h">Shared links</h2>
      <p className="muted">Links to read-only snapshots of your conversations. Use Share in the chat to create one.</p>
      {error && <p role="alert" className="error">{error}</p>}
      {items.length === 0 ? <p className="muted">You have not shared anything.</p> : (
        <ul className="skills">
          {items.map((s) => (
            <li key={s.id}>
              <article className="skill-row" aria-label={s.title}>
                <div className="skill-main">
                  <h4>{s.title} <span className={`chip k-${s.status === "active" ? "ready" : "disabled"}`}>{STATUS[s.status]}</span></h4>
                  <p className="muted small">
                    {s.message_count} messages · shared {when(s.created_at)} · {s.status === "active" ? `until ${when(s.expires_at)}` : s.status === "revoked" ? `turned off ${when(s.revoked_at!)}` : `expired ${when(s.expires_at)}`}
                    {" · "}{s.view_count} {s.view_count === 1 ? "view" : "views"}
                  </p>
                </div>
                {s.status === "active" && (
                  <button onClick={async () => {
                    try { await api2.revokeShare(s.id); announce(`Link for “${s.title}” turned off.`); await load(); }
                    catch (e) { setError(e instanceof ApiError ? e.message : "Could not turn off the link."); }
                  }}>Turn off</button>
                )}
              </article>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
