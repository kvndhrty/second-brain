import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { mockFetch } from "./fetch";
import { question, status, thread } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("dreaming and the injected context", () => {
  it("shows each thread's Now line and what an agent sees", async () => {
    const dreamed = { ...thread, now_line: "Cutover moved to May; owner still open.", now_as_of: "2026-11-07T10:00:00+00:00" };
    const calls = mockFetch({
      "GET /api/status": status,
      "GET /api/active": [dreamed],
      "GET /api/context": { text: "# brain: 2026-11-08 (Sunday).\n", doc: { today: "2026-11-08", entries_through: null, waiting: {} } },
      "GET /api/dream": { last: null, planned: ["write the Now line for GPU quota migration"], max_calls: 50 },
      "POST /api/dream": { requested: true },
    });
    renderAt("/");
    expect(await screen.findByText(/Cutover moved to May/)).toBeTruthy();
    expect(screen.getByText("(as of 2026-11-07)")).toBeTruthy();
    expect(await screen.findByText("What an agent sees at the start of a session")).toBeTruthy();
    expect(screen.getByText(/# brain: 2026-11-08/)).toBeTruthy();
    expect(await screen.findByText(/no pass yet · 1 step\(s\) waiting/)).toBeTruthy();
    fireEvent.click(screen.getByText("Dream now"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/dream")).toBe(true));
  });

  it("decides a proposed overcome-by-events in review", async () => {
    const item = {
      id: "r9",
      kind: "dream_obe",
      entry_id: "e-1",
      payload: { fragment: "f-1", superseded_by: "f-2", reason: "The date moved.", confidence: 0.9, citations: [] },
      entry_date: "2026-10-01",
      entry_label: "Planning",
      context: ["2026-10-01: Cutover is in March.", "2026-11-06: Cutover moved to May."],
      others: 0,
    };
    const calls = mockFetch({ "GET /api/status": status, "GET /api/review": [item], "POST /api/review/r9": { id: "r9" } });
    renderAt("/review");
    expect(await screen.findByText("Has this been overcome by events?")).toBeTruthy();
    expect(screen.getByText(/The date moved\. \(found by dreaming\)/)).toBeTruthy();
    fireEvent.click(screen.getByText("Yes, the earlier one no longer holds"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect((calls.find((c) => c.method === "POST")?.body as { action: string }).action).toBe("yes");
  });

  it("shows dreaming's verdict in the sweep", async () => {
    const items = [
      {
        kind: "stale_open",
        age_days: 40,
        fragment: question,
        thread_id: null,
        thread_name: null,
        threads: [],
        suggestion: { verdict: "probably_resolved", evidence: "f-9", reason: "A later entry confirms MIG support." },
      },
    ];
    mockFetch({ "GET /api/status": status, "GET /api/sweep": items });
    renderAt("/tidy");
    expect(await screen.findByTestId("sweep-suggestion")).toBeTruthy();
    expect(screen.getByText("probably resolved")).toBeTruthy();
  });
});
