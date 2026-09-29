import { useCallback, useEffect, useState } from "react";
import { Action, api2, ApiError, Approval, PreviewField } from "../api";

function relative(iso: string): string {
  const mins = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  if (mins <= 0) return "expired";
  return mins < 90 ? `in ${mins} min` : `in ${Math.round(mins / 60)} h`;
}

function Fields({ fields }: { fields: PreviewField[] }) {
  return (
    <dl className="fields">
      {fields.map((f, i) => (
        <div key={i} className="field">
          <dt>{f.label}</dt>
          <dd>
            {f.before !== undefined && <><del aria-label={`was ${f.before}`}>{f.before}</del>{" → "}</>}
            {f.multiline ? <pre>{f.value}</pre> : <span>{f.value}</span>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** One pending change, shown exactly as it will be sent. Used on the Approvals screen and inline in the chat. */
export function ApprovalCard({ a, announce, onDone }: { a: Approval; announce: (s: string) => void; onDone: () => void | Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const titleId = `ap-${a.id}`;
  const descId = `ap-d-${a.id}`;
  const decide = async (decision: "allow_once" | "deny") => {
    setError(""); setBusy(true);
    try {
      const r = await api2.decide(a, decision);
      announce(decision === "allow_once"
        ? (r.outcome === "approved" ? `Approved: ${a.preview.title}. The assistant will continue.` : `Not approved: this request is ${r.outcome}.`)
        : (r.outcome === "denied" ? `Denied: ${a.preview.title}.` : `This request is ${r.outcome}.`));
      await onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Action failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="card approval" aria-labelledby={titleId} aria-describedby={descId}>
      <h3 id={titleId}>{a.preview.title}</h3>
      <p id={descId} className="muted">
        For task “{a.task_intent.slice(0, 80)}”. {a.preview.side_effects} Expires {relative(a.expires_at)}
        {" "}(<time dateTime={a.expires_at}>{new Date(a.expires_at).toLocaleString()}</time>).
      </p>
      <Fields fields={a.preview.fields} />
      <div className="row">
        <button className="primary" disabled={busy} onClick={() => decide("allow_once")}>Allow once</button>
        <button disabled={busy} onClick={() => decide("deny")}>Deny</button>
      </div>
      {error && <p role="alert" className="error">{error}</p>}
    </article>
  );
}

export function Approvals({ tick, announce }: { tick: number; announce: (s: string) => void }) {
  const [items, setItems] = useState<Approval[]>([]);
  const [unknown, setUnknown] = useState<Action[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const refresh = useCallback(async () => {
    setItems(await api2.approvals());
    setUnknown(await api2.unknownActions());
  }, []);
  useEffect(() => { void refresh(); }, [refresh, tick]);

  const run = async (key: string, fn: () => Promise<string>) => {
    setError(""); setBusy(key);
    try { announce(await fn()); await refresh(); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Action failed"); }
    finally { setBusy(""); }
  };

  return (
    <section aria-labelledby="approvals-h">
      <h2 id="approvals-h">Approvals</h2>
      {error && <p role="alert" className="error">{error}</p>}
      {items.length === 0 && <p>Nothing is waiting for your approval.</p>}
      {items.map((a) => <ApprovalCard key={a.id} a={a} announce={announce} onDone={refresh} />)}

      <h2 id="unknown-h">Needs checking</h2>
      {unknown.length === 0 && <p>No changes with an unknown outcome.</p>}
      {unknown.map((x) => (
        <article key={x.id} className="card unknown" aria-labelledby={`u-${x.id}`}>
          <h3 id={`u-${x.id}`}>{x.preview.title}: outcome unknown</h3>
          <p>The connection dropped after this was sent to the provider, so it may or may not have happened. It will not be retried automatically.</p>
          <Fields fields={x.preview.fields} />
          <div className="row">
            <button disabled={!!busy} onClick={() => run(x.id, async () => {
              const r = await api2.reconcile(x.id);
              return r.state === "unknown" ? "Could not confirm automatically. Please check the account and answer below." : `Checked: the change ${r.state === "succeeded" ? "did happen" : "did not happen"}.`;
            })}>Check with provider</button>
            <button disabled={!!busy} onClick={() => run(x.id, async () => { await api2.resolve(x.id, "happened"); return "Marked as happened."; })}>It happened</button>
            <button disabled={!!busy} onClick={() => run(x.id, async () => { await api2.resolve(x.id, "did_not_happen"); return "Marked as did not happen. A new attempt will need a new approval."; })}>It did not happen</button>
          </div>
        </article>
      ))}
    </section>
  );
}
