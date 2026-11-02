import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { mockFetch } from "./fetch";
import { status, thread } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("App", () => {
  it("lists active threads and the review count", async () => {
    mockFetch({ "GET /api/status": status, "GET /api/active": [thread] });
    renderAt("/");
    expect(await screen.findByText("GPU quota migration")).toBeTruthy();
    expect(screen.getByText("Priya Raman")).toBeTruthy();
    expect(screen.getByText("1").className).toBe("count");
  });

  it("settles a new person from the review page", async () => {
    const item = {
      id: "r1",
      kind: "new_person",
      entry_id: "e-1",
      payload: { type: "person", surface: "Priya", proposed: { name: "Priya" }, candidates: [] },
      entry_date: "2026-10-30",
      entry_label: "Quota kickoff",
      context: ["Kicked off the migration with Priya."],
      others: 2,
    };
    const calls = mockFetch({ "GET /api/status": status, "GET /api/review": [item], "POST /api/review/r1": { id: "r1" } });
    renderAt("/review");
    expect(await screen.findByText("Kicked off the migration with Priya.")).toBeTruthy();
    expect(screen.getByText(/also apply to 2 other entries/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("name"), { target: { value: "Priya Raman" } });
    fireEvent.click(screen.getByText("Create person"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ apply_to_all: true, action: "create", name: "Priya Raman" });
  });

  it("asks a question and shows numbered sources", async () => {
    const answer = {
      question: "when?",
      claims: [{ text: "The cluster moves in December.", citations: ["e-1"] }],
      insufficient: false,
      sources: [{ entry_id: "e-1", date: "2026-10-30", label: "Quota kickoff" }],
    };
    mockFetch({ "GET /api/status": status, "POST /api/ask": answer });
    renderAt("/search");
    fireEvent.change(screen.getByPlaceholderText(/question to answer/), { target: { value: "when does it move?" } });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText(/The cluster moves in December/)).toBeTruthy();
    expect(screen.getByText("2026-10-30 · Quota kickoff")).toBeTruthy();
  });
});
