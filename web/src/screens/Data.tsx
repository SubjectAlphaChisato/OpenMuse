import { useCallback, useEffect, useState } from "react";
import { ApiError, dataApi, ExportInfo, Me } from "../api";

export function Data({ me, tick, announce }: { me: Me; tick: number; announce: (m: string) => void }) {
  const [exports, setExports] = useState<ExportInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const refresh = useCallback(async () => setExports(await dataApi.exports()), []);
  useEffect(() => { void refresh(); }, [refresh, tick]);

  return (
    <section aria-labelledby="data-h">
      <h2 id="data-h">Your data</h2>
      {error && <p role="alert" className="error">{error}</p>}
      <article className="card" aria-labelledby="export-h">
        <h3 id="export-h">Export</h3>
        <p>A ZIP file with your conversations, tasks, memory, automations, files, and activity, each described by a JSON Schema.
          Passwords, sign-in tokens, and account credentials are never included.</p>
        <button disabled={busy} onClick={async () => {
          setBusy(true); setError("");
          try { const e = await dataApi.createExport(); announce(`Export ready: ${e.file_name}.`); await refresh(); }
          catch (e) { setError(e instanceof ApiError ? e.message : "Export failed"); }
          finally { setBusy(false); }
        }}>Create export</button>
        <ul className="plain">{exports.map((e) => (
          <li key={e.id}><a href={dataApi.downloadUrl(e)} download>{e.file_name}</a> <span className="muted">{(e.size / 1024).toFixed(1)} KB, {new Date(e.created_at).toLocaleString()}</span></li>
        ))}</ul>
      </article>
      <article className="card danger" aria-labelledby="delete-h">
        <h3 id="delete-h">Delete all data</h3>
        <p>Cancels running work, disconnects and revokes your accounts, and permanently deletes your conversations, tasks, memory,
          automations, files, and exports. Your sign-in stays. Backups made earlier still contain your data until they are deleted.</p>
        <form onSubmit={async (e) => {
          e.preventDefault(); setError("");
          try {
            const r = await dataApi.deleteAll(confirm);
            const failed = r.revocations.filter((x) => !x.revoked);
            setResult(`Deleted. ${failed.length ? `Revocation failed for ${failed.map((f) => f.account).join(", ")}; remove access in that account's settings. ` : ""}${r.backup_note}`);
            setConfirm(""); announce("All data deleted."); await refresh();
          } catch (err) { setError(err instanceof ApiError ? err.message : "Delete failed"); }
        }}>
          <label className="block">Type your email address ({me.email}) to confirm
            <input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" /></label>
          <button type="submit" disabled={confirm.trim().toLowerCase() !== me.email.toLowerCase()}>Delete all data</button>
        </form>
        {result && <p role="status">{result}</p>}
      </article>
    </section>
  );
}
