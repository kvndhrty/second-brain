// Typed client for the brain HTTP API (src/brain/web/app.py).

export interface Fragment {
  id: string;
  entry_id: string;
  date: string;
  kind: string;
  text: string;
  status: "confirmed" | "speculative" | "overcome_by_events";
  state: "open" | "closed" | null;
  dream: boolean;
  obe_reason: string | null;
}

export interface Link {
  id: string;
  type: string;
  label: string;
}

export interface TimelineItem {
  fragment: Fragment;
  links: Link[];
}

export interface ThreadRow {
  id: string;
  name: string;
  description: string | null;
  state: string;
  pinned: boolean;
  summary: string | null;
  last_activity: string | null;
  fragments: number;
  open_items: number;
  people: string[];
}

export interface EntityView {
  id: string;
  type: string;
  name: string;
  aliases: string[];
  details: Record<string, unknown>;
  notes: string;
  summary: string | null;
  related: Record<string, string[]>;
  open_items: Fragment[];
  recent: Fragment[];
  fragment_count: number;
}

export interface EntitySummary {
  id: string;
  type: string;
  name: string;
  state: string | null;
  fragments: number;
  last_seen: string | null;
  detail: string | null;
}

export interface EntrySummary {
  id: string;
  date: string;
  title: string | null;
  preview: string;
  processed: boolean;
  fragments: number;
}

export interface EntryView {
  id: string;
  date: string;
  title: string | null;
  text: string;
  key: string;
  processed: boolean;
  fragments: Fragment[];
}

export interface Candidate {
  entity_id: string;
  name?: string;
  score: number;
}

export interface ReviewItem {
  id: string;
  kind: string;
  entry_id: string | null;
  payload: Record<string, any>;
  entry_date: string | null;
  entry_label: string | null;
  context: string[];
  others: number;
}

export interface Hit {
  id: string;
  type: string;
  label: string;
  snippet: string;
  date: string | null;
  entry_id: string | null;
  kind: string | null;
  status: string | null;
}

export interface Claim {
  text: string;
  citations: string[];
}

export interface Source {
  entry_id: string;
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
  target: string;
  label: string;
  type: string;
  since: string | null;
  summary: string | null;
  sections: Record<string, Claim[]>;
  open_items: Fragment[];
  sources: Source[];
  notes: string[];
  fragment_count: number;
}

export interface GraphData {
  nodes: { id: string; type: string; label: string; weight: number }[];
  edges: { source: string; target: string; weight: number; type: string }[];
  truncated: boolean;
}

export interface Status {
  version: string;
  store: string;
  entries: number;
  pending: number;
  review: number;
  llm_configured: boolean;
  failures: number;
  worker: { running: boolean; enabled: boolean; current: string | null; last_error: string | null } | null;
}

export interface FragmentPatch {
  status?: Fragment["status"];
  obe_reason?: string | null;
  state?: "open" | "closed";
  closed_note?: string | null;
  dream?: boolean;
}

export interface ReviewDecision {
  action: "link" | "create" | "ignore" | "accept" | "rename" | "merge" | "yes" | "no" | "drop";
  entity_id?: string;
  name?: string;
  role?: string;
  kind?: "domain" | "technique";
  into?: string;
  apply_to_all?: boolean;
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
  active: () => request<ThreadRow[]>("GET", "/api/active"),
  entities: (type: string) => request<EntitySummary[]>("GET", `/api/entities/${type}`),
  entity: (nodeId: string) => request<EntityView>("GET", `/api/entity/${q(nodeId)}`),
  timeline: (nodeId: string) => request<TimelineItem[]>("GET", `/api/entity/${q(nodeId)}/timeline`),
  entries: (before?: string) => request<EntrySummary[]>("GET", `/api/entries${before ? `?before=${q(before)}` : ""}`),
  entry: (id: string) => request<EntryView>("GET", `/api/entries/${q(id)}`),
  createEntry: (text: string, title?: string) =>
    request<{ id: string }>("POST", "/api/entries", { text, title: title || null }),
  updateEntry: (id: string, text: string, title?: string | null) =>
    request<{ id: string }>("PUT", `/api/entries/${q(id)}`, { text, title: title || null }),
  dreams: () => request<Fragment[]>("GET", "/api/dreams"),
  review: () => request<ReviewItem[]>("GET", "/api/review"),
  resolve: (id: string, decision: ReviewDecision) => request<unknown>("POST", `/api/review/${q(id)}`, decision),
  choices: (type: string, text: string) => request<Candidate[]>("GET", `/api/choices/${type}?q=${q(text)}`),
  patchFragment: (id: string, patch: FragmentPatch) => request<unknown>("PATCH", `/api/fragments/${q(id)}`, patch),
  search: (text: string) => request<Hit[]>("GET", `/api/search?q=${q(text)}`),
  ask: (question: string) => request<AskResult>("POST", "/api/ask", { question }),
  brief: (target: string, since?: string) =>
    request<BriefResult>("POST", "/api/brief", { target, since: since || null, mark_seen: true }),
  graph: (center?: string) => request<GraphData>("GET", `/api/graph${center ? `?center=${q(center)}` : ""}`),
};

export function nodePath(nodeId: string): string {
  return `/e/${q(nodeId)}`;
}
