import { useEffect, useState } from "react";
import { api2, ApiError, Connection, Me, Skill } from "../api";

// v0.2 harness: every capability is a skill the assistant loads when a request needs it.
const STATUS: Record<string, string> = {
  ready: "Ready", not_connected: "Not connected", off: "Turned off for the account", needs_reconnect: "Needs reconnecting",
  unavailable: "Unavailable", disabled: "Off",
};
const ASK: Record<string, string> = { not_connected: "connect", off: "turn on", needs_reconnect: "reconnect" };

const MODE: Record<string, string> = { off: "off", read_only: "read only", read_write: "read and propose changes" };
const ACCOUNT_STATUS: Record<string, string> = { connected: "Connected", reauth_required: "Needs reconnecting", error: "Error" };

export function Skills({ me, onMe, tick, announce }: { me: Me; onMe: (m: Me) => void; tick: number; announce: (msg: string) => void }) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [accounts, setAccounts] = useState<Connection[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api2.skills().then(setSkills).catch(() => setError("Could not load skills."));
    api2.connections().then(setAccounts).catch(() => undefined);
  }, [tick]);

  const toggle = async (s: Skill) => {
    setError("");
    try {
      const updated = await api2.setSkill(s.name, !s.enabled);
      setSkills((all) => all.map((x) => (x.name === s.name ? updated : x)));
      announce(`${s.title} ${updated.enabled ? "turned on" : "turned off"}.`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not change the skill.");
    }
  };

  return (
    <section aria-labelledby="skills-h" className="skills-page">
      <h2 id="skills-h">Skills and accounts</h2>
      <p className="muted">
        Accounts are managed in Chat. Ask, for example, “connect my Notion”, “let the assistant send email”, or “disconnect GitHub”;
        the assistant shows a card and nothing changes until you confirm it.
      </p>
      {error && <p role="alert" className="error">{error}</p>}

      <h3>Accounts</h3>
      {accounts.length === 0 ? <p className="muted">No accounts yet. Ask in Chat to connect one.</p> : (
        <ul className="accounts">
          {accounts.map((c) => (
            <li key={c.id}>
              <strong>{c.account_label}</strong>{c.is_demo && <span className="badge">DEMO</span>}{" "}
              <span className="muted">{ACCOUNT_STATUS[c.status] ?? c.status}</span>
              <div className="muted">{c.capabilities.map((k) => `${k.label}: ${MODE[c.modes[k.id] ?? "off"]}`).join(" · ")}</div>
              {c.warning && <div className="notice">{c.warning} Ask in Chat to reconnect.</div>}
            </li>
          ))}
        </ul>
      )}
      <label className="block">Ask before{" "}
        <select value={me.approval_mode} onChange={async (e) => {
          try { onMe(await api2.setApprovalMode(e.target.value as "ask_writes" | "always_ask")); announce("Approval setting saved."); }
          catch (err) { setError(err instanceof ApiError ? err.message : "Could not save."); }
        }}>
          <option value="ask_writes">any change (sending, creating, editing)</option>
          <option value="always_ask">every read and every change</option>
        </select>
      </label>

      <h3>Skills</h3>
      <p className="muted">The assistant loads a skill when a request needs it. Turning one off hides it from the assistant; account access stays as it is.</p>
      <ul className="skills">
        {skills.map((s) => (
          <li key={s.name}>
            <article className="skill-row" aria-label={`${s.title} skill`}>
              <div className="skill-main">
                <h4>{s.title} <span className={`chip k-${s.status}`}>{STATUS[s.status] ?? s.status}</span>{!s.builtin && <span className="chip">Your skill</span>}</h4>
                <p className="muted">{s.summary}</p>
                {s.detail && s.status !== "ready" && s.status !== "disabled" && (
                  <p className="muted small">{s.detail}{ASK[s.status] ? `. Ask in Chat to ${ASK[s.status]} it.` : ""}</p>
                )}
              </div>
              <label className="switch">
                <input type="checkbox" checked={s.enabled} onChange={() => toggle(s)} />
                <span className="sr-only">Let the assistant use {s.title}</span>
                <span aria-hidden="true">{s.enabled ? "On" : "Off"}</span>
              </label>
            </article>
          </li>
        ))}
      </ul>
    </section>
  );
}
