# devdash

A phone-sized dashboard for the devcontainers on this machine: which projects
exist, whether their container is running, which VS Code windows are open, and
what CI last said about them — plus the two things you actually want from a
phone, **Open** and **Close**.

It is a stdlib-only Python server (`ThreadingHTTPServer`) serving a static
SvelteKit build, installed as a launchd LaunchAgent and bound to the tailnet.
It reuses [devopen](https://github.com/nickbrett1/devopen) to open a project and
[devreap](https://github.com/nickbrett1/devreap) for the container and
VS Code-window facts, so it duplicates neither one's config.

## Capabilities

This project includes the following capabilities:

- **Doppler Secrets Management**: Integrates Doppler for secure secrets management. Enables the various MCP servers that rely on privileged tokens to access their services (e.g. Buildkite, GitHub, SonarQube).
- **AI Coding Agents**: Sets up the AI coding agents in the devcontainer: goose (config, MCP servers and spec-first recipes) plus the Antigravity CLI.
- **Container Agent**: Every generated devcontainer brings up and registers its own a2a-goose agent (`<repo>-dev`), reached over the tailnet by the LiteLLM proxy; reuses the a2a-goose GitHub release channel, so the container has the same self-update path as a host.
- **Docker**: Adds Docker support for containerised builds and tooling.
- **Python DevContainer**: Sets up a VS Code DevContainer with Python environment.
- **Node.js DevContainer**: Sets up a VS Code DevContainer with Node.js environment.
- **SvelteKit**: Initializes a SvelteKit project with Svelte 5.
- **Ruff (Python code quality)**: Adds fast, zero-configuration Python linting with Ruff (rules live in pyproject.toml [tool.ruff]). Lint locally with `ruff check`. Requires a Python devcontainer. The generated CI pipeline also runs `ruff check`.
- **Dependabot**: Configures Dependabot for automated dependency updates.
- **Buildkite Integration**: Runs CI on a self-hosted Buildkite agent (Apple silicon) instead of a metered cloud fleet. The pipeline and its GitHub webhook are created during generation, so there is no manual "set up project" step. It can coexist with an existing CI provider, so a repository can migrate without a flag day.

## Setup

1. Clone the repository
2. Create a virtualenv and install the package with dev extras:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. Run the checks:

   ```bash
   ruff check src tests
   pytest -v
   ```

## Install

`install.py` does the whole thing in one shot: it clones or updates the
checkout, builds a venv on `/opt/homebrew/bin/python3.14`, `pip install -e .`,
writes `~/.devdash/config.json`, builds the frontend with the pinned npm, and
loads the LaunchAgent (`launchd/com.nickbrett1.devdash.plist`, `KeepAlive` with
a `ThrottleInterval` so a crash cannot thrash). Re-run it to update; it is
idempotent. `uninstall.py` boots the agent out and removes the plist, leaving
the checkout and the config behind.

It runs standalone, so a fresh Mac needs no clone first:

```bash
curl -fsSL https://raw.githubusercontent.com/nickbrett1/devdash/main/install.py | python3
```

`DEVDASH_PYTHON`, `DEVDASH_HOME`, `DEVDASH_WORKSPACES` and the three
`DEVDASH_SKIP_*` switches (`UPDATE`, `WEB`, `AGENT`) override the defaults.
The interpreter matters: macOS grants Accessibility to `realpath(sys.executable)`,
so the venv is built on the one binary already granted, and the window close
works under launchd.

The plist carries an explicit `PATH` because launchd hands a job a bare
environment, and `ProcessType Interactive` so the Accessibility grant that
window-closing needs is used the same way a foreground app's is. Verify what is
running with:

```bash
launchctl list | grep devdash     # label, last exit status, pid
curl -s http://$(tailscale ip -4):3990/healthz
```

## Doppler

This project uses Doppler for secrets from the shared `common` project
(config `dev`) — no per-repo Doppler project is created. First use (links
the shared project and `dev` config):

```bash
doppler setup --project common --config dev
```

If your repo needs app-specific secrets that shouldn't live in the shared
`common` project, regenerate it with the doppler capability set to
`projectStrategy: "new"` to get a dedicated project.

The Doppler CLI is installed in the devcontainer — it must be on PATH for the
VS Code extension and `doppler run` to work. Auth is persisted via the host
`~/.doppler` bind-mount.

### Env-var precedence (read this if `doppler run` hits the wrong project)

Doppler resolves its target as **environment variables > `doppler.yaml` >
`~/.doppler` scoped config**. If your shell — or the session that launched
the devcontainer (e.g. an agent runtime) — exports `DOPPLER_PROJECT` /
`DOPPLER_CONFIG` / `DOPPLER_ENVIRONMENT`, those silently override this
repo's `doppler.yaml` and every `doppler` command targets the wrong
project. The devcontainer's post-create setup pins this repo's context
(`common`/`dev`) in `~/.bashrc` and `~/.zshrc` and warns at
setup if resolution still mismatches. To force the correct context manually:

```bash
unset DOPPLER_PROJECT DOPPLER_CONFIG DOPPLER_ENVIRONMENT
doppler setup --no-interactive --project common --config dev
```

## The container's agent

This devcontainer brings up its own `a2a-goose` agent, registered in the hub as
`devdash-dev` - one agent per repo, so a restart reclaims the same entry
instead of adding a second one. Turns are billed through the LiteLLM proxy
configured in Doppler (`LITELLM_BASE_URL`).

```bash
scripts/agent-dev.sh start    # write secrets + config, fetch the launcher, run it
scripts/agent-dev.sh status   # running or not, the card URL, the log tail
scripts/agent-dev.sh stop     # SIGTERM, wait for a clean deregister, confirm gone
```

`start` runs from the devcontainer's post-start hook, so the agent is normally
already up when you arrive. It fails open: with no network on a first start it
prints why it did not start and leaves the project usable. Secrets come from
Doppler into `~/.config/a2a-goose/env` (mode 0600) and never into the image or
`containerEnv`.

## Dependencies

devdash is a front end over two CLIs that already do the work:

| Need | Comes from |
| ---- | ---------- |
| Container enumeration, live-session veto, stop | `devreap.containers` |
| Closing the VS Code window | `devreap.vscode` |
| Days since the last human build | `devreap.buildkite` |
| Clone / `devcontainer up` / open a window | `devopen.opener` |

Both are declared as **git dependencies pinned to a tag** (`pyproject.toml`),
so devdash is reproducible and a push to either repo cannot move it. Nothing
is forked or vendored: the container, URI and window logic has exactly one
home.

### Developing against local checkouts

While working on devdash you usually want live edits from `~/DevOpen/devopen`
and `~/DevOpen/devreap`. Overlay editable installs on top of the pins:

```bash
.venv/bin/pip install -e ~/DevOpen/devopen -e ~/DevOpen/devreap
```

### Re-pinning a tag

Bumping `@v0.1.1` → `@v0.1.2` in `pyproject.toml` and running
`pip install -e .` does **nothing**: pip sees the distribution name already
satisfied and leaves the old commit installed. Install the ref explicitly,
then confirm what you got:

```bash
.venv/bin/pip install --force-reinstall --no-deps \
    "devreap @ git+https://github.com/nickbrett1/devreap@v0.1.2"
.venv/bin/pip freeze | grep devreap   # shows the commit the tag resolved to
```

Symptoms of forgetting this are an `AttributeError` for a function you can see
in the checkout, or a `TypeError` for one whose signature you just changed.

Run the venv on **`/opt/homebrew/bin/python3.14`** and nothing else. Closing a
VS Code window goes through System Events, and macOS grants Accessibility to
the *interpreter* — specifically to `realpath(sys.executable)`. That Homebrew
3.14 binary is the one already granted; a venv built on it resolves through to
the same binary, so the grant carries over. A venv on any other Python gets a
fresh prompt for an interpreter you did not mean to bless.

### A window count of 0 usually means the screen is locked

Not a permission problem — that was the first conclusion drawn here and it was
wrong. Measured with the display awake, the venv python under `gui/501`:

    AXIsProcessTrusted:  True
    list_window_titles:  (['a2a-goose [Dev Container: ...]', ... 9 titles], None)

Accessibility is granted to the Cellar binary and — because TCC matches a path
entry on *realpath* — the venv inherits it, which is what makes
`/opt/homebrew/bin/python3.14` the right interpreter to build on. What misled
the first measurement is that **System Events returns `[]` with no error while
the screen is locked or asleep**, exactly as it does when no window is open.
(`screencapture` fails with "could not create image from display" in the same
state, which is how the two cases were told apart.)

That ambiguity is why `/api/projects` carries the raw `window_titles` count
next to the per-project `window_open` flags — and why it also carries
`screen_locked`, read straight from `ioreg -n Root -d1`
(`CGSSessionScreenIsLocked` / `IOConsoleLocked`, ~50 ms). The count alone
cannot tell "nothing is open" from "nobody can see the screen"; the probe can,
so the strip says **windows hidden — screen locked** instead of showing a
confident `0` after an Open that actually worked.

M2's Close is still best-effort, but for the ordinary reason — a "save your
changes?" sheet can block a close — not because the LaunchAgent cannot see
windows.

## Access

There is no token, cookie or `?token=` URL. devdash binds to the tailnet
address and nothing else (see `config.bind_host()`: the configured `host`, else
`tailscale ip -4`, else `127.0.0.1` — never `0.0.0.0`), so the only thing that
can reach the port is a device on your tailnet. That bind *is* the access
control, and it is the whole of it: loopback on this host cannot reach the
server and there are no CORS headers, so a page in a browser has no route in
either. Bookmark `http://<tailnet-ip>:3990/` on the phone and you are done.

The residual risk is worth stating plainly: any other device on your tailnet
can reach these routes, including the mutating ones. If that ever matters, the
fix is an allow-list of tailnet identities, not a shared secret.

## The API

Read-only, GET:

| Route | Answer |
| --- | --- |
| `/healthz` | `{"status":"ok"}` |
| `/api/projects` | the joined rows — running, then stopped, then absent, each by name — plus `window_error`, `window_titles` and `screen_locked`. Each row carries `mem_bytes`, the memory that container is using (null when it is not running) |
| `/api/status` | VM memory, the devcontainer footprint, and how many devcontainers are running versus how many exist. Both counts are over the devcontainers, so `running_count <= container_count`: `docker stats` sees unrelated containers too, and counting those once made "running" exceed the total |
| `/api/repos` | repos with no workspace here yet, plus a listing error if any |
| `/api/jobs/<id>` | one job's state and log |
| `/api/jobs` | the jobs still running |

Mutating, POST only — a prefetcher or a back button must not be able to stop a
container, so no action is reachable by GET:

```bash
curl -X POST -H 'Content-Type: application/json' -d '{"force":true}' \
     "http://<tailnet-ip>:3990/api/projects/<name>/close"
```

`close` stops the container and closes its VS Code window. It refuses an
expensive mistake by default: if someone is sitting in the container
(`tmux`, `ssh`, a `who`-visible login) the answer is

```json
{"refused": true, "stopped": false,
 "detail": "live session (tmux: attached); pass force to stop anyway"}
```

— a `200`, because declining to act is a successful request. The two halves are
reported independently (`stopped`, `window_closed`) since a "save your changes?"
sheet can keep the window open while the RAM is reclaimed either way.

`close` also means "stop", never "remove": `docker stop` keeps the container
and its volumes, so `Open` brings the same container straight back.

### Which button a row offers

| state | window | screen | buttons |
|---|---|---|---|
| running | hidden | unlocked | `Close` `Open` |
| running | visible | unlocked | `Close` |
| running | either | locked | `Close` |
| stopped | either | either | `Open` |
| absent | — | either | `Open` |

`Close` appears only on a **running** container, because that is the only row
with RAM to reclaim. The API still accepts `close` on a stopped container — it
closes the leftover window and reports `stopped: true` for a stop that had
nothing to do — but the UI does not offer it: a button named after memory, on a
row using none, is a promise it cannot keep. A stale window is dealt with by
`Open`, which starts the container and attaches one anyway.

### Why `Open` is hidden on a running container that already has a window

`running` and `window_open` are different facts, and the UI keeps them apart: a
container can be up with nothing attached (the Dev Containers extension stops a
container when its window closes, but a container started by a terminal, or one
whose stop failed while its window closed, will not), and that container is
exactly the one worth reopening. So `Open` exists to attach a window, not to
start a project — it reuses a running container rather than rebuilding it
(`--fresh` is what rebuilds, and devdash never passes it unless asked).

But when the container is running *and* devdash can see its window, `Open`
would only re-focus something the user is already looking at, so the row offers
`Close` alone. It comes back for a stopped container that still has a stale
window, where reopening is the point.

A *running* container is also stripped of `Open` while `screen_locked` is
true. Every window reads as closed then, open or not, so the button would be a
guess — and the wrong guess stacks a second window on a Mac nobody can see.

`Open` is only ever withheld from a **running** container, though, because a
second window is the only thing it can get wrong. A stopped or absent project
has no container to duplicate, so the button always stands — including while
the screen is locked, which is exactly when reaching a project from a phone is
the point. Unlock and the running rows get their button back on the next poll.

### Jobs

`open` and `provision` run `devcontainer up`, which takes minutes on a first
build — longer than a phone's HTTP request, or the browser that made it, will
wait. So they do not answer with an outcome:

```bash
curl -X POST -H 'Content-Type: application/json' \
     -d '{"repo":"nickbrett1/acme"}' "http://<tailnet-ip>:3990/api/provision"
# 202 {"job_id":"42c6b745c9a7","kind":"provision","target":"nickbrett1/acme",
#      "state":"running","log":[], ...}

curl "http://<tailnet-ip>:3990/api/jobs/42c6b745c9a7"
# {"state":"failed","error":"ActionError: command failed (128): git clone …",
#  "log":["Cloning …","remote: Repository not found.", …], "running_for":0.5}
```

`state` is `running`, `done` or `failed`; `log` is the tail of devopen's own
output, which is the only thing that can explain a slow or failed build. A
second request for the same target is **`409`, not a queue** — a second
`devcontainer up` on a workspace already building would fight the first for the
same container name, and a phone would rather hear "already running" than wait
behind a build it did not start.

Jobs are not durable, deliberately. A restart loses them; `devcontainer up` is
the part you would have to redo anyway, so there is nothing to resume.

### Provision

`GET /api/repos` is the GitHub repos the configured account can see that have
**no workspace directory here yet**, so the picker never offers to provision
something already in the project list. The listing is a keychain lookup plus up
to three HTTPS calls through `curl`, cached for five minutes, and a listing
failure comes back as a message beside the answer rather than a 500 — the
picker still has something to show.

`open` hands off to `devopen.open_repo` (as does `provision`, whose only
difference is that it takes a *repository* rather than a project, because the
project does not exist yet). It registers the container on Tailscale whenever
devopen's config carries an authkey, and leaves `fresh`/`clean` off unless the
body asks for them: every one of devopen's prompts is a question a server
cannot answer, so it is a parameter with a safe default instead.

### Why the first load is the slow one

The two read endpoints are joins over reads that are individually slow and
collectively repeated — measured on this machine, warm:

| Read | Cost | Why |
| --- | --- | --- |
| `docker ps -a` + 4 inspects per container | ~1.2s | ~40 CLI invocations |
| `docker stats --no-stream` | ~1.9s | it samples; there is no cheaper way |
| `active_session` per running container | ~0.25s | three `docker exec`s |
| `osascript` × 2 (System Events) | ~0.4s | one call per poll |

None of them is *wrong*, and none of them changes much between two polls thirty
seconds apart. So they are sampled, not awaited: `projects._Memo` serves the
last value immediately and refreshes behind it, which makes a poll a join over
cached samples. Two consequences worth knowing:

- a value can be one TTL stale (`_containers` 3s, the docker stats 15s), and a
  failed refresh keeps the last good value rather than blanking the page;
- anything that changes the world — a `close`, or a `devcontainer up` that has
  finished — calls `projects.invalidate()`, so the reload that follows an action
  never shows the state the action just changed.

The independent waits that *are* paid are paid in parallel: the System Events
calls and the per-container session probes run at once, so `rows()` costs the
slowest read rather than the sum. `serve()` warms the memos at startup, off the
request path, so a restart does not make the next poll pay for the first
sample.

## On a phone

The page is installable (a manifest, a `standalone` display mode and an
apple-touch icon in `web/static`), pulls to refresh, and keeps every control at
the 44 px minimum. The manifest is fetched same-origin, so `start_url: "/"`
keeps the installed app pointed at the same tailnet URL you opened.

There was a **Blink** link on each running project. It never worked, so it is
gone: what the phone needs is the container's MagicDNS name, and that is
devopen's to register, not a URL for devdash to guess at.

That is why `open` registers Tailscale when devopen's config carries an
authkey: reaching a project from the phone is most of the point of opening it
from the phone — ssh, a terminal app or VS Code all need a name to aim at — and
`tailscale up` without a key wants a browser a server cannot provide. No key
means silence, not a hang.

There are no CLI shims here. `devdash` is a server, installed as a LaunchAgent
(`install.py`), so there is nothing for a `pip install` to shadow.

## The card on the NAS dashboard

`deploy/homepage-services.yaml` is the [Homepage](https://gethomepage.dev)
entry for devdash, for the **Activity** group of the dashboard on the NAS. It
is a service with an `href` (the click target, the full page) plus an `iframe`
widget pointing at `/tile.html`.

`/tile.html` is a route that renders `StatusStrip` and nothing else, so the
card carries exactly the same summary as the top of the page — the VM memory
meter with used *and* total, the devcontainer footprint, and the running/total
devcontainer counts — and cannot drift from it. A `customapi` widget was the first
attempt: it is a fixed list of label/value pairs, so it had no room for the
meter and could not show the total, only what is in use.

Both the iframe and the `href` are read by the **browser**, so both need a
tailnet route from the device looking at the dashboard; devdash binds to the
tailnet IP only, so the card opens from tailnet clients and fails from LAN-only
ones. `siteMonitor` is the exception — fetched server-side by the NAS, which
cannot resolve `mac-studio` (its only nameserver is the LAN router).

Homepage hot-reloads `services.yaml`, so no container restart is needed. The block in
`deploy/homepage-services.yaml` is indented to the NAS file's own style — group
items at four spaces, their keys at eight — so it can be pasted in unchanged.

## Generated by genproj

This project was generated using the genproj tool.
