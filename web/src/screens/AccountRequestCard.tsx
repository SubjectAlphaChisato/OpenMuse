import { useState } from "react";
import { AccountRequest, api2, ApiError, Me } from "../api";

// CRM: an account change the assistant asked for. Only an admin's click here adds access; reductions by an admin
// already happened and the card just records them.
const STATUS: Record<string, string> = {
  completed: "已完成", declined: "已拒绝", expired: "已过期：需要的话请再说一次", superseded: "已被更新的请求替代", failed: "失败",
};

export function AccountRequestCard({ req, me, onChanged }: { req: AccountRequest; me: Me; onChanged: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [inviteUrl, setInviteUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const open = req.status === "pending";
  const isAdmin = me.role === "admin";

  const act = async (confirm: boolean) => {
    setBusy(true); setError("");
    try {
      if (!confirm) { await api2.declineAccountRequest(req.id); onChanged(`已拒绝：${req.title}。`); return; }
      const res = await api2.confirmAccountRequest(req.id);
      if (res.invite_url) setInviteUrl(res.invite_url); // shown once; the server keeps only a hash
      onChanged(`${req.title}：${res.request.result ?? "已完成"}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "没有成功，请再试一次。");
    } finally {
      setBusy(false);
    }
  };
  const copy = async () => {
    try { await navigator.clipboard.writeText(inviteUrl!); setCopied(true); }
    catch { (document.getElementById(`invite-${req.id}`) as HTMLInputElement | null)?.select(); }
  };

  return (
    <article className={`card request r-${req.status}`} aria-label={req.title}>
      <h4>{req.title}</h4>
      {req.reason && <p className="muted">助理：“{req.reason}”</p>}
      {req.lines.length > 0 && <ul className="changes">{req.lines.map((l) => <li key={l}>{l}</li>)}</ul>}
      {req.note && <p className="muted">{req.note}</p>}
      {inviteUrl ? (
        <div className="row">
          <label className="grow"><span className="sr-only">邀请链接</span>
            <input id={`invite-${req.id}`} readOnly value={inviteUrl} onFocus={(e) => e.currentTarget.select()} />
          </label>
          <button className="primary" onClick={copy}>{copied ? "已复制" : "复制邀请链接"}</button>
          <p className="muted small">链接只显示这一次，7 天内有效。</p>
        </div>
      ) : open ? (
        isAdmin ? (
          <div className="actions">
            <button className="primary" disabled={busy} onClick={() => act(true)}>{req.confirm_label}</button>
            <button disabled={busy} onClick={() => act(false)}>暂不</button>
          </div>
        ) : (
          <p className="status" role="status">等待管理员确认。{me.email === req.requested_by && <button className="link" disabled={busy} onClick={() => act(false)}>撤回</button>}</p>
        )
      ) : (
        <p className="status" role="status">{STATUS[req.status] ?? req.status}{req.result && req.status === "completed" ? `：${req.result}` : ""}</p>
      )}
      {error && <p role="alert" className="error">{error}</p>}
    </article>
  );
}
