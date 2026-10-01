"""Which repos could be provisioned — the picker's list.

devopen.repos does the listing (a keychain token if there is one, else the
public API). This adds the one thing devdash knows and devopen does not: which
of those repos are already checked out, so the picker does not offer to
provision something that is sitting right there in the project list.
"""

import os
import time

from devopen import config as devopen_config
from devopen import repos as devopen_repos
from devreap import config as reap_config

from . import projects

# The listing is a keychain lookup plus up to three HTTPS calls through curl,
# and it changes on the scale of days. The picker is a convenience; a phone that
# opens it twice should not pay for it twice.
CACHE_TTL = 300

_cache = {"at": 0.0, "repos": [], "error": None}


def workspaces_dir():
    """Where workspaces live, from devreap's config — the same source the
    project list is built from, so the two can never disagree about what is
    already here."""
    cfg = reap_config.load()
    return os.path.expanduser(cfg.get("workspaces_dir") or "")


def _raw(ttl=CACHE_TTL):
    """The full listing, cached. Returns (repos, error)."""
    now = time.monotonic()
    if _cache["repos"] and now - _cache["at"] < ttl:
        return _cache["repos"], _cache["error"]
    username = (devopen_config.load().get("github_username") or "").strip()
    try:
        repos = devopen_repos.list_repos(username=username) if username else devopen_repos.list_repos()
        error = None
    except Exception as e:  # noqa: BLE001 — a listing failure is a message, not a 500
        repos, error = [], f"{type(e).__name__}: {e}"
    if not repos and error is None:
        error = "no repositories returned (no GitHub token and no public repos?)"
    _cache.update(at=now, repos=repos, error=error)
    return repos, error


def unprovisioned(workspaces_dir, repos=None, error=None, limit=100):
    """Repos with no workspace directory here yet, plus a listing error if the
    listing itself failed.

    `repos` is the raw `owner/name` list; omit it to use the cached fetch.
    Passing it is how the tests stay offline and how a caller can reuse one
    listing for two questions.
    """
    if repos is None:
        repos, error = _raw()
    have = set(projects._workspace_dirs(workspaces_dir))
    out, seen = [], set()
    for full in repos or []:
        name = full.rsplit("/", 1)[-1]
        if name in have or name in seen:
            continue
        seen.add(name)
        out.append(full)
    return out[:limit], error


def clear_cache():
    """Tests only."""
    _cache.update(at=0.0, repos=[], error=None)
