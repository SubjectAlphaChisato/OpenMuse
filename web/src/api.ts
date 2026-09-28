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
    "action.succeeded", "action.failed", "action.unknown", "action.cancelled", "connection.updated", "connection.disconnected", "connection.connected", "connection.reauth_required",
    "task.succeeded", "task.failed", "task.cancelled", "message.created", "conversation.deleted"];
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
