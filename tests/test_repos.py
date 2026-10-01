"""The Provision picker's list.

Synthetic repos throughout (nickbrett1/acme, owner/greenfield) — devdash is a
public repo and a fixture naming a real private repository would publish it.
"""

import pytest

from devdash import repos


@pytest.fixture(autouse=True)
def clear_repo_cache():
    repos.clear_cache()
    yield
    repos.clear_cache()


def test_only_repos_without_a_workspace_are_offered(tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / "example-one").mkdir()
    (tmp_path / ".hidden").mkdir()

    available, error = repos.unprovisioned(
        str(tmp_path),
        repos=["nickbrett1/acme", "nickbrett1/greenfield", "nickbrett1/example-one"],
    )
    assert available == ["nickbrett1/greenfield"]
    assert error is None


def test_the_directory_name_is_what_matters_not_the_owner(tmp_path):
    """The workspace is keyed on the repo name, so a fork or a repo under
    another owner must still compare by its basename."""
    (tmp_path / "acme").mkdir()
    available, _ = repos.unprovisioned(str(tmp_path), repos=["someone-else/acme"])
    assert available == []


def test_a_repo_is_named_once_even_if_it_appears_twice(tmp_path):
    available, _ = repos.unprovisioned(
        str(tmp_path), repos=["nickbrett1/acme", "other/acme", "nickbrett1/beta"]
    )
    assert available == ["nickbrett1/acme", "nickbrett1/beta"]


def test_the_listing_is_capped(tmp_path):
    available, _ = repos.unprovisioned(
        str(tmp_path), repos=[f"o/r{i}" for i in range(20)], limit=5
    )
    assert len(available) == 5


def test_a_listing_error_is_returned_alongside_an_empty_answer(tmp_path):
    """The picker shows the error *and* nothing to provision, rather than 500ing
    or claiming there is nothing left to do."""
    available, error = repos.unprovisioned(str(tmp_path), repos=[], error="Network is unreachable")
    assert available == []
    assert error == "Network is unreachable"


def test_a_missing_workspaces_directory_offers_everything(tmp_path):
    """First run: nothing is checked out, so everything is provisionable —
    an OSError from listdir must not look like a failure."""
    available, error = repos.unprovisioned(str(tmp_path / "nope"), repos=["nickbrett1/acme"])
    assert available == ["nickbrett1/acme"]
    assert error is None


def test_the_raw_listing_is_cached(monkeypatch):
    calls = []

    def fake_list(**kw):
        calls.append(kw)
        return ["nickbrett1/acme"]

    monkeypatch.setattr(repos.devopen_repos, "list_repos", fake_list)
    monkeypatch.setattr(repos.devopen_config, "load", lambda: {"github_username": "nickbrett1"})

    repos._raw()
    repos._raw()
    assert len(calls) == 1
    # The configured account is used, not a hard-coded one.
    assert calls[0]["username"] == "nickbrett1"


def test_a_failed_listing_becomes_a_message_not_an_exception(monkeypatch):
    def boom(**kw):
        raise OSError("curl exploded")

    monkeypatch.setattr(repos.devopen_repos, "list_repos", boom)
    monkeypatch.setattr(repos.devopen_config, "load", lambda: {"github_username": "nickbrett1"})
    got, error = repos._raw()
    assert got == []
    assert "curl exploded" in error


def test_an_empty_listing_says_why(monkeypatch):
    """No token and no public repos is the most common empty answer, and it is
    indistinguishable from "everything is already checked out" unless the
    server says which."""
    monkeypatch.setattr(repos.devopen_repos, "list_repos", lambda **kw: [])
    monkeypatch.setattr(repos.devopen_config, "load", lambda: {"github_username": "nickbrett1"})
    got, error = repos._raw()
    assert got == []
    assert "no repositories returned" in error
