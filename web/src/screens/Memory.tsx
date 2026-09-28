import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Conversation } from "../api";
import { errorText, memory, MemoryDoc, MemoryItem } from "../apiM4";

const parseTags = (s: string) => s.split(",").map((t) => t.trim().toLowerCase()).filter(Boolean);

export function Memory({ tick, announce }: { tick: number; announce: (s: string) => void }) {
  const [doc, setDoc] = useState<MemoryDoc | null>(null);
  const [convs, setConvs] = useState<Conversation[]>([]);
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const [tags, setTags] = useState("");
  const [scope, setScope] = useState("global");
  const [md, setMd] = useState<{ text: string; version: string } | null>(null);
  const [conflict, setConflict] = useState(false);

  const refresh = useCallback(async () => { setDoc(await memory.get()); }, []);
  useEffect(() => { void refresh().catch(() => undefined); }, [refresh, tick]);
  useEffect(() => { void api.conversations().then(setConvs).catch(() => undefined); }, []);

  const scopeLabel = (s: string) => s === "global" ? "All conversations" : `Only in “${convs.find((c) => c.id === s)?.title ?? "a deleted conversation"}”`;
  const run = async (fn: () => Promise<string>) => {
    setError("");
    try { announce(await fn()); await refresh(); } catch (e) { setError(errorText(e, "That did not work")); await refresh(); }
  };

  return (
    <section aria-labelledby="mem-h" className="memory">
      <h2 id="mem-h">Memory</h2>
      <p className="muted">The assistant uses only items you saved or accepted. Items tagged <code>style</code> or <code>identity</code> are always included;
        others are included when they match the request. Memory is context, never permission to act.</p>
      {error && <p role="alert" className="error">{error}</p>}

      {doc && doc.suggestions.length > 0 && (
        <section aria-labelledby="sug-h" className="card">
          <h3 id="sug-h">Suggestions waiting for you ({doc.suggestions.length})</h3>
          <p className="muted">The assistant inferred these. They are not used until you accept them.</p>
          <ul className="plain">
            {doc.suggestions.map((m) => (
              <li key={m.id}>
                <p>{m.text} {m.tags.length > 0 && <span className="muted">[{m.tags.join(", ")}]</span>}</p>
                {typeof m.source_ref.reason === "string" && <p className="muted">Why: {m.source_ref.reason as string}</p>}
                <button onClick={() => run(async () => { await memory.accept(m); return "Suggestion accepted."; })} aria-label={`Accept: ${m.text}`}>Accept</button>{" "}
                <button onClick={() => run(async () => { await memory.forget(m); return "Suggestion rejected."; })} aria-label={`Reject: ${m.text}`}>Reject</button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <h3>Remembered</h3>
      {doc && doc.items.length === 0 && <p>Nothing remembered yet.</p>}
      <ul className="plain mem-list">
        {doc?.items.map((m) => <MemoryRow key={`${m.id}:${m.version}`} m={m} scopeLabel={scopeLabel(m.scope)} run={run} />)}
      </ul>
      {doc && <p className="muted">{doc.forget_note}</p>}

      <form className="card" aria-labelledby="add-h" onSubmit={(e) => {
        e.preventDefault();
        if (!text.trim()) { setError("Write what to remember."); return; }
        void run(async () => { await memory.add(text, scope, parseTags(tags)); setText(""); setTags(""); return "Saved to memory."; });
      }}>
        <h3 id="add-h">Add</h3>
        <label className="block">What to remember<input value={text} onChange={(e) => setText(e.target.value)} maxLength={1000} /></label>
        <label className="block">Tags (comma-separated, optional; e.g. style, identity)<input value={tags} onChange={(e) => setTags(e.target.value)} /></label>
        <label className="block">Applies to{" "}
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="global">All conversations</option>
            {convs.map((c) => <option key={c.id} value={c.id}>Only in “{c.title}”</option>)}
          </select>
        </label>
        <button type="submit">Save</button>
      </form>

      <section aria-labelledby="md-h" className="card">
        <h3 id="md-h">Edit as Markdown</h3>
        <p className="muted">One item per line starting with “- ”. Tags go in brackets: <code>- [style] Keep replies short.</code> Deleting a line forgets that item.
          Keep the hidden <code>&lt;!-- m:… --&gt;</code> markers so edits apply to the right item.</p>
        {!md ? (
          <button onClick={() => doc && setMd({ text: doc.markdown, version: doc.version })} disabled={!doc}>Open Markdown editor</button>
        ) : (
          <form onSubmit={async (e) => {
            e.preventDefault(); setError("");
            try {
              const next = await memory.save(md.text, md.version);
              setDoc(next); setMd(null); setConflict(false); announce("Memory saved.");
            } catch (err) {
              if (err instanceof ApiError && err.status === 409) setConflict(true);
              else setError(errorText(err, "Could not save"));
            }
          }}>
            <label className="block">Memory (Markdown)
              <textarea rows={14} value={md.text} onChange={(e) => setMd({ ...md, text: e.target.value })} spellCheck />
            </label>
            {conflict && (
              <p role="alert" className="error">Your memory changed since you opened the editor, so nothing was saved.
                Your text is still here; reload to get the latest, then apply your edits again.</p>
            )}
            <button type="submit">Save</button>{" "}
            {conflict && <button type="button" onClick={async () => { const d = await memory.get(); setDoc(d); setMd({ text: d.markdown, version: d.version }); setConflict(false); }}>Reload latest</button>}{" "}
            <button type="button" onClick={() => { setMd(null); setConflict(false); }}>Close editor</button>
          </form>
        )}
      </section>
    </section>
  );
}

function MemoryRow({ m, scopeLabel, run }: { m: MemoryItem; scopeLabel: string; run: (fn: () => Promise<string>) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(m.text);
  const [tags, setTags] = useState(m.tags.join(", "));
  const source = m.source_ref.type === "owner_message" ? "You asked in chat" : m.source_ref.type === "memory_editor" ? "Added in Memory"
    : m.kind === "inference" ? "Suggested by the assistant, accepted by you" : String(m.source_ref.type ?? "unknown");
  return (
    <li className="card">
      {!editing ? (
        <p>{m.text} {m.tags.length > 0 && <span className="muted">[{m.tags.join(", ")}]</span>}</p>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); void run(async () => { await memory.edit(m, text, parseTags(tags)); setEditing(false); return "Memory updated."; }); }}>
          <label className="block">Text<input value={text} onChange={(e) => setText(e.target.value)} maxLength={1000} /></label>
          <label className="block">Tags<input value={tags} onChange={(e) => setTags(e.target.value)} /></label>
          <button type="submit">Save</button>{" "}<button type="button" onClick={() => setEditing(false)}>Cancel</button>
        </form>
      )}
      <details>
        <summary>Details</summary>
        <dl className="meta">
          <dt>Applies to</dt><dd>{scopeLabel}</dd>
          <dt>Source</dt><dd>{source}{typeof m.source_ref.excerpt === "string" && <> — “{(m.source_ref.excerpt as string).slice(0, 160)}”</>}</dd>
          <dt>Saved</dt><dd><time dateTime={m.created_at}>{new Date(m.created_at).toLocaleString()}</time></dd>
          <dt>Last changed</dt><dd><time dateTime={m.updated_at}>{new Date(m.updated_at).toLocaleString()}</time> (version {m.version})</dd>
        </dl>
      </details>
      {!editing && <button onClick={() => setEditing(true)} aria-label={`Edit: ${m.text}`}>Edit</button>}{" "}
      <button onClick={() => { if (confirm(`Forget “${m.text}”?`)) void run(async () => { await memory.forget(m); return "Forgotten."; }); }} aria-label={`Forget: ${m.text}`}>Forget</button>
    </li>
  );
}
