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
  it("summarises memory and the container counts", async () => {
    render(Page);
    expect(await screen.findByText("8 GB / 16 GB")).toBeInTheDocument();
    expect(screen.getByText("containers")).toBeInTheDocument();
    expect(screen.getByText("devcontainers")).toBeInTheDocument();
  });

  it("reports no window count", async () => {
    // The count was noise: work happens over SSH inside the container, so how
    // many VS Code windows System Events can see says nothing about it.
    stubFetch({ ...PROJECTS, window_titles: 9 });
    render(Page);
    expect(await screen.findByText("containers")).toBeInTheDocument();
    expect(screen.queryByText("vscode windows")).not.toBeInTheDocument();
    expect(screen.queryByText("windows hidden — screen locked")).not.toBeInTheDocument();
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
    mem_bytes: null,
    live_session: false,
    live_evidence: "",
    window_open: false,
    pipeline: "acme",
    last_build: null,
    last_human_build_days: null,
    ...over,
  };
}

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

/** Stub fetch with one project and a canned answer for the next POST. */
function stubAction(row = project(), postBody = {}, postStatus = 200, screenLocked = false) {
  const calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, init) => {
      if (init?.method === "POST") {
        calls.push({ url: String(url), body: JSON.parse(init.body) });
        return jsonResponse(postBody, postStatus);
      }
      return String(url).includes("/api/projects")
        ? jsonResponse({ projects: [row], window_error: null, window_titles: 0, screen_locked: screenLocked })
        : jsonResponse(STATUS);
    }),
  );
  return calls;
}

describe("the jobs a phone starts", () => {
  const AUTH = { Authorization: "Bearer x" };

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
    // The picker is the `?provision=1` window, so it is already open when the
    // window loads; tapping Provision closes it and the build log takes over.
    window.history.pushState({}, "", "/?provision=1");
    try {
      render(Page);
      await fireEvent.click(await screen.findByRole("button", { name: "Provision" }));
      const post = calls.find((c) => c.key === "POST /api/provision");
      expect(post.body).toEqual({ repo: "nickbrett1/greenfield" });
      expect(await screen.findByText(/provision nickbrett1\/greenfield/)).toBeInTheDocument();
    } finally {
      window.history.pushState({}, "", "/");
    }
  });

  it("offers a top link that opens the provision picker in its own window", async () => {
    stubRoutes([
      { key: "GET /api/projects", body: { projects: [], window_titles: 0 } },
      { key: "GET /api/status", body: STATUS },
    ]);
    render(Page);
    const link = await screen.findByRole("link", { name: "Provision a repo" });
    expect(link).toHaveAttribute("href", "?provision=1");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("opens the job view with a link to its own window", async () => {
    vi.useFakeTimers();
    try {
      stubRoutes([
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
          body: { job_id: "j1", kind: "open", target: "acme", state: "running", log: ["Cloning acme"] },
        },
      ]);
      render(Page);
      await fireEvent.click(await screen.findByRole("button", { name: "Open" }));

      // The log renders without waiting a full poll, and the job can be handed
      // to its own browser window.
      expect(await screen.findByText(/Cloning acme/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "New window" })).toHaveAttribute("href", "?job=j1");
    } finally {
      vi.useRealTimers();
    }
  });

  it("adopts a job named in the URL, so it can live in its own window", async () => {
    window.history.pushState({}, "", "/?job=j9");
    try {
      stubRoutes([
        { key: "GET /api/projects", body: { projects: [], window_titles: 0 } },
        { key: "GET /api/status", body: STATUS },
        {
          key: "GET /api/jobs/j9",
          body: { job_id: "j9", kind: "provision", target: "greenfield", state: "done", log: ["done here"] },
        },
      ]);
      render(Page);

      expect(await screen.findByText(/done here/)).toBeInTheDocument();
      expect(screen.getByText("provision greenfield")).toBeInTheDocument();
    } finally {
      window.history.pushState({}, "", "/");
    }
  });

  it("renders a job's pending Tailscale login as a tappable link", async () => {
    window.history.pushState({}, "", "/?job=j9");
    try {
      stubRoutes([
        { key: "GET /api/projects", body: { projects: [], window_titles: 0 } },
        { key: "GET /api/status", body: STATUS },
        {
          key: "GET /api/jobs/j9",
          body: {
            job_id: "j9",
            kind: "provision",
            target: "acme",
            state: "running",
            log: ["Cloning acme"],
            attention: {
              kind: "tailscale",
              url: "https://login.tailscale.com/a/x",
              detail: "Authenticate acme on the tailnet to finish",
            },
          },
        },
      ]);
      render(Page);

      // The URL is a real link, not a line in the log: the job stands still
      // until the phone can tap it, so it must be a tap target.
      const link = await screen.findByRole("link", { name: /Authenticate on Tailscale/ });
      expect(link).toHaveAttribute("href", "https://login.tailscale.com/a/x");
      expect(screen.getByText(/Authenticate acme on the tailnet to finish/)).toBeInTheDocument();
    } finally {
      window.history.pushState({}, "", "/");
    }
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
    window.history.pushState({}, "", "/?provision=1");
    try {
      render(Page);
      expect(await screen.findByText(/no GitHub token/)).toBeInTheDocument();
    } finally {
      window.history.pushState({}, "", "/");
    }
  });
});

describe("reaching a running container", () => {
  const connected = {
    state: "connected",
    host: "acme",
    user: "vscode",
    ip: "100.64.0.9",
    ssh: "ssh vscode@acme",
    blink: "blink://host/?host=acme&username=vscode&port=22",
  };

  it("shows the tailnet state and the ssh target", async () => {
    stubAction(project({ state: "running", connection: connected }));
    render(Page);

    expect(await screen.findByText("tailnet")).toBeInTheDocument();
    expect(screen.getByText("ssh vscode@acme")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Blink" })).not.toBeInTheDocument();
  });

  it("offers Register when the container is on tailscale but not registered", async () => {
    // Register is a job like the gate: it drives the login, waits for the
    // tailnet, then starts the agent. So it lands in the job view, whose
    // attention link is the tap target the phone needs.
    const calls = stubRoutes([
      {
        key: "GET /api/projects",
        body: {
          projects: [
            project({ state: "running", connection: { ...connected, state: "logged_out", blink: null, ssh: null } }),
          ],
          window_titles: 0,
        },
      },
      { key: "GET /api/status", body: STATUS },
      {
        key: "POST /api/projects/acme/tailscale",
        status: 202,
        body: { job_id: "j1", kind: "tailscale", target: "acme", state: "running", log: [] },
      },
      {
        key: "GET /api/jobs/j1",
        body: {
          job_id: "j1",
          kind: "tailscale",
          target: "acme",
          state: "running",
          log: ["Registering acme on the tailnet"],
          attention: {
            kind: "tailscale",
            url: "https://login.tailscale.com/a/x",
            detail: "Authenticate acme on the tailnet to finish",
          },
        },
      },
    ]);
    vi.useFakeTimers();
    try {
      render(Page);

      await fireEvent.click(await screen.findByRole("button", { name: "Register" }));

      expect(calls).toContainEqual({ key: "POST /api/projects/acme/tailscale", body: {} });

      await vi.advanceTimersByTimeAsync(1100);
      // The login URL is the whole point: the phone has to open it.
      const link = screen.getByRole("link", { name: /Authenticate on Tailscale/ });
      expect(link).toHaveAttribute("href", "https://login.tailscale.com/a/x");
    } finally {
      vi.useRealTimers();
    }
  });

  it("says so when a container has no tailscale at all", async () => {
    stubAction(project({ state: "running", connection: { ...connected, state: "absent", blink: null, ssh: null } }));
    render(Page);

    expect(await screen.findByText("no tailscale")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Register" })).not.toBeInTheDocument();
  });
});

describe("open and close", () => {
  it("offers Close and Open for a project that exists", async () => {
    stubAction();
    render(Page);
    expect(await screen.findByRole("button", { name: "Close" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open" })).toBeInTheDocument();
  });

  it("offers only Open for a stopped container", async () => {
    // Close on a stopped container would close a leftover window and nothing
    // else — named after RAM it cannot free. The window is dealt with by Open.
    stubAction(project({ state: "stopped", window_open: false }));
    render(Page);
    expect(await screen.findByRole("button", { name: "Open" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
  });

  it("shows what each running container is using", async () => {
    stubFetch({
      ...PROJECTS,
      projects: [
        project({ name: "acme", mem_bytes: 1_500_000_000 }),
        project({ name: "example-one", state: "stopped", mem_bytes: null }),
        project({ name: "greenfield", state: "absent", mem_bytes: null }),
      ],
    });
    render(Page);

    expect(await screen.findByText("1.5 GB")).toBeInTheDocument();
    // A stopped or absent container is using nothing, and "—" beside a state
    // that already says "absent" would be noise.
    expect(screen.queryByText("—")).not.toBeInTheDocument();
  });

  it("hides Open when a running container already has a window", async () => {
    // Open would only reuse the container and re-focus a window the user can
    // already see, so the row offers Close alone.
    stubAction(project({ state: "running", window_open: true }));
    render(Page);
    expect(await screen.findByRole("button", { name: "Close" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open" })).not.toBeInTheDocument();
  });

  it("keeps Open for a stopped container that still has a window", async () => {
    // A stale window can outlive its container (a refused close, say). Reopening
    // is still the useful action, so the button stays.
    stubAction(project({ state: "stopped", window_open: true }));
    render(Page);
    expect(await screen.findByRole("button", { name: "Open" })).toBeInTheDocument();
  });

  it("hides Open for a running container while the screen is locked", async () => {
    // Every window reads as closed while the screen is locked, so devdash
    // cannot tell whether one is already open — it declines to guess rather
    // than stack a second window nobody can see.
    stubAction(project({ state: "running", window_open: false }), {}, 200, true);
    render(Page);
    expect(await screen.findByRole("button", { name: "Close" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open" })).not.toBeInTheDocument();
  });

  it("keeps Open for a stopped project while the screen is locked", async () => {
    // Nothing is running, so there is no window to duplicate. This is the case
    // that matters: opening a project from a phone while the Mac is locked.
    stubAction(project({ state: "stopped" }), {}, 200, true);
    render(Page);
    expect(await screen.findByRole("button", { name: "Open" })).toBeInTheDocument();
  });

  it("keeps Open for an absent project while the screen is locked", async () => {
    stubAction(project({ state: "absent", container: null }), {}, 200, true);
    render(Page);
    expect(await screen.findByRole("button", { name: "Open" })).toBeInTheDocument();
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
