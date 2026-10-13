"""devdash's MCP surface: an agent can open, close and rebuild a container.

The server is already reachable from the tailnet and already carries every
action an agent needs — devdash is a front end over devopen and devreap, and
these tools are a thin projection of the same functions the phone's buttons
call. Nothing here re-implements an action; it re-labels one.

Two things this deliberately is:

  * **In-process.** The endpoint is a route on the running server, so a job an
    agent starts is the *same* job the phone polls — one registry, one truth.
    A separate MCP process would keep its own `jobs` dict and the dashboard
    would never see the build the agent asked for.
  * **Stdlib JSON-RPC.** MCP is JSON-RPC 2.0 over a single POST. The transport
    here is stateless JSON (no SSE, no session): the server never initiates a
    request, so neither is needed. The official `mcp` SDK would add a
    dependency tree to a server whose whole point is that it stops
    memory-hungry containers with nothing installed but Python.

The tools are annotated honestly. `destructiveHint` defaults to *true* in MCP,
so every tool states it explicitly: `open` is not destructive, `rebuild` and
`close` are, and a client that honours the hint can prompt before either.

REST semantics do not carry over. On the HTTP surface a refusal is a `200`
(declining to act is a well-formed request); here a refusal is `isError: true`,
because the consumer is a model that should *report* a veto rather than narrate
success. Only "it did the thing" is `isError: false`.
"""

import json
import time
from collections import namedtuple
from dataclasses import dataclass

from . import __version__, actions, jobs, projects

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "devdash", "version": __version__}

# How long `get_job` will block. Bounded well under any client's tool timeout,
# because a tool call that outlives the client is a call whose result is lost.
MAX_WAIT = 120

INSTRUCTIONS = (
    "Open, close and rebuild the devcontainers on this Mac. Long actions "
    "(open, rebuild, register_on_tailnet, provision) return immediately with a "
    "job_id — poll get_job to follow them. If a job carries an attention.url, "
    "hand it to the human: it is a Tailscale login only they can complete. A "
    "result with isError=true is a refusal or a failure; report it, do not "
    "work around it."
)

# A tool's answer: the machine-readable object, a human/model-readable line,
# and whether it counts as an error to the agent (refusals included).
ToolResult = namedtuple("ToolResult", "structured text is_error")


@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    annotations: dict
    handler: object
    default: bool = True  # provision_project sets False

    @property
    def required(self):
        return self.input_schema.get("required", [])


# -- text the model reads ------------------------------------------------------


def _project_line(row):
    bits = [f"{row.get('name')}: {row.get('state')}"]
    conn = row.get("connection") or {}
    if conn.get("ssh"):
        bits.append(f"[{conn['ssh']}]")
    if row.get("live_session"):
        bits.append("(live session)")
    mem = row.get("mem_bytes")
    if mem:
        bits.append(f"{mem // (1024 * 1024)}MiB")
    return " ".join(bits)


def _job_line(snap):
    parts = [f"{snap.get('kind')} {snap.get('target')}: {snap.get('state')}"]
    attention = snap.get("attention") or {}
    if attention.get("url"):
        parts.append(f"ACTION REQUIRED — open {attention['url']}")
    elif attention.get("detail"):
        parts.append(f"ACTION REQUIRED — {attention['detail']}")
    if snap.get("error"):
        parts.append(f"error: {snap['error']}")
    log = snap.get("log") or []
    if log:
        parts.append(f"last: {log[-1]}")
    if snap.get("state") == "running":
        parts.append(f"poll get_job job_id={snap.get('job_id')}")
    return " | ".join(parts)


# -- tool handlers -------------------------------------------------------------
#
# Every handler reuses an `actions`/`projects`/`jobs` function. The only logic
# here is presentation (what the model reads) and the `projects.invalidate()`
# that follows a mutating call, so a phone poll after an agent's build never
# shows the list the build just changed.


def _open_runner(name, fresh):
    """The job body for open/rebuild: run devopen, then drop the cached list —
    including on failure, where half a container may be up."""
    def run(on_log, on_attention):
        try:
            return actions.open_project(name, fresh=fresh, on_log=on_log,
                                        on_attention=on_attention)
        finally:
            projects.invalidate()
    return run


def _tool_list_projects(args, cfg):
    rows, meta = projects.rows(probe_builds=False)
    text = "\n".join(_project_line(r) for r in rows) or "No projects."
    if meta.get("screen_locked"):
        text += "\n(screen is locked — window state reads as closed)"
    return ToolResult({"projects": rows, **meta}, text, False)


def _tool_get_status(args, cfg):
    structured = projects.status()
    return ToolResult(structured, json.dumps(structured, default=str), False)


def _tool_get_job(args, cfg):
    job_id = args["job_id"]
    wait = min(int(args.get("wait_seconds") or 0), MAX_WAIT)
    snap = jobs.get(job_id)
    if snap is None:
        return ToolResult({"job_id": job_id},
                          f"No job '{job_id}' — jobs are not durable across a restart.", True)
    deadline = time.monotonic() + wait
    while snap["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.5)
        snap = jobs.get(job_id) or snap
    return ToolResult(snap, _job_line(snap), snap["state"] == "failed")


def _tool_open_project(args, cfg):
    name = args["name"]
    snapshot, already = jobs.start_unique("open", name,
                                          _open_runner(name, bool(args.get("fresh"))))
    text = _job_line(snapshot)
    if already:
        text = f"{name} is already being opened; following the existing job. " + text
    return ToolResult(snapshot, text, False)


def _tool_rebuild_project(args, cfg):
    name = args["name"]
    snapshot, already = jobs.start_unique("open", name, _open_runner(name, True))
    if already:
        return ToolResult(
            snapshot,
            f"{name} is already being opened or rebuilt; following the existing job "
            "rather than starting a second one. " + _job_line(snapshot), False)
    return ToolResult(snapshot, "Rebuilding — " + _job_line(snapshot), False)


def _tool_close_project(args, cfg):
    name = args["name"]
    result = actions.close_project(name, force=bool(args.get("force")))
    projects.invalidate()
    if result.get("refused"):
        return ToolResult(
            result,
            f"Refused: {result.get('detail') or 'a live session is attached'}. "
            "Re-issue with force=true only if stopping it is intended.", True)
    text = f"{name}: stopped={result.get('stopped')} window_closed={result.get('window_closed')}"
    if result.get("detail"):
        text += f" — {result['detail']}"
    return ToolResult(result, text, False)


def _tool_register_on_tailnet(args, cfg):
    name = args["name"]

    def run(on_log, on_attention):
        return actions.register_tailscale(name, on_log=on_log, on_attention=on_attention)

    snapshot, already = jobs.start_unique("tailscale", name, run)
    text = _job_line(snapshot)
    if already:
        text = f"{name} is already registering; following the existing job. " + text
    return ToolResult(snapshot, text, False)


def _tool_provision_project(args, cfg):
    repo = args["repo"]

    def run(on_log, on_attention):
        try:
            return actions.provision(repo, on_log=on_log, on_attention=on_attention)
        finally:
            projects.invalidate()

    snapshot, already = jobs.start_unique("provision", repo, run)
    text = _job_line(snapshot)
    if already:
        text = f"{repo} is already being provisioned; following the existing job. " + text
    return ToolResult(snapshot, text, False)


# -- the registry --------------------------------------------------------------
#
# `readOnlyHint` is set on the read tools; every other hint is stated on every
# tool because MCP's defaults are not the safe ones (`destructiveHint` defaults
# to true). `provision_project` is default=False — see the README's MCP section.

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
              "idempotentHint": True, "openWorldHint": False}
_NOT_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": False}

_TOOLS = [
    Tool(
        name="list_projects",
        title="List projects",
        description=(
            "List the devcontainers on this machine with their state (running, "
            "stopped or absent), memory, whether a live session is attached, and "
            "how to reach a running one (ssh / blink / tailscale state). Call this "
            "first to find the project name to pass to the other tools."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=dict(_READ_ONLY),
        handler=_tool_list_projects,
    ),
    Tool(
        name="get_status",
        title="Host status",
        description="VM memory, the devcontainer footprint, and running/total counts.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=dict(_READ_ONLY),
        handler=_tool_get_status,
    ),
    Tool(
        name="get_job",
        title="Follow a job",
        description=(
            "State, log tail and any attention prompt for a job started by open, "
            "rebuild, register_on_tailnet or provision. Pass wait_seconds to block "
            "until it finishes (capped at 120s); otherwise it answers immediately."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The id the starting tool returned."},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT,
                                 "description": "Block up to this long for the job to finish."},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        annotations=dict(_READ_ONLY),
        handler=_tool_get_job,
    ),
    Tool(
        name="open_project",
        title="Open a project",
        description=(
            "Start a project's container and open a VS Code window on it, or "
            "attach a window to one already running. Not destructive. Returns a "
            "job_id to follow with get_job."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name, from list_projects."},
                "fresh": {"type": "boolean", "default": False,
                          "description": "Recreate the container instead of reusing it."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        annotations=dict(_NOT_DESTRUCTIVE),
        handler=_tool_open_project,
    ),
    Tool(
        name="close_project",
        title="Close a project",
        description=(
            "Stop a project's container and close its VS Code window, reclaiming "
            "its memory. Refuses by default when a live session (tmux, ssh) is "
            "attached; pass force=true to stop it anyway. A refusal is an error — "
            "report it rather than retrying with force."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name, from list_projects."},
                "force": {"type": "boolean", "default": False,
                          "description": "Stop even if a live session is attached."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": True, "openWorldHint": False},
        handler=_tool_close_project,
    ),
    Tool(
        name="rebuild_project",
        title="Rebuild a project",
        description=(
            "Delete a project's container and rebuild it from its devcontainer "
            "config (pulling origin first). DESTRUCTIVE: the container is "
            "recreated, though its volumes — and your workspace files — are not "
            "touched. Returns a job_id to follow with get_job."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name, from list_projects."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": False, "openWorldHint": False},
        handler=_tool_rebuild_project,
    ),
    Tool(
        name="register_on_tailnet",
        title="Register on the tailnet",
        description=(
            "Register a running container on the tailnet. Without an authkey this "
            "returns a job whose attention.url the human must open in a browser; "
            "relay it and poll get_job."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name, from list_projects."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        annotations={"readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False},
        handler=_tool_register_on_tailnet,
    ),
    Tool(
        name="provision_project",
        title="Provision a new project",
        description=(
            "Clone a GitHub repository, build its container and open a window — "
            "for a project that has no workspace here yet. DESTRUCTIVE and off by "
            "default: the build is arbitrary code from that repository. Only "
            "available when explicitly enabled in devdash's config."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repo": {"type": "string",
                         "description": "Repository as owner/name or a URL."},
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": False, "openWorldHint": True},
        handler=_tool_provision_project,
        default=False,
    ),
]

_BY_NAME = {tool.name: tool for tool in _TOOLS}


# -- protocol ------------------------------------------------------------------


def _error(rid, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def _result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _exposed(tool, cfg):
    """Whether this tool is on the surface. An empty/absent `mcp.tools` means
    the default set; a non-empty list is an explicit allowlist that replaces it
    — so provision_project is reachable only by naming it."""
    allow = (cfg.get("mcp") or {}).get("tools")
    if allow:
        return tool.name in allow
    return tool.default


def _exposed_tools(cfg):
    return [t for t in _TOOLS if _exposed(t, cfg)]


def _spec(tool):
    return {
        "name": tool.name,
        "title": tool.title,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "annotations": tool.annotations,
    }


def _tool_response(tr):
    return {
        "content": [{"type": "text", "text": tr.text}],
        "structuredContent": tr.structured if isinstance(tr.structured, dict) else {"value": tr.structured},
        "isError": bool(tr.is_error),
    }


def _initialize(params):
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": INSTRUCTIONS,
    }


def _tools_call(rid, params, cfg):
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(name, str):
        return _error(rid, -32602, "a tool name is required")
    tool = _BY_NAME.get(name)
    if tool is None or not _exposed(tool, cfg):
        return _error(rid, -32602, f"no such tool: {name}")
    if not isinstance(args, dict):
        return _error(rid, -32602, "arguments must be an object")
    missing = [key for key in tool.required if key not in args]
    if missing:
        return _error(rid, -32602, f"missing required argument(s): {', '.join(missing)}")
    try:
        tr = tool.handler(args, cfg)
    except actions.ActionError as e:
        tr = ToolResult({}, str(e), True)
    except Exception as e:  # noqa: BLE001 — a crashed tool is an error result, not a dead server
        tr = ToolResult({}, f"{type(e).__name__}: {e}", True)
    return _result(rid, _tool_response(tr))


def handle(message, cfg):
    """Dispatch one JSON-RPC message. Returns the response, or None for a
    notification (which has no reply). Transport errors (a body that is not
    JSON) are the caller's — the server answers -32700 before reaching here."""
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request")
    if "id" not in message:
        return None  # a notification: nothing to answer, and no side effect to run
    rid = message["id"]
    method = message.get("method")
    if not isinstance(method, str):
        return _error(rid, -32600, "invalid request")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _result(rid, _initialize(message.get("params") or {}))
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": [_spec(t) for t in _exposed_tools(cfg)]})
    if method == "tools/call":
        return _tools_call(rid, message.get("params") or {}, cfg)
    return _error(rid, -32601, f"method not found: {method}")
