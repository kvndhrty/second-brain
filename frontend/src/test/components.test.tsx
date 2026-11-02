import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { citationOrder, ClaimList, FragmentCard, SummaryText } from "../components";
import { mockFetch } from "./fetch";
import { question } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

describe("SummaryText", () => {
  it("renders bullets and turns entry citations into links", () => {
    render(
      <MemoryRouter>
        <SummaryText text={"Current state.\n- Moving in **December**. [e-20261030T143205Z]"} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Current state.")).toBeTruthy();
    expect(screen.getByText("December").tagName).toBe("STRONG");
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toBe("/entries/e-20261030T143205Z");
  });
});

describe("citations", () => {
  it("numbers sources in order of first citation", () => {
    const claims = [
      { text: "a", citations: ["e-2", "e-1"] },
      { text: "b", citations: ["e-1", "e-3"] },
    ];
    expect(citationOrder(claims)).toEqual(["e-2", "e-1", "e-3"]);
    render(
      <MemoryRouter>
        <ClaimList claims={claims} order={citationOrder(claims)} />
      </MemoryRouter>,
    );
    expect(screen.getAllByRole("link").map((l) => l.textContent)).toEqual(["[1]", "[2]", "[2]", "[3]"]);
  });
});

describe("FragmentCard", () => {
  it("shows badges and closes an open question with a JSON request", async () => {
    const calls = mockFetch({ [`PATCH /api/fragments/${question.id}`]: { id: question.id } });
    const onChanged = vi.fn();
    render(
      <MemoryRouter>
        <FragmentCard fragment={question} links={[{ id: "person:priya", type: "person", label: "Priya" }]} onChanged={onChanged} />
      </MemoryRouter>,
    );
    expect(screen.getByText("question")).toBeTruthy();
    expect(screen.getByText("open")).toBeTruthy();
    expect(screen.getByText("Priya").getAttribute("href")).toBe("/e/person%3Apriya");
    fireEvent.click(screen.getByText("Close"));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(calls[0]).toMatchObject({ method: "PATCH", body: { state: "closed" }, contentType: "application/json" });
  });
});

describe("api", () => {
  it("raises ApiError with the server's detail message", async () => {
    mockFetch({});
    await expect(api.entity("person:nobody")).rejects.toBeInstanceOf(ApiError);
    await expect(api.entity("person:nobody")).rejects.toThrow("no mock for GET /api/entity/person%3Anobody");
  });
});
