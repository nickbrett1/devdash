# devdash

A devdash project generated with genproj

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

That ambiguity is the whole reason `/api/projects` carries the raw
`window_titles` count next to the per-project `window_open` flags: a `0` while
you know windows are open means the screen is locked, not that a grant was
lost.

M2's Close is still best-effort, but for the ordinary reason — a "save your
changes?" sheet can block a close — not because the LaunchAgent cannot see
windows.

## The API

Read-only, GET, behind the token:

| Route | Answer |
| --- | --- |
| `/healthz` | `{"status":"ok"}` — the one route with no token |
| `/api/projects` | the joined rows, plus `window_error` and `window_titles` |
| `/api/status` | VM memory and container counts |
| `/api/repos` | repos with no workspace here yet, plus a listing error if any |
| `/api/jobs/<id>` | one job's state and log |
| `/api/jobs` | the jobs still running |

Mutating, POST only — a prefetcher or a back button must not be able to stop a
container, so no action is reachable by GET:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"force":true}' \
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

### Jobs

`open` and `provision` run `devcontainer up`, which takes minutes on a first
build — longer than a phone's HTTP request, or the browser that made it, will
wait. So they do not answer with an outcome:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"repo":"nickbrett1/acme"}' "http://<tailnet-ip>:3990/api/provision"
# 202 {"job_id":"42c6b745c9a7","kind":"provision","target":"nickbrett1/acme",
#      "state":"running","log":[], ...}

curl -H "Authorization: Bearer $TOKEN" "http://<tailnet-ip>:3990/api/jobs/42c6b745c9a7"
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
project does not exist yet). It passes `tailscale=False` unconditionally and
leaves `fresh`/`clean` off unless the body asks for them: every one of
devopen's prompts is a question a server cannot answer, so it is a parameter
with a safe default instead.

## On a phone

The page is installable (a manifest, a `standalone` display mode and an
apple-touch icon in `web/static`), pulls to refresh, and keeps every control at
the 44 px minimum. The manifest is fetched same-origin, so the browser sends
the token cookie with it and `start_url: "/"` keeps the secret out of the
installed app's URL — the cookie is the credential.

Each running project carries a **Blink** link, built as
`blink://host/?host=<project>&username=<remoteUser>&port=22`. devopen registers
the container on Tailscale under the workspace name, so the link and the
project agree by construction; `remoteUser` is read from the repo's
devcontainer config, defaulting to `vscode` as devopen does. It is only offered
while the container is running, because that is the only time the name
resolves.

That link is also why `open` now registers Tailscale when devopen's config has
an authkey: reaching a project from the phone is most of the point of opening
it from the phone, and `tailscale up` without a key wants a browser a server
cannot provide. No key means silence, not a hang.

There are no CLI shims here. `devdash` is a server, installed as a LaunchAgent
(`install.py`), so there is nothing for a `pip install` to shadow.

## Generated by genproj

This project was generated using the genproj tool.
