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

### Open: the LaunchAgent cannot see windows (yet)

Measured, not assumed. A one-shot LaunchAgent in `gui/501` running the venv
python — whose realpath *is* the granted Cellar binary — reports 87 processes
from System Events but `0` windows for TextEdit and Finder, which both had
windows open. An `osascript` from a Background-session shell reports the same.
So the grant currently reaches neither context: **window enumeration returns
`0` for everything, everywhere**, regardless of which path is used to launch
the interpreter.

Two consequences:

* `/api/projects` reports `window_titles: 0` and `window_open: false` for
  every project. That is the honest answer from where devdash runs, and the
  reason that raw count is on the page at all — a `0` here while VS Code
  visibly has windows open means the grant is not in effect.
* **M2's Close must not assume System Events works.** Closing the window is
  best-effort in devreap too; devdash has to stop the container, attempt the
  close, and report which half succeeded rather than failing the request.

Fixing it is a TCC question, not a code question (grant Accessibility, or
re-grant if the existing entry has gone stale), and nothing here will be
believed until a window count other than `0` shows up on the page.

## Generated by genproj

This project was generated using the genproj tool.
