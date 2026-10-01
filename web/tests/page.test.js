import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/svelte";
import Page from "../src/routes/+page.svelte";

// Fixtures use synthetic project names: devdash is public, and a test that
// baked in a real private workspace name would publish it.

const PROJECTS = {
  projects: [
    {
      name: "acme",
      path: "/w/acme",
      state: "running",
      live_session: true,
      live_evidence: "tmux: attached",
      window_open: true,
      pipeline: "acme",
      last_build: "#7 (Nick Brett, main)",
      last_human_build_days: 2.0,
    },
    {
      name: "example-one",
      path: "/w/example-one",
      state: "stopped",
      live_session: false,
      live_evidence: "",
      window_open: false,
      pipeline: "example-one",
      last_build: null,
      last_human_build_days: null,
    },
  ],
  window_error: null,
  window_titles: 3,
};

const STATUS = {
  vm_mem_total: 16_000_000_000,
  vm_mem_used: 8_000_000_000,
  container_count: 2,
  running_count: 1,
  devcontainer_footprint: 1_000_000_000,
};

function jsonResponse(body, status = 200) {
  return { ok: status < 400, status, json: async () => body };
}

function stubFetch(projectsBody = PROJECTS, projectsStatus = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url) =>
      String(url).includes("/api/projects")
        ? jsonResponse(projectsBody, projectsStatus)
        : jsonResponse(STATUS),
    ),
  );
}

beforeEach(() => stubFetch());
afterEach(() => vi.unstubAllGlobals());

describe("the project list", () => {
  it("renders a row per project", async () => {
    render(Page);
    expect(await screen.findByText("acme")).toBeInTheDocument();
    expect(screen.getByText("example-one")).toBeInTheDocument();
  });

  it("marks the live session and the open window", async () => {
    render(Page);
    expect(await screen.findByText("live session")).toBeInTheDocument();
    expect(screen.getByText("window open")).toBeInTheDocument();
  });

  it("shows the last human build with its age", async () => {
    render(Page);
    expect(await screen.findByText("#7 (Nick Brett, main)", { exact: false })).toBeInTheDocument();
  });

  it("says so when a project has never had a human build", async () => {
    render(Page);
    expect(await screen.findByText("no human build")).toBeInTheDocument();
  });
});

describe("the status strip", () => {
  it("summarises memory, counts and visible windows", async () => {
    render(Page);
    expect(await screen.findByText("8 GB / 16 GB")).toBeInTheDocument();
    // The raw title count is the tell for a lost Accessibility grant.
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("vscode windows")).toBeInTheDocument();
  });
});

describe("when the token is missing", () => {
  it("says how to get one instead of rendering an empty list", async () => {
    stubFetch(PROJECTS, 401);
    render(Page);
    expect(await screen.findByText(/Not authorised/)).toBeInTheDocument();
  });
});
