import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { mockFetch } from "./fetch";
import { question, status } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

describe("Tidy up", () => {
  it("closes a stale open item and archives a quiet thread", async () => {
    const items = [
      { kind: "stale_open", age_days: 40, fragment: question, thread_id: null, thread_name: null, threads: ["GPU quota"] },
      { kind: "quiet_thread", age_days: 70, fragment: null, thread_id: "old-work", thread_name: "Old work", threads: null },
    ];
    const calls = mockFetch({
      "GET /api/status": status,
      "GET /api/sweep": items,
      [`PATCH /api/fragments/${question.id}`]: {},
      "POST /api/threads/old-work": { id: "old-work" },
    });
    render(
      <MemoryRouter initialEntries={["/tidy"]}>
        <App />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/Still open after 40 days/)).toBeTruthy();
    fireEvent.click(screen.getByText("Done / answered"));
    fireEvent.click(screen.getByText("Archive"));
    await waitFor(() => expect(calls.filter((c) => c.method !== "GET").length).toBe(2));
    const writes = calls.filter((c) => c.method !== "GET").map((c) => [c.method, c.url, c.body]);
    expect(writes).toContainEqual(["PATCH", `/api/fragments/${encodeURIComponent(question.id)}`, { state: "closed" }]);
    expect(writes).toContainEqual(["POST", "/api/threads/old-work", { op: "archive" }]);
  });
});
