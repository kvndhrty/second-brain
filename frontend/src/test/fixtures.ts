import type { Page, PageSummary, Statement, Status } from "../api";

export const status: Status = {
  version: "0.2.0",
  store: "/tmp/store",
  notes: 2,
  pending: 0,
  pages: 3,
  llm_configured: true,
  failures: 0,
  worker: { running: true, enabled: true, current: "idle", last_error: null },
};

export const thread: PageSummary = {
  id: "gpu-quota-migration",
  type: "thread",
  name: "GPU quota migration",
  status: "active",
  now: "Moving in January; the quota ticket is still to be filed.",
  last_activity: new Date().toISOString(),
  open_items: 1,
  note_count: 3,
};

export const question: Statement = {
  id: "s-20261030T143205Z-03",
  note_id: "n-20261030T143205Z",
  date: "2026-10-30",
  age: "9d",
  kind: "question",
  text: "Does the vendor support MIG?",
  status: "current",
  replaced_by: null,
  thread: "gpu-quota-migration",
  thread_name: "GPU quota migration",
};

export const december: Statement = {
  ...question,
  id: "s-20261030T143205Z-02",
  kind: "decision",
  text: "We decided to migrate in December.",
  status: "superseded",
  replaced_by: "We decided to migrate in January instead.",
};

export const page: Page = {
  id: "gpu-quota-migration",
  type: "thread",
  name: "GPU quota migration",
  names: ["quota work"],
  status: "active",
  profile: {},
  as_of: "2026-11-05",
  last_activity: "2026-11-05T10:00:00+00:00",
  closed_at: null,
  now: { text: "Moving in January.", citations: ["n-20261105T100000Z"] },
  summary: [{ text: "First planned for December, then moved to January.", citations: ["n-20261030T143205Z"] }],
  open: [question],
  decided: [],
  history: [december, question],
  related: [{ id: "priya-raman", type: "person", name: "Priya Raman" }],
  note_count: 3,
  log: [],
};
