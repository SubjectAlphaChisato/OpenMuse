import { useCallback, useEffect, useState } from "react";
import { api2, ApiError, Automation, AutomationKind, AutomationPreview, automationsApi, AutomationSpec, Connection, Me } from "../api";

const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];
const PAUSE_TEXT: Record<string, string> = {
  connection_disconnected: "Paused: an account it uses was disconnected. Edit it or delete it.",
  reauth_required: "Paused: an account it uses needs reconnecting. It resumes after you reconnect.",
  connection_error: "Paused: an account it uses has an error.",
};
const STATE_TEXT: Record<string, string> = {
  started: "ran", pending: "waiting for the previous run", coalesced: "merged into a later run", missed_paused: "skipped while paused", cancelled: "cancelled",
};

function Form({ me, conns, announce, onDone }: { me: Me; conns: Connection[]; announce: (s: string) => void; onDone: () => void }) {
  const [kind, setKind] = useState<AutomationKind>("weekly");
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [time, setTime] = useState("18:00");
  const [date, setDate] = useState("");
  const [days, setDays] = useState<string[]>(["mon", "tue", "wed", "thu", "fri"]);
  const [zone, setZone] = useState(me.time_zone);
  const [accounts, setAccounts] = useState<string[]>([]);
  const [calConn, setCalConn] = useState("");
  const [preview, setPreview] = useState<AutomationPreview | null>(null);
  const [error, setError] = useState("");

  const spec = (): AutomationSpec => ({
    name, kind, prompt, time_zone: zone || null,
    local_time: kind === "calendar_change" ? null : time, weekdays: kind === "weekly" ? days : null, run_date: kind === "once" ? date : null,
    connection_ids: accounts, calendar: kind === "calendar_change" ? { connection_id: calConn, calendar_id: "primary" } : null,
  });
  const change = <T,>(set: (v: T) => void) => (v: T) => { set(v); setPreview(null); };

  return (
    <form className="card" aria-labelledby="new-auto-h" onSubmit={async (e) => {
      e.preventDefault(); setError("");
      try {
        if (!preview) { setPreview(await automationsApi.preview(spec())); return; }
        await automationsApi.create(spec()); announce(`Automation “${name}” is active.`); onDone();
      } catch (err) { setError(err instanceof ApiError ? err.message : "Could not save"); setPreview(null); }
    }}>
      <h3 id="new-auto-h">New automation</h3>
      <label className="block">Name <input value={name} required maxLength={120} onChange={(e) => change(setName)(e.target.value)} /></label>
      <label className="block">What should happen each time?
        <textarea rows={3} required value={prompt} onChange={(e) => change(setPrompt)(e.target.value)} /></label>
      <label className="block">When{" "}
        <select value={kind} onChange={(e) => change(setKind)(e.target.value as AutomationKind)}>
          <option value="once">Once</option><option value="daily">Every day</option>
          <option value="weekly">On chosen weekdays</option><option value="calendar_change">When tomorrow's calendar changes</option>
        </select>
      </label>
      {kind === "once" && <label className="block">Date <input type="date" required value={date} onChange={(e) => change(setDate)(e.target.value)} /></label>}
      {kind === "weekly" && (
        <fieldset><legend>Weekdays</legend>
          {DAYS.map(([v, l]) => (
            <label key={v} className="inline"><input type="checkbox" checked={days.includes(v)}
              onChange={(e) => change(setDays)(e.target.checked ? [...days, v] : days.filter((d) => d !== v))} /> {l}</label>
          ))}
        </fieldset>
      )}
      {kind !== "calendar_change" && (
        <>
          <label className="block">Time (24-hour) <input type="time" required value={time} onChange={(e) => change(setTime)(e.target.value)} /></label>
          <label className="block">Time zone <input value={zone} onChange={(e) => change(setZone)(e.target.value)} aria-describedby="tz-help" /></label>
          <p id="tz-help" className="muted">An IANA zone such as America/Los_Angeles. Changing your profile zone later does not move this schedule.</p>
        </>
      )}
      {kind === "calendar_change" && (
        <label className="block">Calendar account{" "}
          <select required value={calConn} onChange={(e) => change(setCalConn)(e.target.value)}>
            <option value="">Choose…</option>
            {conns.filter((c) => (c.modes.calendar ?? "off") !== "off").map((c) => <option key={c.id} value={c.id}>{c.account_label}</option>)}
          </select>
        </label>
      )}
      <fieldset><legend>Accounts each run may use</legend>
        {conns.length === 0 && <p className="muted">No connected accounts. Runs can still write notes and files.</p>}
        {conns.map((c) => (
          <label key={c.id} className="inline"><input type="checkbox" checked={accounts.includes(c.id)}
            onChange={(e) => change(setAccounts)(e.target.checked ? [...accounts, c.id] : accounts.filter((x) => x !== c.id))} /> {c.account_label}</label>
        ))}
      </fieldset>
      {preview && (
        <div role="status" className="notice">
          <strong>{preview.description}</strong>
          {preview.upcoming.length > 0 && <ul>{preview.upcoming.map((u) => <li key={u.at}>{u.local}</li>)}</ul>}
          <p>Sending email or changing events will still ask for your approval each time.</p>
        </div>
      )}
      {error && <p role="alert" className="error">{error}</p>}
      <div className="row">
        <button type="submit" className="primary">{preview ? "Confirm and activate" : "Preview schedule"}</button>
        <button type="button" onClick={onDone}>Cancel</button>
      </div>
    </form>
  );
}

export function Automations({ me, tick, announce }: { me: Me; tick: number; announce: (s: string) => void }) {
  const [items, setItems] = useState<Automation[]>([]);
  const [conns, setConns] = useState<Connection[]>([]);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => { setItems(await automationsApi.list()); setConns(await api2.connections()); }, []);
  useEffect(() => { void refresh(); }, [refresh, tick]);
  const run = async (fn: () => Promise<string>) => {
    setError("");
    try { announce(await fn()); } catch (e) { setError(e instanceof ApiError ? e.message : "Action failed"); }
    await refresh();
  };
  const label = (id: string) => conns.find((c) => c.id === id)?.account_label ?? "a removed account";
  const pending = items.filter((a) => !a.confirmed);
  const active = items.filter((a) => a.confirmed);

  return (
    <section aria-labelledby="auto-h">
      <h2 id="auto-h">Automations</h2>
      {error && <p role="alert" className="error">{error}</p>}
      {pending.map((a) => (
        <article key={a.id} className="card approval" aria-label={`Proposed: ${a.name}`}>
          <h3>Proposed by the assistant: {a.name}</h3>
          <p><strong>{a.description}</strong></p>
          <p>Each time: {a.prompt}</p>
          <p className="muted">Accounts: {a.connection_ids.length ? a.connection_ids.map(label).join(", ") : "none"}</p>
          <div className="row">
            <button className="primary" onClick={() => run(async () => { await automationsApi.confirm(a); return `Confirmed “${a.name}”.`; })}>Confirm</button>
            <button onClick={() => run(async () => { await automationsApi.remove(a); return `Discarded “${a.name}”.`; })}>Discard</button>
          </div>
        </article>
      ))}
      {adding ? <Form me={me} conns={conns} announce={announce} onDone={() => { setAdding(false); void refresh(); }} />
        : <button onClick={() => setAdding(true)}>New automation</button>}
      {active.length === 0 && !adding && <p>No automations yet.</p>}
      {active.map((a) => (
        <article key={a.id} className="card" aria-label={a.name}>
          <h3>{a.name}</h3>
          <p><strong>{a.description}</strong></p>
          <p>Each time: {a.prompt}</p>
          {a.paused_reason && <p role="alert" className="error">{PAUSE_TEXT[a.paused_reason] ?? `Paused: ${a.paused_reason}`}</p>}
          <dl className="fields">
            <div className="field"><dt>Status</dt><dd>{a.completed_at ? "Finished" : a.enabled ? "Active" : "Off"}</dd></div>
            {a.kind !== "calendar_change" && <div className="field"><dt>Next run</dt><dd>{a.enabled && a.next_run_local ? a.next_run_local : "—"}</dd></div>}
            <div className="field"><dt>Last run</dt><dd>{a.last_run_local ?? "—"}</dd></div>
            {a.trigger_status && <div className="field"><dt>Watching</dt><dd>
              {a.trigger_status.initialized ? `last checked ${a.trigger_status.last_poll_at ? new Date(a.trigger_status.last_poll_at).toLocaleString() : "—"}` : "starting"}
              {a.trigger_status.pending_changes > 0 && ` · ${a.trigger_status.pending_changes} change(s) about to run`}
              {a.trigger_status.last_error && ` · problem: ${a.trigger_status.last_error}`}</dd></div>}
            <div className="field"><dt>Accounts</dt><dd>{a.connection_ids.length ? a.connection_ids.map(label).join(", ") : "none"}</dd></div>
          </dl>
          {a.recent.length > 0 && (
            <details><summary>Recent runs</summary>
              <ul>{a.recent.map((o) => (
                <li key={o.scheduled_for}>{o.local}: {STATE_TEXT[o.state] ?? o.state}
                  {o.late && ", late"}{o.missed_count > 0 && `, covered ${o.missed_count} missed run(s)`}{o.changes > 0 && `, ${o.changes} calendar change(s)`}</li>
              ))}</ul>
            </details>
          )}
          <div className="row">
            {!a.completed_at && <button onClick={() => run(async () => { await automationsApi.setEnabled(a, !a.enabled); return `${a.name} turned ${a.enabled ? "off" : "on"}.`; })}>
              {a.enabled ? "Turn off" : "Turn on"}</button>}
            <button onClick={() => { if (confirm(`Delete “${a.name}”? Any run in progress is cancelled.`)) void run(async () => { await automationsApi.remove(a); return `Deleted “${a.name}”.`; }); }}>Delete</button>
          </div>
        </article>
      ))}
    </section>
  );
}
