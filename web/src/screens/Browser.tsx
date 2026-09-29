import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, browserApi, BrowserSession } from "../api";

const KEYS = ["Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "PageDown", "PageUp"];

function LiveView({ s, announce }: { s: BrowserSession; announce: (m: string) => void }) {
  const [frame, setFrame] = useState("");
  const [status, setStatus] = useState("Connecting to the live view…");
  const [text, setText] = useState("");
  const ws = useRef<WebSocket | null>(null);
  const img = useRef<HTMLImageElement>(null);

  useEffect(() => {
    const sock = new WebSocket(browserApi.streamUrl(s.id));
    ws.current = sock;
    sock.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.t === "frame") { setFrame(`data:image/jpeg;base64,${msg.jpeg}`); setStatus(msg.url ? `Showing ${msg.url}` : "Live"); }
      if (msg.t === "ended") setStatus("This browser session has ended.");
      if (msg.t === "unavailable") setStatus("The browser is not responding.");
      if (msg.t === "error") announce(msg.message);
    };
    sock.onclose = () => setStatus((cur) => (cur.startsWith("This browser") ? cur : "Live view disconnected."));
    return () => sock.close();
  }, [s.id, announce]);

  const send = (event: Record<string, unknown>) => ws.current?.send(JSON.stringify(event));
  const driving = s.control === "user" && s.state === "active";

  return (
    <div className="live">
      <p role="status" aria-live="polite" className="muted">{status}</p>
      {frame && (
        <img ref={img} src={frame} alt={`Live view of the assistant's browser${s.current_url ? ` at ${s.current_url}` : ""}`}
          className={driving ? "driving" : ""}
          onClick={(e) => {
            if (!driving || !img.current) return;
            const r = img.current.getBoundingClientRect();
            send({ type: "click", x: ((e.clientX - r.left) / r.width) * 1280, y: ((e.clientY - r.top) / r.height) * 800 });
          }} />
      )}
      {driving && (
        <form className="row" onSubmit={(e) => { e.preventDefault(); if (text) { send({ type: "type", text }); setText(""); } }}>
          <label>Type into the page <input value={text} onChange={(e) => setText(e.target.value)} /></label>
          <button type="submit">Type</button>
          {KEYS.map((k) => <button key={k} type="button" onClick={() => send({ type: "key", key: k })}>{k}</button>)}
          <button type="button" onClick={() => send({ type: "wheel", dy: 500 })}>Scroll down</button>
          <button type="button" onClick={() => send({ type: "wheel", dy: -500 })}>Scroll up</button>
        </form>
      )}
    </div>
  );
}

export function Browser({ tick, announce }: { tick: number; announce: (m: string) => void }) {
  const [items, setItems] = useState<BrowserSession[]>([]);
  const [open, setOpen] = useState<string>("");
  const [error, setError] = useState("");
  const refresh = useCallback(async () => setItems(await browserApi.list()), []);
  useEffect(() => { void refresh(); }, [refresh, tick]);
  const run = async (fn: () => Promise<string>) => {
    setError("");
    try { announce(await fn()); } catch (e) { setError(e instanceof ApiError ? e.message : "Action failed"); }
    await refresh();
  };
  const active = items.filter((s) => s.state === "active");
  const past = items.filter((s) => s.state !== "active");

  return (
    <section aria-labelledby="browser-h">
      <h2 id="browser-h">Browser</h2>
      <p className="muted">The assistant can read public web pages only. It cannot sign in, fill in forms, buy anything, or reach private networks.
        Opening an address that would carry your private data needs your approval.</p>
      {error && <p role="alert" className="error">{error}</p>}
      {active.length === 0 && <p>No browser is open.</p>}
      {active.map((s) => (
        <article key={s.id} className="card" aria-label={`Browser for ${s.task_intent ?? "your own browsing"}`}>
          <h3>{s.task_intent ? `Task: ${s.task_intent.slice(0, 80)}` : "Your browser"}</h3>
          <p>{s.control === "user" ? "You are in control. The assistant is paused." : "The assistant is in control."}</p>
          <div className="row">
            {s.control === "agent"
              ? <button onClick={() => run(async () => { await browserApi.takeover(s.id); setOpen(s.id); return "You have control of the browser. The assistant is paused."; })}>Take control</button>
              : s.task_id && <button className="primary" onClick={() => run(async () => { await browserApi.resume(s.id); return "Control handed back. Cookies from your turn were cleared."; })}>Resume</button>}
            <button onClick={() => setOpen(open === s.id ? "" : s.id)} aria-expanded={open === s.id}>{open === s.id ? "Hide live view" : "Show live view"}</button>
            <button onClick={() => run(async () => { await browserApi.stop(s.id); return "Browser stopped. The task will continue without it."; })}>Stop</button>
          </div>
          {open === s.id && <LiveView s={s} announce={announce} />}
          <Visited s={s} />
        </article>
      ))}
      {past.length > 0 && <h3>Earlier sessions</h3>}
      {past.map((s) => (
        <article key={s.id} className="card" aria-label={`Ended browser for ${s.task_intent ?? "your own browsing"}`}>
          <p><strong>{s.task_intent ? s.task_intent.slice(0, 80) : "Your browser"}</strong> <span className="muted">ended {s.ended_at ? new Date(s.ended_at).toLocaleString() : ""}</span></p>
          <Visited s={s} />
        </article>
      ))}
    </section>
  );
}

function Visited({ s }: { s: BrowserSession }) {
  if (s.urls_visited.length === 0) return null;
  return (
    <details><summary>Pages visited ({s.urls_visited.length})</summary>
      <ol>{s.urls_visited.map((u, i) => (
        <li key={i}><time dateTime={u.at}>{new Date(u.at).toLocaleTimeString()}</time> {u.blocked ? "blocked: " : ""}{u.title ? `${u.title} — ` : ""}<span className="url">{u.url}</span></li>
      ))}</ol>
    </details>
  );
}
