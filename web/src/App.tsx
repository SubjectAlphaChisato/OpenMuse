import { useCallback, useEffect, useRef, useState } from "react";
import { api, api2, ApiError, Conversation, Me, Message, Notification, setCsrf, subscribe, Task, TaskState } from "./api";
import { Approvals } from "./screens/Approvals";
import { Connections } from "./screens/Connections";
import { Automations } from "./screens/Automations"; // M5
import { Browser } from "./screens/Browser"; // M6
import { Data } from "./screens/Data"; // M6
import { ArtifactLinks, Files } from "./screens/Files"; // M4
import { Memory } from "./screens/Memory"; // M4

type Screen = "chat" | "tasks" | "approvals" | "automations" | "browser" | "notifications" | "connections" | "files" | "memory" | "data";

const STATE_LABEL: Record<TaskState, string> = {
  queued: "Queued", running: "Running", waiting_approval: "Needs approval", waiting_user: "Needs your reply",
  blocked: "Blocked", succeeded: "Done", failed: "Failed", cancelled: "Cancelled",
};

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);

  const load = useCallback(async () => {
    try {
      const m = await api.me();
      setCsrf(m.csrf_token);
      setMe(m);
    } catch {
      setMe(null);
    } finally {
      setChecked(true);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  if (!checked) return <p role="status" className="center">Loading…</p>;
  if (!me) return <Login onDone={load} />;
  return <Shell me={me} onLogout={async () => { await api.logout(); setMe(null); }} />;
}

function Login({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  return (
    <main className="center">
      <form className="card login" onSubmit={async (e) => {
        e.preventDefault(); setError("");
        try { await api.login(email, password); onDone(); }
        catch (err) { setError(err instanceof ApiError ? err.message : "Sign-in failed"); }
      }}>
        <h1>Sign in</h1>
        <label>Email<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {error && <p role="alert" className="error">{error}</p>}
        <button type="submit">Sign in</button>
      </form>
    </main>
  );
}

function Shell({ me: initialMe, onLogout }: { me: Me; onLogout: () => void }) {
  const [me, setMe] = useState(initialMe);
  const [notice] = useState(() => {
    // Returning from Google: /?oauth=connected|error&note=...&reason=...
    const q = new URLSearchParams(window.location.search);
    const oauth = q.get("oauth");
    if (!oauth) return null;
    window.history.replaceState(null, "", window.location.pathname);
    return oauth === "connected"
      ? { ok: true, text: `Google account connected.${q.get("note") ? " " + q.get("note") : ""}` }
      : { ok: false, text: q.get("reason") ?? "Google sign-in failed." };
  });
  const [screen, setScreen] = useState<Screen>(notice ? "connections" : "chat");
  const [pending, setPending] = useState(0);
  const [tick, setTick] = useState(0);
  const [live, setLive] = useState("");
  useEffect(() => subscribe((type) => {
    setTick((t) => t + 1);
    if (type === "task.succeeded") setLive("A task finished.");
    if (type === "task.waiting_user") setLive("A task needs your reply.");
    if (type === "task.blocked") setLive("A task is blocked.");
    if (type === "approval.requested") setLive("An action needs your approval.");
    if (type === "connection.reauth_required") setLive("A connected account needs reconnecting.");
  }), []);
  useEffect(() => { api2.approvals().then((a) => setPending(a.length)).catch(() => undefined); }, [tick]);
  const labels: Record<Screen, string> = { chat: "Chat", tasks: "Tasks", approvals: "Approvals", automations: "Automations", browser: "Browser", notifications: "Notifications", connections: "Connections", files: "Files", memory: "Memory", data: "Your data" };
  const [fileFocus, setFileFocus] = useState<string>(""); // M4: open a file from a chat message

  return (
    <div className="shell">
      <header className="top">
        <h1 className="brand">{me.assistant_name}</h1>
        {me.mode === "demo" && <span className="badge" aria-label="Demo mode: no real provider calls">DEMO</span>}
        <nav aria-label="Main">
          {(Object.keys(labels) as Screen[]).map((s) => (
            <button key={s} aria-current={screen === s ? "page" : undefined} onClick={() => setScreen(s)}
              aria-label={s === "approvals" && pending > 0 ? `Approvals, ${pending} waiting` : undefined}>
              {labels[s]}{s === "approvals" && pending > 0 && <span className="count" aria-hidden="true">{pending}</span>}
            </button>
          ))}
        </nav>
        <button className="link" onClick={onLogout}>Sign out</button>
      </header>
      {me.setup_required.length > 0 && (
        <div role="alert" className="setup">Setup needed: {me.setup_required.join("; ")}</div>
      )}
      <div role="status" aria-live="polite" className="sr-only">{live}</div>
      <main>
        {screen === "chat" && <Chat tick={tick} onOpenFile={(id) => { setFileFocus(id); setScreen("files"); }} />}
        {screen === "tasks" && <Tasks tick={tick} />}
        {screen === "approvals" && <Approvals tick={tick} announce={setLive} />}
        {screen === "notifications" && <Notifications tick={tick} />}
        {screen === "automations" && <Automations me={me} tick={tick} announce={setLive} />}
        {screen === "browser" && <Browser tick={tick} announce={setLive} />}
        {screen === "data" && <Data me={me} tick={tick} announce={setLive} />}
        {screen === "connections" && <Connections me={me} tick={tick} onMe={setMe} announce={setLive} notice={notice} />}
        {screen === "files" && <Files tick={tick} announce={setLive} focusId={fileFocus || undefined} />}
        {screen === "memory" && <Memory tick={tick} announce={setLive} />}
      </main>
    </div>
  );
}

function Chat({ tick, onOpenFile }: { tick: number; onOpenFile: (id: string) => void }) {
  const [convs, setConvs] = useState<Conversation[]>([]);
  const [active, setActive] = useState<string>("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [tasks, setTasks] = useState<Record<string, Task>>({});
  const [draft, setDraft] = useState("");
  const [replyTo, setReplyTo] = useState<string>("");
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => { api.conversations().then((c) => { setConvs(c); setActive((a) => a || c[0]?.id || ""); }); }, [tick]);
  useEffect(() => {
    if (!active) return;
    api.messages(active).then(setMessages);
    api.tasks().then((r) => setTasks(Object.fromEntries(r.items.map((t) => [t.id, t]))));
  }, [active, tick]);
  useEffect(() => { endRef.current?.scrollIntoView({ block: "end" }); }, [messages.length]);

  const waiting = Object.values(tasks).filter((t) => t.state === "waiting_user" && t.conversation_id === active);

  return (
    <section className="chat" aria-labelledby="chat-h">
      <h2 id="chat-h" className="sr-only">Chat</h2>
      <div className="conv-bar">
        <label>Conversation{" "}
          <select value={active} onChange={(e) => setActive(e.target.value)}>
            {convs.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}
          </select>
        </label>
        <button onClick={async () => {
          const title = prompt("Name for the side conversation");
          if (title) { const c = await api.createConversation(title); setConvs([...convs, c]); setActive(c.id); }
        }}>New side chat</button>
      </div>
      <ol className="messages" aria-label="Messages">
        {messages.map((m) => {
          const t = m.task_id ? tasks[m.task_id] : undefined;
          return (
            <li key={m.id} className={`msg ${m.role}`}>
              <h3 className="sr-only">{m.role === "user" ? "You" : m.role === "system" ? "System" : "Assistant"}</h3>
              <p>{m.content}</p>
              {t && m.role === "user" && <TaskChip task={t} />}
              <ArtifactLinks ids={m.artifact_ids} onOpen={onOpenFile} />
            </li>
          );
        })}
      </ol>
      <div ref={endRef} />
      <form className="composer" onSubmit={async (e) => {
        e.preventDefault(); setError("");
        if (!draft.trim() || !active) return;
        try { await api.send(active, draft, replyTo || undefined); setDraft(""); setReplyTo(""); setMessages(await api.messages(active)); }
        catch (err) { setError(err instanceof ApiError ? err.message : "Send failed"); }
      }}>
        {waiting.length > 0 && (
          // CHAT-01: never guess whether a message is a new task or a reply to existing work.
          <label>Send as{" "}
            <select value={replyTo} onChange={(e) => setReplyTo(e.target.value)}>
              <option value="">New task</option>
              {waiting.map((t) => <option key={t.id} value={t.id}>Reply to: {t.intent.slice(0, 60)}</option>)}
            </select>
          </label>
        )}
        <label className="grow"><span className="sr-only">Message</span>
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={2}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); (e.currentTarget.form as HTMLFormElement).requestSubmit(); } }} />
        </label>
        <button type="submit" disabled={!active}>Send</button>
        {error && <p role="alert" className="error">{error}</p>}
      </form>
    </section>
  );
}

function TaskChip({ task }: { task: Task }) {
  return <span className={`chip s-${task.state}`}>Task: {STATE_LABEL[task.state]}{task.blocker_detail ? ` — ${task.blocker_detail}` : ""}</span>;
}

function Tasks({ tick }: { tick: number }) {
  const [filter, setFilter] = useState("");
  const [items, setItems] = useState<Task[]>([]);
  const [error, setError] = useState("");
  const refresh = useCallback(() => api.tasks(filter || undefined).then((r) => setItems(r.items)), [filter]);
  useEffect(() => { void refresh(); }, [refresh, tick]);
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); await refresh(); } catch (e) { setError(e instanceof ApiError ? e.message : "Action failed"); } };

  return (
    <section aria-labelledby="tasks-h">
      <h2 id="tasks-h">Tasks</h2>
      <label>Filter by state{" "}
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">All</option>
          {Object.entries(STATE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {error && <p role="alert" className="error">{error}</p>}
      <table>
        <thead><tr><th scope="col">Task</th><th scope="col">State</th><th scope="col">Current step</th><th scope="col">Active time</th><th scope="col">Last activity</th><th scope="col">Actions</th></tr></thead>
        <tbody>
          {items.map((t) => (
            <tr key={t.id}>
              <td>{t.intent.slice(0, 80)}{t.retry_of && <small> (retry)</small>}</td>
              <td><TaskChip task={t} /></td>
              <td>{t.current_step ?? "—"}</td>
              <td>{(t.active_ms / 1000).toFixed(1)} s</td>
              <td>{t.last_activity_at ? new Date(t.last_activity_at).toLocaleString() : "—"}</td>
              <td className="actions">
                {!["succeeded", "failed", "cancelled"].includes(t.state) && <button onClick={() => act(() => api.cancel(t.id))}>Stop</button>}
                {["blocked", "waiting_user"].includes(t.state) && <button onClick={() => act(() => api.resume(t.id))}>Resume</button>}
                {["succeeded", "failed", "cancelled"].includes(t.state) && <button onClick={() => act(() => api.retry(t.id))}>Retry</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {items.length === 0 && <p>No tasks.</p>}
    </section>
  );
}

function Notifications({ tick }: { tick: number }) {
  const [items, setItems] = useState<Notification[]>([]);
  useEffect(() => { api.notifications().then(setItems); }, [tick]);
  return (
    <section aria-labelledby="notif-h">
      <h2 id="notif-h">Notifications</h2>
      <ul className="notifs">
        {items.map((n) => (
          <li key={n.id} className={n.read_at ? "read" : "unread"}>
            <strong>{n.title}</strong> <span>{n.body}</span>{" "}
            <time dateTime={n.created_at}>{new Date(n.created_at).toLocaleString()}</time>{" "}
            {!n.read_at && <button onClick={async () => { await api.markRead(n.id); setItems(await api.notifications()); }}>Mark read</button>}
          </li>
        ))}
      </ul>
      {items.length === 0 && <p>No notifications.</p>}
    </section>
  );
}
