import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/svelte";
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

// -- the mutating buttons ----------------------------------------------------

function project(over = {}) {
  return {
    name: "acme",
    path: "/w/acme",
    state: "running",
    live_session: false,
    live_evidence: "",
    window_open: false,
    pipeline: "acme",
    last_build: null,
    last_human_build_days: null,
    ...over,
  };
}

/** Stub fetch with one project and a canned answer for the next POST. */
function stubAction(row = project(), postBody = {}, postStatus = 200) {
  const calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, init) => {
      if (init?.method === "POST") {
        calls.push({ url: String(url), body: JSON.parse(init.body) });
        return jsonResponse(postBody, postStatus);
      }
      return String(url).includes("/api/projects")
        ? jsonResponse({ projects: [row], window_error: null, window_titles: 0 })
        : jsonResponse(STATUS);
    }),
  );
  return calls;
}

describe("the jobs a phone starts", () => {
  const AUTH = { Authorization: "Bearer x" };

  /** A fetch stub keyed by method+path, so a job can be polled to completion. */
  function stubRoutes(routes) {
    const calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url, init = {}) => {
        const key = `${init.method || "GET"} ${String(url)}`;
        calls.push({ key, body: init.body ? JSON.parse(init.body) : undefined });
        const route = routes.find((r) => key.startsWith(r.key));
        if (!route) throw new Error(`unstubbed request: ${key}`);
        return jsonResponse(
          typeof route.body === "function" ? route.body(calls.length) : route.body,
          route.status || 200,
        );
      }),
    );
    return calls;
  }

  it("starts an open as a job and streams the log from the poll", async () => {
    vi.useFakeTimers();
    try {
      const calls = stubRoutes([
        {
          key: "GET /api/projects",
          body: { projects: [project({ state: "absent", container: null })], window_titles: 0 },
        },
        { key: "GET /api/status", body: STATUS },
        {
          key: "POST /api/projects/acme/open",
          status: 202,
          body: { job_id: "j1", kind: "open", target: "acme", state: "running", log: [] },
        },
        {
          key: "GET /api/jobs/j1",
          body: {
            job_id: "j1",
            kind: "open",
            target: "acme",
            state: "done",
            log: ["Cloning acme", "Container ready"],
            result: { uri: "vscode-remote://x" },
          },
        },
      ]);
      render(Page);
      await fireEvent.click(await screen.findByRole("button", { name: "Open" }));
      expect(
        calls.some((c) => c.key === "POST /api/projects/acme/open" && JSON.stringify(c.body) === "{}"),
      ).toBe(true);

      await vi.advanceTimersByTimeAsync(1100);
      expect(screen.getByText(/Container ready/)).toBeInTheDocument();
      expect(screen.getByText("done")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("says so when the server refuses a second job for the same target", async () => {
    stubRoutes([
      {
        key: "GET /api/projects",
        body: { projects: [project({ state: "absent", container: null })], window_titles: 0 },
      },
      { key: "GET /api/status", body: STATUS },
      {
        key: "POST /api/projects/acme/open",
        status: 409,
        body: { error: "open already running for acme", job_id: "j1" },
      },
    ]);
    render(Page);
    await fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    expect(await screen.findByText("open already running for acme")).toBeInTheDocument();
  });

  it("lists repos with no workspace and provisions one", async () => {
    const calls = stubRoutes([
      { key: "GET /api/projects", body: { projects: [], window_titles: 0 } },
      { key: "GET /api/status", body: STATUS },
      { key: "GET /api/repos", body: { repos: ["nickbrett1/greenfield"], error: null } },
      {
        key: "POST /api/provision",
        status: 202,
        body: { job_id: "j2", kind: "provision", target: "nickbrett1/greenfield", state: "running" },
      },
    ]);
    render(Page);
    await fireEvent.click(await screen.findByRole("button", { name: /Provision a repo/ }));
    await fireEvent.click(await screen.findByRole("button", { name: "Provision" }));
    const post = calls.find((c) => c.key === "POST /api/provision");
    expect(post.body).toEqual({ repo: "nickbrett1/greenfield" });
    expect(await screen.findByText(/nickbrett1\/greenfield/)).toBeInTheDocument();
  });

  it("shows a listing error beside the picker rather than hiding it", async () => {
    stubRoutes([
      { key: "GET /api/projects", body: { projects: [], window_titles: 0 } },
      { key: "GET /api/status", body: STATUS },
      {
        key: "GET /api/repos",
        body: { repos: [], error: "no repositories returned (no GitHub token?)" },
      },
    ]);
    render(Page);
    await fireEvent.click(await screen.findByRole("button", { name: /Provision a repo/ }));
    expect(await screen.findByText(/no GitHub token/)).toBeInTheDocument();
  });
});

describe("open and close", () => {
  it("offers Close and Open for a project that exists", async () => {
    stubAction();
    render(Page);
    expect(await screen.findByRole("button", { name: "Close" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open" })).toBeInTheDocument();
  });

  it("offers only Open for a project with no container", async () => {
    stubAction(project({ state: "absent", container: null }));
    render(Page);
    expect(await screen.findByRole("button", { name: "Open" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
  });

  it("posts the close and reports both halves", async () => {
    const calls = stubAction(project(), { name: "acme", stopped: true, window_closed: false });
    render(Page);
    await fireEvent.click(await screen.findByRole("button", { name: "Close" }));
    expect(await screen.findByText("container stopped · window left open")).toBeInTheDocument();
    expect(calls).toEqual([{ url: "/api/projects/acme/close", body: {} }]);
  });

  it("shows a refusal and offers the override rather than deciding for you", async () => {
    const calls = stubAction(project({ live_session: true, live_evidence: "tmux: attached" }), {
      name: "acme",
      refused: true,
      detail: "live session (tmux: attached); pass force to stop anyway",
    });
    render(Page);
    await fireEvent.click(await screen.findByRole("button", { name: "Close" }));
    expect(await screen.findByText(/live session \(tmux: attached\)/)).toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "Force stop" }));
    expect(calls.map((c) => c.body)).toEqual([{}, { force: true }]);
  });

  it("surfaces the server's reason when an action is refused outright", async () => {
    stubAction(project(), { error: "unknown project 'acme'" }, 400);
    render(Page);
    await fireEvent.click(await screen.findByRole("button", { name: "Close" }));
    expect(await screen.findByText("unknown project 'acme'")).toBeInTheDocument();
  });
});
