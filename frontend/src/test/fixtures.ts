import type { Fragment, Status, ThreadRow } from "../api";

export const status: Status = {
  version: "0.1.0",
  store: "/tmp/store",
  entries: 2,
  pending: 0,
  review: 1,
  llm_configured: true,
  failures: 0,
  worker: { running: true, enabled: true, current: "idle", last_error: null },
};

export const thread: ThreadRow = {
  id: "gpu-quota",
  name: "GPU quota migration",
  description: "Moving eval to the new pool",
  state: "active",
  pinned: false,
  summary: "- Moving in December. [e-20261030T143205Z]",
  last_activity: new Date().toISOString(),
  fragments: 3,
  open_items: 1,
  people: ["Priya Raman"],
};

export const question: Fragment = {
  id: "f-e-20261030T143205Z-03",
  entry_id: "e-20261030T143205Z",
  date: "2026-10-30",
  kind: "question",
  text: "Does the vendor support MIG?",
  status: "confirmed",
  state: "open",
  someday: false,
  obe_reason: null,
};
