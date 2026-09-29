// Thin typed client for the OPA API. Types mirror server/opa/api/schemas.py.
// TODO(M6): generate these from /api/openapi.json instead of maintaining by hand.

export type TaskState =
  | "queued" | "running" | "waiting_approval" | "waiting_user"
  | "blocked" | "succeeded" | "failed" | "cancelled";

export interface Me {
  id: string; email: string; time_zone: string; assistant_name: string;
  response_style: string; mode: "demo" | "live"; setup_required: string[]; csrf_token: string;
  approval_mode: "ask_writes" | "always_ask";
}
export interface Conversation { id: string; title: string; is_default: boolean; created_at: string }
export interface Message {
  id: string; seq: number; role: "user" | "assistant" | "system"; content: string;
  task_id: string | null; artifact_ids: string[]; created_at: string;
}
export interface Task {
  id: string; conversation_id: string; intent: string; state: TaskState;
  current_step: string | null; blocker_code: string | null; blocker_detail: string | null;
  retry_of: string | null; usage: Record<string, number>; active_ms: number;
  last_activity_at: string | null; finished_at: string | null; created_at: string;
  outputs: Record<string, { value: unknown; confirmed: boolean }>;
}
export interface Notification {
  id: string; type: string; title: string; body: string; task_id: string | null;
  read_at: string | null; created_at: string;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

let csrf = "";
export function setCsrf(token: string) { csrf = token; }
export function csrfToken() { return csrf; } // M4: raw-body uploads

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["content-type"] = "application/json";
  if (method !== "GET") headers["x-csrf-token"] = csrf;
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
  if (r.status === 204) return undefined as T;
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = data?.error ?? {};
    throw new ApiError(r.status, e.code ?? "error", e.message ?? r.statusText);
  }
  return data as T;
}

export const api = {
  login: (email: string, password: string) => req<void>("POST", "/api/auth/login", { email, password }),
  logout: () => req<void>("POST", "/api/auth/logout"),
  me: () => req<Me>("GET", "/api/me"),
  conversations: () => req<Conversation[]>("GET", "/api/conversations"),
  createConversation: (title: string) => req<Conversation>("POST", "/api/conversations", { title }),
  deleteConversation: (id: string) => req<void>("DELETE", `/api/conversations/${id}`),
  messages: (id: string) => req<Message[]>("GET", `/api/conversations/${id}/messages`),
  send: (conversationId: string, content: string, taskId?: string) =>
    req<{ message_id: string; task_id: string; created: boolean }>(
      "POST", `/api/conversations/${conversationId}/messages`,
      { content, client_request_id: crypto.randomUUID(), task_id: taskId ?? null }),
  tasks: (state?: string) => req<{ items: Task[]; next_cursor: string | null }>("GET", `/api/tasks${state ? `?state=${state}` : ""}`),
  cancel: (id: string) => req<Task>("POST", `/api/tasks/${id}/cancel`),
  resume: (id: string) => req<Task>("POST", `/api/tasks/${id}/resume`),
  retry: (id: string) => req<{ task_id: string }>("POST", `/api/tasks/${id}/retry`),
  notifications: () => req<Notification[]>("GET", "/api/notifications"),
  markRead: (id: string) => req<Notification>("POST", `/api/notifications/${id}/read`),
};

/** Subscribe to the owner's event stream. EventSource resends Last-Event-ID on reconnect,
 *  and the server replays from the outbox, so no event is lost or duplicated. */
export function subscribe(onEvent: (type: string) => void): () => void {
  const es = new EventSource("/api/events", { withCredentials: true });
  const handler = (e: MessageEvent) => onEvent((e as MessageEvent & { type: string }).type);
  const types = ["task.queued", "task.running", "task.waiting_user", "task.waiting_approval", "task.blocked", "approval.requested", "approval.decided",
    "browser.session_started", "browser.control", "browser.stopped", "automation.created", "automation.confirmed", "automation.updated", "automation.deleted", "automation.run", "automation.paused", "automation.resumed", "automation.held", "automation.catch_up",
    "action.succeeded", "action.failed", "action.unknown", "action.cancelled", "connection.updated", "connection.disconnected", "connection.connected", "connection.reauth_required",
    "task.succeeded", "task.failed", "task.cancelled", "message.created", "conversation.deleted"];
  types.push("artifact.created", "artifact.updated", "artifact.deleted", "memory.updated"); // M4
  types.forEach((t) => es.addEventListener(t, handler));
  return () => es.close();
}

// ---------------------------------------------------------------- M2: approvals, actions, connections

export type Mode = "off" | "read_only" | "read_write";
export interface PreviewField { label: string; value: string; before?: string; multiline?: boolean }
export interface Preview { title: string; tool: string; fields: PreviewField[]; side_effects: string; payload: Record<string, unknown> }
export interface Approval {
  id: string; state: string; expires_at: string; decided_at: string | null; reason: string | null; args_hash: string;
  action_id: string; action_state: string; task_id: string; task_intent: string; account: string; preview: Preview; effect: "read" | "write";
}
export interface Action {
  id: string; task_id: string; tool_name: string; state: string; preview: Preview; provider_refs: string[];
  error: { code: string; message: string } | null; resolution: string | null; dispatch_started_at: string | null;
}
export interface Connector { provider: string; label: string; capabilities: string[]; available: boolean; reason: string | null }
export interface Connection {
  id: string; provider: string; account_label: string; status: string; modes: Record<string, Mode>; version: number; is_demo: boolean;
  granted_scopes: string[]; last_error: string | null; last_health_at: string | null; warning: string | null;
}

export const api2 = {
  approvals: () => req<Approval[]>("GET", "/api/approvals"),
  decide: (a: Approval, decision: "allow_once" | "deny") =>
    req<{ approval: Approval; outcome: string }>("POST", `/api/approvals/${a.id}/decision`, { decision, expected_args_hash: a.args_hash }),
  unknownActions: () => req<Action[]>("GET", "/api/actions?state=unknown"),
  reconcile: (id: string) => req<Action>("POST", `/api/actions/${id}/reconcile`),
  resolve: (id: string, outcome: "happened" | "did_not_happen") => req<Action>("POST", `/api/actions/${id}/resolve`, { outcome }),
  connectors: () => req<Connector[]>("GET", "/api/connectors"),
  connections: () => req<Connection[]>("GET", "/api/connections"),
  connect: (provider: string) => req<Connection>("POST", "/api/connections", { provider }),
  setModes: (c: Connection, modes: Record<string, Mode>) => req<Connection>("PATCH", `/api/connections/${c.id}`, { modes, expected_version: c.version }),
  disconnect: (c: Connection) => req<Connection>("DELETE", `/api/connections/${c.id}`),
  setApprovalMode: (mode: "ask_writes" | "always_ask") => req<Me>("PATCH", "/api/me", { approval_mode: mode }),
  googleAuthorize: (modes: Record<string, Mode>, connectionId?: string) =>
    req<{ authorization_url: string; scopes: string[] }>("POST", "/api/connectors/google/authorize", { modes, connection_id: connectionId ?? null }),
  health: (c: Connection) => req<{ status: string; detail: string | null }>("POST", `/api/connections/${c.id}/health`),
};

export { req }; // M4: used by apiM4.ts

// ---------------------------------------------------------------- M5: automations

export type AutomationKind = "once" | "daily" | "weekly" | "calendar_change";
export interface AutomationSpec {
  name: string; kind: AutomationKind; prompt: string; time_zone: string | null; local_time: string | null;
  weekdays: string[] | null; run_date: string | null; connection_ids: string[];
  calendar: { connection_id: string; calendar_id: string } | null;
}
export interface Occurrence { scheduled_for: string; local: string; state: string; task_id: string | null; late: boolean; missed_count: number; changes: number }
export interface Automation {
  id: string; name: string; kind: AutomationKind; prompt: string; description: string; time_zone: string;
  local_time: string | null; weekdays: number[] | null; run_date: string | null; connection_ids: string[];
  enabled: boolean; confirmed: boolean; created_by: string; paused_reason: string | null;
  next_run_local: string | null; last_run_local: string | null; completed_at: string | null; version: number;
  trigger: { connection_id: string; calendar_id: string; window_hours: number } | null;
  trigger_status: { initialized: boolean; last_poll_at: string | null; last_error: string | null; pending_changes: number } | null;
  recent: Occurrence[];
}
export interface AutomationPreview { description: string; upcoming: { at: string; local: string }[] }

export const automationsApi = {
  list: () => req<Automation[]>("GET", "/api/automations"),
  preview: (spec: AutomationSpec) => req<AutomationPreview>("POST", "/api/automations/preview", spec),
  create: (spec: AutomationSpec) => req<Automation>("POST", "/api/automations", { ...spec, confirm: true }),
  confirm: (a: Automation) => req<Automation>("POST", `/api/automations/${a.id}/confirm`, { expected_version: a.version }),
  setEnabled: (a: Automation, enabled: boolean) => req<Automation>("PATCH", `/api/automations/${a.id}`, { expected_version: a.version, enabled }),
  remove: (a: Automation) => req<void>("DELETE", `/api/automations/${a.id}`),
};

// ---------------------------------------------------------------- M6: browser, export, delete-all

export interface BrowserSession {
  id: string; task_id: string | null; task_intent: string | null; state: string; control: "agent" | "user";
  current_url: string | null; urls_visited: { url: string; at: string; tab_id: string | null; blocked: boolean; title: string }[];
  version: number; created_at: string; ended_at: string | null;
}
export interface ExportInfo { id: string; file_name: string; size: number; sha256: string; counts: Record<string, number>; created_at: string }

export const browserApi = {
  list: () => req<BrowserSession[]>("GET", "/api/browser-sessions"),
  takeover: (id: string) => req<BrowserSession>("POST", `/api/browser-sessions/${id}/takeover`),
  resume: (id: string) => req<BrowserSession>("POST", `/api/browser-sessions/${id}/resume`),
  stop: (id: string) => req<BrowserSession>("POST", `/api/browser-sessions/${id}/stop`),
  input: (id: string, event: Record<string, unknown>) => req<{ ok: boolean }>("POST", `/api/browser-sessions/${id}/input`, event),
  streamUrl: (id: string) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/browser-sessions/${id}/stream`,
};

export const dataApi = {
  exports: () => req<ExportInfo[]>("GET", "/api/exports"),
  createExport: () => req<ExportInfo>("POST", "/api/export"),
  downloadUrl: (e: ExportInfo) => `/api/exports/${e.id}/download`,
  deleteAll: (confirm: string) => req<{ deleted: Record<string, number>; revocations: { account: string; revoked: boolean; error: string | null }[]; backup_note: string }>(
    "POST", "/api/delete-all", { confirm }),
};

export const automationResume = (a: { id: string; version: number }) =>
  req<unknown>("PATCH", `/api/automations/${a.id}`, { expected_version: a.version, resume: true });
