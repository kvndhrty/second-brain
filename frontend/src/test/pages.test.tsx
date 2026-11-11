import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { mockFetch } from "./fetch";
import { page, status, thread } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

const home = {
  "GET /api/status": status,
  "GET /api/active": [thread],
  "GET /api/log": [
    { at: "2026-11-05T10:00:00+00:00", source: "n-20261105T100000Z", kind: "supersedes", text: "Replaced: December by January", entity_id: null },
  ],
  "GET /api/dream": { last: null, planned: { duplicates: [["a", "b"]], stale: [] }, max_calls: 50 },
  "GET /api/context": { text: "# brain: the user's work journal" },
};

describe("App", () => {
  it("shows the note box, active threads with where they stand, and recent changes", async () => {
    mockFetch(home);
    renderAt("/");
    expect(await screen.findByText("GPU quota migration")).toBeTruthy();
    expect(screen.getByText(/Moving in January; the quota ticket/)).toBeTruthy();
    expect(await screen.findByText(/Replaced: December by January/)).toBeTruthy();
    expect(screen.getByLabelText("note")).toBeTruthy();
    expect(await screen.findByText(/1 thing\(s\) to look at/)).toBeTruthy();
  });

  it("saves a note as the only write", async () => {
    const calls = mockFetch({ ...home, "POST /api/notes": { id: "n-1" } });
    renderAt("/");
    const box = screen.getByLabelText("note");
    fireEvent.change(box, { target: { value: "Met Priya about the quota." } });
    // The shortcut pressed twice while the first save is in flight saves one note.
    fireEvent.keyDown(box, { key: "Enter", metaKey: true });
    fireEvent.keyDown(box, { key: "Enter", metaKey: true });
    expect(await screen.findByText(/See what brain made of it/)).toBeTruthy();
    expect(calls.filter((c) => c.method === "POST").length).toBe(1);
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toBe("/api/notes");
    expect(post.body).toEqual({ text: "Met Priya about the quota.", about: null });
    expect(post.contentType).toBe("application/json");
    expect(await screen.findByText(/See what brain made of it/)).toBeTruthy();
  });

  it("shows a wiki page with superseded statements struck through", async () => {
    mockFetch({ "GET /api/status": status, "GET /api/pages/gpu-quota-migration": page });
    renderAt("/p/gpu-quota-migration");
    expect(await screen.findByText("Moving in January.")).toBeTruthy();
    expect(screen.getByText(/also called quota work/)).toBeTruthy();
    expect(screen.getByText("Priya Raman")).toBeTruthy();
    const lines = screen.getAllByTestId("statement");
    expect(lines.some((l) => l.className.includes("superseded") && l.textContent?.includes("January instead"))).toBe(true);
  });

  it("turns 'This is wrong' into a note about that page", async () => {
    const calls = mockFetch({
      "GET /api/status": status,
      "GET /api/pages/gpu-quota-migration": page,
      "POST /api/notes": { id: "n-2" },
    });
    renderAt("/p/gpu-quota-migration");
    fireEvent.click(await screen.findByText("This is wrong"));
    fireEvent.change(screen.getByLabelText("note"), { target: { value: "This is finished." } });
    fireEvent.click(screen.getByText("Save correction"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({
      text: "This is finished.",
      about: "gpu-quota-migration",
    });
    // No other kind of write exists in the UI.
    expect(calls.filter((c) => c.method !== "GET").map((c) => c.url)).toEqual(["/api/notes"]);
  });

  it("asks a question and shows numbered sources", async () => {
    const answer = {
      question: "when?",
      claims: [{ text: "The cluster moves in January.", citations: ["n-1"] }],
      insufficient: false,
      sources: [{ note_id: "n-1", date: "2026-11-05", label: "Quota update" }],
    };
    mockFetch({ "GET /api/status": status, "POST /api/ask": answer });
    renderAt("/search");
    fireEvent.change(screen.getByPlaceholderText(/question to answer/), { target: { value: "when does it move?" } });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText(/The cluster moves in January/)).toBeTruthy();
    expect(screen.getByText("2026-11-05 · Quota update")).toBeTruthy();
  });

  it("lists wiki pages by type", async () => {
    mockFetch({
      "GET /api/status": status,
      "GET /api/pages": [thread, { ...thread, id: "priya-raman", type: "person", name: "Priya Raman", now: null }],
    });
    renderAt("/wiki");
    expect(await screen.findByText("Threads")).toBeTruthy();
    expect(screen.getByText("People")).toBeTruthy();
    expect(screen.getByText("Priya Raman")).toBeTruthy();
  });
});
