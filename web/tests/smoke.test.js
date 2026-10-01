import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/svelte";
import Page from "../src/routes/+page.svelte";

describe("generated app smoke test", () => {
  it("renders the home page with the initial counter", () => {
    render(Page);
    expect(screen.getByText("Welcome to SvelteKit")).toBeInTheDocument();
    expect(screen.getByRole("button").textContent).toContain("0");
  });
  it("increments the counter on click", () => {
    render(Page);
    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    expect(btn.textContent).toContain("1");
  });
});
