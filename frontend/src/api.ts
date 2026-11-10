// Typed client for the brain HTTP API (src/brain/web/app.py).

export interface Statement {
  id: string;
  note_id: string;
  date: string;
  age: string;
  kind: "fact" | "decision" | "question" | "action" | "idea" | "correction";
  text: string;
  status: "current" | "superseded" | "answered" | "repeat" | "retracted";
  replaced_by: string | null;
  thread: string | null;
  thread_name: string | null;
}

export interface Cited {
  text: string;
  citations: string[];
}

export interface Ref {
  id: string;
  type: string;
  name: string;
}

export interface LogEvent {
  at: string;
  source: string;
  kind: string;
  text: string;
  entity_id: string | null;
}

export interface Page {
  id: string;
  type: "thread" | "person" | "org" | "topic";
  name: string;
  names: string[];
  status: "active" | "closed" | "provisional";
  profile: Record<string, string>;
  as_of: string | null;
  last_activity: string | null;
  closed_at: string | null;
  now: Cited | null;
  summary: Cited[];
  open: Statement[];
  decided: Statement[];
  history: Statement[];
  related: Ref[];
  note_count: number;
  log: LogEvent[];
}

export interface PageSummary {
  id: string;
  type: string;
  name: string;
  status: string;
  now: string | null;
  last_activity: string | null;
  open_items: number;
  note_count: number;
}

export interface NoteSummary {
  id: string;
  date: string;
  label: string;
  done: boolean;
  statements: number;
}

export interface Note {
  id: string;
  created: string;
  title: string | null;
  about: string | null;
  body: string;
  done: boolean;
  statements: Statement[];
  entities: Ref[];
  events: LogEvent[];
}

export interface Hit {
  id: string;
  type: string;
  label: string;
  snippet: string;
  date: string | null;
  note_id: string | null;
  kind: string | null;
  status: string | null;
}

export interface Claim {
  text: string;
  citations: string[];
}

export interface Source {
  note_id: string;
  date: string;
  label: string;
}

export interface AskResult {
  question: string;
  claims: Claim[];
  insufficient: boolean;
  sources: Source[];
}

export interface BriefResult {
  request: string;
  topics: Ref[];
  since: string | null;
  sections: Record<string, Claim[]>;
  sources: Source[];
}

export interface Status {
  version: string;
  store: string;
  notes: number;
  pending: number;
  pages: number;
  llm_configured: boolean;
  failures: number;
  worker: { running: boolean; enabled: boolean; current: string | null; last_error: string | null } | null;
}

export interface ContextResult {
  text: string;
}

export interface DreamRun {
  run_id: string;
  started_at: string;
  finished_at: string | null;
  calls: number;
  budget_exhausted: boolean;
  events: { kind: string; text: string; entity: string | null }[];
  errors: string[];
}

export interface DreamStatus {
  last: DreamRun | null;
  planned: Record<string, unknown[]>;
  max_calls: number;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body: any,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    // The server refuses writes that are not JSON, which keeps other sites from posting to it.
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const message = typeof data === "object" && data?.detail ? String(data.detail) : `HTTP ${res.status}`;
    throw new ApiError(res.status, message, data);
  }
  return data as T;
}

const q = encodeURIComponent;

export const api = {
  status: () => request<Status>("GET", "/api/status"),
  active: () => request<PageSummary[]>("GET", "/api/active"),
  pages: () => request<PageSummary[]>("GET", "/api/pages"),
  page: (id: string) => request<Page>("GET", `/api/pages/${q(id)}`),
  notes: (before?: string) => request<NoteSummary[]>("GET", `/api/notes${before ? `?before=${q(before)}` : ""}`),
  note: (id: string) => request<Note>("GET", `/api/notes/${q(id)}`),
  // The only write. `about` is the page a "This is wrong" note corrects.
  jot: (text: string, about?: string) => request<{ id: string }>("POST", "/api/notes", { text, about: about ?? null }),
  log: (limit = 100) => request<LogEvent[]>("GET", `/api/log?limit=${limit}`),
  search: (text: string) => request<Hit[]>("GET", `/api/search?q=${q(text)}`),
  ask: (question: string) => request<AskResult>("POST", "/api/ask", { question }),
  brief: (text: string) => request<BriefResult>("POST", "/api/brief", { request: text, mark_seen: true }),
  context: () => request<ContextResult>("GET", "/api/context"),
  dreamStatus: () => request<DreamStatus>("GET", "/api/dream"),
  dreamNow: () => request<{ requested: boolean }>("POST", "/api/dream", {}),
};

export const pagePath = (id: string) => `/p/${q(id)}`;
export const notePath = (id: string) => `/notes/${q(id)}`;

// Timestamps from the API are UTC; show them as local dates and times.
export function day(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.slice(0, 10) : d.toLocaleDateString("sv-SE");
}

export function minute(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16).replace("T", " ");
  return `${d.toLocaleDateString("sv-SE")} ${d.toLocaleTimeString("sv-SE").slice(0, 5)}`;
}
