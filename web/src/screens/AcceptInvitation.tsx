import { useEffect, useState } from "react";
import { api, api2, ApiError, InvitationPreview } from "../api";

// CRM: /invite/{token}. The invitee has no account yet: preview the invitation, choose a password, then sign in.
const ROLE: Record<string, string> = { admin: "管理员", advisor: "顾问", partner: "合伙人" };

export function AcceptInvitation({ token, onDone }: { token: string; onDone: () => void }) {
  const [preview, setPreview] = useState<InvitationPreview | null>(null);
  const [missing, setMissing] = useState(false);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api2.invitation(token).then((p) => { setPreview(p); setName(p.name); }).catch(() => setMissing(true));
  }, [token]);

  if (missing) {
    return (
      <main className="center">
        <div className="card login"><h1>邀请链接不可用</h1><p className="muted">它可能已过期、已使用，或已被撤回。请联系邀请你的人重新发送。</p></div>
      </main>
    );
  }
  if (!preview) return <p role="status" className="center">Loading…</p>;
  return (
    <main className="center">
      <form className="card login" onSubmit={async (e) => {
        e.preventDefault(); setError("");
        if (password !== again) { setError("两次输入的密码不一致。"); return; }
        setBusy(true);
        try {
          await api2.acceptInvitation(token, password, name.trim() || null);
          await api.login(preview.email, password);
          window.history.replaceState(null, "", "/");
          onDone();
        } catch (err) {
          setError(err instanceof ApiError ? err.message : "没有成功，请再试一次。");
        } finally {
          setBusy(false);
        }
      }}>
        <h1>加入 {preview.organization}</h1>
        <p className="muted">你将以<strong>{ROLE[preview.role] ?? preview.role}</strong>身份加入，登录邮箱 <strong>{preview.email}</strong>。</p>
        <label>姓名<input value={name} maxLength={100} onChange={(e) => setName(e.target.value)} /></label>
        <label>设置密码（至少 12 位）<input type="password" autoComplete="new-password" minLength={12} value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        <label>再输一次<input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} required /></label>
        {error && <p role="alert" className="error">{error}</p>}
        <button type="submit" className="primary" disabled={busy}>加入并登录</button>
      </form>
    </main>
  );
}
