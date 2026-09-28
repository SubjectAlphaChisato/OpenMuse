import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { Artifact, ArtifactVersion, downloadUrl, errorText, files, formatSize, Preview } from "../apiM4";

const TYPE_LABEL: Record<string, string> = {
  "text/markdown": "Markdown", "text/plain": "Text", "text/csv": "CSV", "application/pdf": "PDF",
};
const ORIGIN_LABEL: Record<string, string> = { upload: "Uploaded", generated: "Made by the assistant", owner: "Made by you" };
const EDITABLE = new Set(["text/markdown", "text/plain", "text/csv"]);

export function Files({ tick, announce, focusId }: { tick: number; announce: (s: string) => void; focusId?: string }) {
  const [items, setItems] = useState<Artifact[]>([]);
  const [selected, setSelected] = useState<string>(focusId ?? "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [newName, setNewName] = useState("");
  const [newBody, setNewBody] = useState("");

  const refresh = useCallback(async () => { setItems(await files.list()); }, []);
  useEffect(() => { void refresh().catch(() => undefined); }, [refresh, tick]);
  useEffect(() => { if (focusId) setSelected(focusId); }, [focusId]);

  const run = async (fn: () => Promise<string>) => {
    setError(""); setBusy(true);
    try { announce(await fn()); await refresh(); }
    catch (e) { setError(errorText(e, "That did not work")); }
    finally { setBusy(false); }
  };

  return (
    <section aria-labelledby="files-h" className="files">
      <h2 id="files-h">Files</h2>
      <p className="muted">Uploads and documents the assistant made. Every change creates a new version; earlier versions stay available.
        Only you can open or download them.</p>
      {error && <p role="alert" className="error">{error}</p>}

      <div className="two-col">
        <form className="card" aria-labelledby="upload-h" onSubmit={(e) => {
          e.preventDefault();
          const f = fileRef.current?.files?.[0];
          if (!f) { setError("Choose a file first."); return; }
          void run(async () => {
            const a = await files.upload(f);
            if (fileRef.current) fileRef.current.value = "";
            setSelected(a.id);
            return `Uploaded ${a.name}.`;
          });
        }}>
          <h3 id="upload-h">Upload</h3>
          <label className="block">File (TXT, MD, CSV, or PDF with text; up to 20 MiB)
            <input ref={fileRef} type="file" accept=".txt,.text,.md,.markdown,.csv,.pdf,text/plain,text/markdown,text/csv,application/pdf" />
          </label>
          <p className="muted">Scanned PDFs (images of text) are not supported.</p>
          <button type="submit" disabled={busy}>Upload</button>
        </form>

        <form className="card" aria-labelledby="new-h" onSubmit={(e) => {
          e.preventDefault();
          if (!newName.trim()) { setError("Give the document a name."); return; }
          void run(async () => {
            const a = await files.createMarkdown(newName, newBody);
            setNewName(""); setNewBody(""); setSelected(a.id);
            return `Created ${a.name}.`;
          });
        }}>
          <h3 id="new-h">New Markdown document</h3>
          <label className="block">Name<input value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={200} /></label>
          <label className="block">Content<textarea rows={4} value={newBody} onChange={(e) => setNewBody(e.target.value)} /></label>
          <button type="submit" disabled={busy}>Create</button>
        </form>
      </div>

      <table>
        <caption className="sr-only">Your files</caption>
        <thead><tr><th scope="col">Name</th><th scope="col">Type</th><th scope="col">Version</th><th scope="col">Source</th><th scope="col">Updated</th><th scope="col">Actions</th></tr></thead>
        <tbody>
          {items.map((a) => (
            <tr key={a.id} className={selected === a.id ? "selected" : undefined}>
              <th scope="row">
                <button className="link" aria-expanded={selected === a.id} aria-controls="file-detail" onClick={() => setSelected(selected === a.id ? "" : a.id)}>
                  {a.name}
                </button>
              </th>
              <td>{TYPE_LABEL[a.mime] ?? a.mime}</td>
              <td>v{a.current_version}</td>
              <td>{ORIGIN_LABEL[a.origin] ?? a.origin}</td>
              <td><time dateTime={a.updated_at}>{new Date(a.updated_at).toLocaleString()}</time></td>
              <td className="actions">
                <a className="button" href={downloadUrl(a.id, a.current_version)} download aria-label={`Download ${a.name}, version ${a.current_version}`}>Download</a>
                <button disabled={busy} onClick={() => {
                  if (confirm(`Delete ${a.name} and all its versions? Pending emails that attach it will be cancelled.`)) {
                    void run(async () => { await files.remove(a.id); if (selected === a.id) setSelected(""); return `Deleted ${a.name}.`; });
                  }
                }} aria-label={`Delete ${a.name}`}>Delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {items.length === 0 && <p>No files yet.</p>}

      {selected && items.some((a) => a.id === selected) && (
        <FileDetail key={selected} artifact={items.find((a) => a.id === selected)!} tick={tick} announce={announce}
          onChanged={refresh} onError={setError} />
      )}
    </section>
  );
}

function FileDetail({ artifact, tick, announce, onChanged, onError }: {
  artifact: Artifact; tick: number; announce: (s: string) => void; onChanged: () => Promise<void>; onError: (s: string) => void;
}) {
  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [shown, setShown] = useState(artifact.current_version);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [base, setBase] = useState(artifact.current_version);
  const [conflict, setConflict] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => { headingRef.current?.focus(); }, []);
  useEffect(() => { void files.versions(artifact.id).then(setVersions).catch(() => undefined); }, [artifact.id, artifact.current_version, tick]);
  useEffect(() => { setShown(artifact.current_version); }, [artifact.current_version]);
  useEffect(() => { void files.preview(artifact.id, shown).then(setPreview).catch(() => setPreview(null)); }, [artifact.id, shown]);

  const v = versions.find((x) => x.version === shown);
  const disclosure = typeof v?.note?.disclosure === "string" ? (v.note.disclosure as string) : "";

  return (
    <article id="file-detail" className="card detail" aria-labelledby="file-detail-h">
      <h3 id="file-detail-h" tabIndex={-1} ref={headingRef}>{artifact.name}</h3>
      <label>Version{" "}
        <select value={shown} onChange={(e) => setShown(Number(e.target.value))}>
          {versions.map((x) => <option key={x.version} value={x.version}>v{x.version}{x.version === artifact.current_version ? " (current)" : ""} — {new Date(x.created_at).toLocaleString()}</option>)}
        </select>
      </label>{" "}
      <a className="button" href={downloadUrl(artifact.id, shown)} download>Download v{shown}</a>{" "}
      {artifact.mime === "text/markdown" && (
        <button onClick={async () => {
          try { const pdf = await files.renderPdf(artifact.id, shown); announce(`PDF ready: ${pdf.name}, version ${pdf.current_version}.`); await onChanged(); }
          catch (e) { onError(errorText(e, "Could not make a PDF")); }
        }}>Make PDF of v{shown}</button>
      )}{" "}
      {EDITABLE.has(artifact.mime) && !editing && (
        <button onClick={async () => {
          try { setDraft(await files.versionText(artifact.id, artifact.current_version)); setBase(artifact.current_version); setConflict(false); setEditing(true); }
          catch (e) { onError(errorText(e, "Could not open the file")); }
        }}>Edit</button>
      )}

      {v && (
        <dl className="meta">
          <dt>Size</dt><dd>{formatSize(v.size)}</dd>
          <dt>SHA-256</dt><dd><code>{v.sha256}</code></dd>
          <dt>Sources</dt><dd>{v.source_refs.length ? v.source_refs.map((r) => String(r.ref ?? r.type ?? JSON.stringify(r))).join(", ") : "—"}</dd>
          {typeof v.note.pages === "number" && (<><dt>Pages</dt><dd>{String(v.note.pages)}</dd></>)}
        </dl>
      )}
      {disclosure && <p className="notice" role="note">{disclosure}</p>}

      {editing && (
        <form onSubmit={async (e) => {
          e.preventDefault();
          try {
            const a = await files.edit(artifact.id, base, draft);
            setEditing(false); announce(`Saved ${a.name} as version ${a.current_version}.`); await onChanged();
          } catch (err) {
            if (err instanceof ApiError && err.status === 409) setConflict(true);
            else onError(errorText(err, "Could not save"));
          }
        }}>
          <label className="block">Edit {artifact.name} (based on version {base})
            <textarea rows={14} value={draft} onChange={(e) => setDraft(e.target.value)} />
          </label>
          {conflict && (
            <p role="alert" className="error">This file changed to a newer version while you were editing. Your text is kept here;
              copy what you need, then reload the latest version and apply it again.</p>
          )}
          <button type="submit">Save as new version</button>{" "}
          {conflict && <button type="button" onClick={async () => { setDraft(await files.versionText(artifact.id, artifact.current_version)); setBase(artifact.current_version); setConflict(false); }}>Reload latest</button>}{" "}
          <button type="button" onClick={() => setEditing(false)}>Cancel</button>
        </form>
      )}

      <h4>Preview of v{shown}</h4>
      {!preview && <p className="muted">No preview.</p>}
      {preview?.kind === "markdown" && preview.html !== null && (
        // Rendered on the server with raw HTML disabled, then sanitized with an allowlist (ART-01).
        <div className="preview markdown" dangerouslySetInnerHTML={{ __html: preview.html }} />
      )}
      {preview?.kind === "csv" && preview.rows && (
        <div className="preview scroll"><table><caption className="sr-only">CSV preview</caption><tbody>
          {preview.rows.map((row, i) => <tr key={i}>{row.map((c, j) => i === 0 ? <th key={j} scope="col">{c}</th> : <td key={j}>{c}</td>)}</tr>)}
        </tbody></table></div>
      )}
      {(preview?.kind === "text" || preview?.kind === "pdf") && (
        <pre className="preview">{preview.text}{preview.kind === "pdf" ? "\n\n(Text extracted from the PDF.)" : ""}</pre>
      )}
      {preview?.truncated && <p className="muted">Preview shortened; download for the full file.</p>}
    </article>
  );
}

export function ArtifactLinks({ ids, onOpen }: { ids: string[]; onOpen: (id: string) => void }) {
  if (!ids.length) return null;
  return (
    <p className="files-created">Files: {ids.map((id, i) => (
      <span key={id}>{i > 0 && ", "}<button className="link" onClick={() => onOpen(id)}>open file {i + 1}</button></span>
    ))}</p>
  );
}
