"""Where a container sits on the tailnet, and the Blink host for it.

Every case here is about *degrading*: tailscale is optional in a devcontainer,
and a project list must not break because one container has none. Synthetic
names throughout — this repo is public.
"""

import os
import subprocess

import pytest

from devdash import tailnet


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def no_blink_key(monkeypatch):
    """Blink keys live in devopen's config; a test must not read the machine's."""
    monkeypatch.setattr(tailnet, "_blink_key", lambda: None)


def _docker(stdout, returncode=0):
    return lambda args, timeout=15: _completed(stdout, returncode=returncode)


def test_facts_reads_a_connected_container(monkeypatch):
    monkeypatch.setattr(tailnet, "_docker",
                        _docker("BACKEND=Running\nIP=100.64.0.9\n"))
    assert tailnet.facts("c1") == {"state": "connected", "ip": "100.64.0.9"}


def test_facts_reports_a_logged_out_container(monkeypatch):
    monkeypatch.setattr(tailnet, "_docker", _docker("BACKEND=NeedsLogin\nIP=\n"))
    assert tailnet.facts("c1") == {"state": "logged_out", "ip": None}


def test_facts_reports_a_stopped_node_as_not_on_the_tailnet(monkeypatch):
    """`tailscale down` leaves a node that Register can bring back."""
    monkeypatch.setattr(tailnet, "_docker", _docker("BACKEND=Stopped\nIP=\n"))
    assert tailnet.facts("c1") == {"state": "logged_out", "ip": None}


def test_facts_says_absent_when_there_is_no_tailscale(monkeypatch):
    monkeypatch.setattr(tailnet, "_docker", _docker("STATE=absent\n"))
    assert tailnet.facts("c1") == {"state": "absent", "ip": None}


def test_facts_degrades_when_docker_is_unreachable(monkeypatch):
    def boom(args, timeout=15):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(tailnet, "_docker", boom)
    assert tailnet.facts("c1") == {"state": "absent", "ip": None}


def test_facts_degrades_on_a_broken_probe(monkeypatch):
    # tailscaled not up yet, or the exec failed: no STATE line at all.
    monkeypatch.setattr(tailnet, "_docker", _docker("failed to connect to local tailscaled", 1))
    assert tailnet.facts("c1") == {"state": "absent", "ip": None}


def _probe_with(tmp_path, script):
    """Run the real probe against a stub `tailscale` on PATH.

    The old probe's failure lived entirely in shell logic, so a mocked
    `_docker` could never have caught it: it fed the parser a verdict the
    shell would never have produced.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tailscale"
    stub.write_text(script)
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
    out = subprocess.run(["sh", "-c", tailnet._PROBE], check=False,
                         capture_output=True, text=True, env=env)
    return tailnet._parse(out.stdout)


def test_probe_does_not_call_a_dead_daemon_connected(tmp_path):
    """The bug behind a container that "started" but never joined the tailnet.

    `tailscale status` with no daemon prints "failed to connect to local
    tailscaled" — non-empty text that the old heuristic read as a healthy
    node. It is exactly the state of a container whose tailscaled has not
    started, so the open job skipped registration and reported success.
    """
    state, ip = _probe_with(tmp_path, "#!/bin/sh\n"
                            "echo 'failed to connect to local tailscaled' >&2\n"
                            "exit 1\n")
    assert (state, ip) == ("absent", None)


def test_probe_reads_the_json_backend_state(tmp_path):
    state, ip = _probe_with(
        tmp_path,
        "#!/bin/sh\n"
        "if [ \"$1\" = ip ]; then echo 100.64.0.9; exit 0; fi\n"
        "echo '{\"BackendState\": \"Running\"}'\n",
    )
    assert (state, ip) == ("connected", "100.64.0.9")


def test_probe_treats_a_running_node_without_an_address_as_absent():
    """Running is not reachable; an ssh target needs an address."""
    assert tailnet._state("Running", None) == "absent"
    assert tailnet._state("NeedsLogin", None) == "logged_out"
    assert tailnet._state("", None) == "absent"       # daemon did not answer
    assert tailnet._state("Starting", None) == "absent"


def test_wait_for_tailscale_returns_a_state_once_the_daemon_answers(monkeypatch):
    states = iter(["absent", "absent", "logged_out"])
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": next(states), "ip": None})
    monkeypatch.setattr(tailnet.time, "sleep", lambda seconds: None)
    assert tailnet.wait_for_tailscale("c1", timeout=10) == "logged_out"


def test_wait_for_tailscale_gives_up_after_the_timeout(monkeypatch):
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": "absent", "ip": None})
    monkeypatch.setattr(tailnet.time, "sleep", lambda seconds: None)
    assert tailnet.wait_for_tailscale("c1", timeout=0) == "absent"


def test_register_starts_the_daemon_and_accepts_a_self_reconnect(monkeypatch):
    """A node with persisted state reconnects as soon as tailscaled runs — no
    login URL, nothing to tap, and `tailscale up` was never needed."""
    states = iter(["logged_out", "connected", "connected"])
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": next(states), "ip": None})
    monkeypatch.setattr(tailnet, "present", lambda cid: True)
    started = {"daemon": False, "up": False}

    def fake_docker(args, timeout=15):
        if args[-1] == tailnet._START_DAEMON:
            started["daemon"] = True
            return _completed("")
        started["up"] = True
        return _completed("")

    monkeypatch.setattr(tailnet, "_docker", fake_docker)
    monkeypatch.setattr(tailnet.time, "sleep", lambda seconds: None)
    out = tailnet.register("c1", "acme")
    assert started["daemon"] is True and started["up"] is False
    assert out["started"] is False
    assert "reconnected" in out["detail"]


def test_present_degrades_when_docker_is_unreachable(monkeypatch):
    def boom(args, timeout=15):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(tailnet, "_docker", boom)
    assert tailnet.present("c1") is False


def test_connection_builds_the_blink_link_and_ssh_for_a_connected_node(monkeypatch, tmp_path):
    monkeypatch.setattr(tailnet, "_docker", _docker("STATE=connected\nIP=100.64.0.9\n"))
    conn = tailnet.connection("c1", str(tmp_path / "acme"))
    assert conn["state"] == "connected"
    assert conn["host"] == "acme"
    assert conn["user"] == "vscode"  # no devcontainer config -> the default
    # The stable MagicDNS name, not the IP, so the target survives a re-register.
    assert conn["ssh"] == "ssh vscode@acme"
    assert conn["blink"] == "blink://host/?host=acme&username=vscode&port=22"


def test_connection_reads_remote_user_and_the_blink_key(monkeypatch, tmp_path):
    ws = tmp_path / "acme"
    (ws / ".devcontainer").mkdir(parents=True)
    (ws / ".devcontainer" / "devcontainer.json").write_text('{"remoteUser": "dev"}')
    monkeypatch.setattr(tailnet, "_docker", _docker("STATE=connected\nIP=100.64.0.9\n"))
    monkeypatch.setattr(tailnet, "_blink_key", lambda: "id_ed25519")

    conn = tailnet.connection("c1", str(ws))

    assert conn["user"] == "dev"
    assert "username=dev" in conn["blink"]
    assert "key=id_ed25519" in conn["blink"]


def test_connection_offers_nothing_to_reach_when_logged_out(monkeypatch, tmp_path):
    monkeypatch.setattr(tailnet, "_docker", _docker("STATE=logged_out\nIP=\n"))
    conn = tailnet.connection("c1", str(tmp_path / "acme"))
    assert conn["state"] == "logged_out"
    assert conn["ssh"] is None and conn["blink"] is None


def test_register_reports_an_already_connected_node(monkeypatch):
    monkeypatch.setattr(tailnet, "_docker", _docker("STATE=connected\nIP=100.64.0.9\n"))
    out = tailnet.register("c1", "acme")
    assert out["started"] is False
    assert "already registered" in out["detail"]


def test_register_returns_the_login_url_once_it_appears(monkeypatch):
    """`tailscale up` blocks until a browser authenticates; the detached run's
    URL is what the phone needs, and it is read back from the container."""
    calls = []

    def fake_docker(args, timeout=15):
        calls.append(args)
        if "exec" in args and "-d" in args:
            return _completed("")  # the detached start
        # The first read has the URL.
        return _completed("To authenticate, visit:\n\thttps://login.tailscale.com/a/abc123\n")

    monkeypatch.setattr(tailnet, "_docker", fake_docker)
    # `facts` runs first and must say not-connected, or register short-circuits.
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": "logged_out", "ip": None})
    out = tailnet.register("c1", "acme")
    assert out["started"] is True
    assert out["url"] == "https://login.tailscale.com/a/abc123"
    assert any("-d" in c for c in calls), "registration must be started detached"


def test_register_clears_a_stale_up_without_killing_its_own_shell(monkeypatch):
    """The start script must not `pkill -f 'tailscale up'`.

    It runs under `sh -c "<script>"`, so the shell's own argv contains the
    literal text "tailscale up". A `-f` pattern therefore matches and SIGTERMs
    the very shell running it: the following `rm`/`nohup` never execute, no log
    file is written, and `register` reports "no login URL yet" against a node
    that has one. Match by process name instead.
    """
    seen = {}

    def fake_docker(args, timeout=15):
        if "exec" in args and "-d" in args:
            seen["script"] = args[-1]
            return _completed("")
        return _completed("To authenticate, visit:\n\thttps://login.tailscale.com/a/abc123\n")

    monkeypatch.setattr(tailnet, "_docker", fake_docker)
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": "logged_out", "ip": None})
    tailnet.register("c1", "acme")
    assert "pkill -x tailscale" in seen["script"]
    assert "pkill -f" not in seen["script"], (
        "a `-f` pattern matches this script's own shell and kills it before "
        "it can start `tailscale up`")


def test_register_falls_back_to_status_when_the_log_never_lands(monkeypatch):
    """If the detached run's output is missing, `tailscale status` still prints
    the login URL for a logged-out node — read it rather than reporting nothing."""
    def fake_docker(args, timeout=15):
        if "exec" in args and "-d" in args:
            return _completed("")  # the detached start
        if args[-1] == "status":
            return _completed("Logged out.\nLog in at: https://login.tailscale.com/a/zzz9\n")
        return _completed("")  # the (empty) log file

    monkeypatch.setattr(tailnet, "_docker", fake_docker)
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": "logged_out", "ip": None})
    monkeypatch.setattr(tailnet.time, "sleep", lambda seconds: None)
    out = tailnet.register("c1", "acme", wait=0.01)
    assert out["url"] == "https://login.tailscale.com/a/zzz9"


def test_wait_connected_returns_true_once_the_container_connects(monkeypatch):
    states = iter(["logged_out", "logged_out", "connected"])
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": next(states), "ip": None})
    monkeypatch.setattr(tailnet.time, "sleep", lambda seconds: None)
    assert tailnet.wait_connected("c1", timeout=10, interval=0.01) is True


def test_wait_connected_gives_up_while_still_logged_out(monkeypatch):
    """A server waits, but not forever: an ignored login prompt must not hold a
    job (and its 409) open all afternoon."""
    calls = []
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": "logged_out", "ip": None})
    monkeypatch.setattr(tailnet.time, "sleep", lambda seconds: calls.append(seconds))
    assert tailnet.wait_connected("c1", timeout=0) is False
    assert calls == []  # the deadline is checked before sleeping again


def test_wait_connected_stops_early_when_the_container_disappears(monkeypatch):
    """`absent` mid-wait means the container is gone — there is nothing left to
    authenticate, and holding the job for the full timeout would block a retry."""
    monkeypatch.setattr(tailnet, "facts", lambda cid: {"state": "absent", "ip": None})
    monkeypatch.setattr(tailnet.time, "sleep", lambda seconds: (_ for _ in ()).throw(
        AssertionError("must not keep polling a container that is gone")))
    assert tailnet.wait_connected("c1", timeout=600) is False
