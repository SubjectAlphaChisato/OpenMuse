// M4 client: files/artifacts (ART-01) and memory (MEM-01). Types mirror
// server/opa/api/routers/artifacts.py and memory.py.
import { ApiError, csrfToken, req } from "./api";

export interface ArtifactVersion {
  version: number; size: number; sha256: string; source_refs: Record<string, unknown>[];
  created_by_task_id: string | null; note: Record<string, unknown>; created_at: string;
}
export interface Artifact {
  id: string; name: string; mime: string; origin: "upload" | "generated" | "owner"; task_id: string | null;
  current_version: number; created_at: string; updated_at: string; current: ArtifactVersion | null;
}
export interface Preview {
  kind: "markdown" | "text" | "csv" | "pdf"; version: number; html: string | null; text: string | null;
  rows: string[][] | null; truncated: boolean; note: Record<string, unknown>;
}
export interface MemoryItem {
  id: string; text: string; kind: "user_statement" | "inference"; scope: string; tags: string[]; confirmed: boolean;
  source_ref: Record<string, unknown>; version: number; created_at: string; updated_at: string;
}
export interface MemoryDoc { items: MemoryItem[]; suggestions: MemoryItem[]; markdown: string; version: string; forget_note: string }

export const downloadUrl = (id: string, version: number) => `/api/artifacts/${id}/versions/${version}/download`;

async function upload(file: File): Promise<Artifact> {
  const r = await fetch(`/api/artifacts?filename=${encodeURIComponent(file.name)}`, {
    method: "POST", body: file, credentials: "same-origin",
    headers: { "content-type": file.type || "application/octet-stream", "x-csrf-token": csrfToken() },
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = data?.error ?? {};
    throw new ApiError(r.status, e.code ?? "error", e.message ?? r.statusText);
  }
  return data as Artifact;
}

async function versionText(id: string, version: number): Promise<string> {
  const r = await fetch(downloadUrl(id, version), { credentials: "same-origin" });
  if (!r.ok) throw new ApiError(r.status, "error", "Could not load the file");
  return r.text();
}

export const files = {
  list: () => req<Artifact[]>("GET", "/api/artifacts"),
  get: (id: string) => req<Artifact>("GET", `/api/artifacts/${id}`),
  upload,
  createMarkdown: (name: string, content: string) => req<Artifact>("POST", "/api/artifacts/markdown", { name, content }),
  edit: (id: string, expectedVersion: number, content: string) =>
    req<Artifact>("PATCH", `/api/artifacts/${id}`, { expected_version: expectedVersion, content }),
  remove: (id: string) => req<void>("DELETE", `/api/artifacts/${id}`),
  versions: (id: string) => req<ArtifactVersion[]>("GET", `/api/artifacts/${id}/versions`),
  preview: (id: string, version: number) => req<Preview>("GET", `/api/artifacts/${id}/versions/${version}/preview`),
  renderPdf: (id: string, version: number) => req<Artifact>("POST", `/api/artifacts/${id}/render-pdf`, { version }),
  versionText,
};

export const memory = {
  get: () => req<MemoryDoc>("GET", "/api/memory"),
  save: (markdown: string, expectedVersion: string) => req<MemoryDoc>("PUT", "/api/memory", { markdown, expected_version: expectedVersion }),
  add: (text: string, scope: string, tags: string[]) => req<MemoryItem>("POST", "/api/memory", { text, scope, tags }),
  edit: (m: MemoryItem, text: string, tags: string[]) => req<MemoryItem>("PATCH", `/api/memory/${m.id}`, { expected_version: m.version, text, tags }),
  accept: (m: MemoryItem) => req<MemoryItem>("POST", `/api/memory/${m.id}/accept`, { expected_version: m.version }),
  forget: (m: MemoryItem) => req<void>("DELETE", `/api/memory/${m.id}`),
};

export function formatSize(n: number): string {
  if (n < 1024) return `${n} bytes`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function errorText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return e.status === 409 ? `${e.message}` : e.message;
  return fallback;
}
