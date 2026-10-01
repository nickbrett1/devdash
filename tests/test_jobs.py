"""The job registry — what a long action becomes when there is no tty.

The load-bearing property is that a job always reaches a terminal state. A job
thread that dies silently is worse than one that fails: the phone keeps polling
something that will never change.
"""

import threading
import time

import pytest

from devdash import jobs


@pytest.fixture(autouse=True)
def empty_registry():
    jobs.clear()
    yield
    jobs.clear()


def wait_for(job_id, timeout=5.0):
    deadline = time.time() + timeout
    while True:
        snapshot = jobs.get(job_id)
        if snapshot["state"] != "running" or time.time() > deadline:
            return snapshot
        time.sleep(0.01)


def test_a_job_records_its_result():
    started = jobs.start("open", "acme", lambda on_log: {"uri": "vscode-remote://x"})
    # Deliberately not asserting "running": the thread may already have finished,
    # and a caller that races the job must not see a broken snapshot.
    assert started["state"] in ("running", "done")
    assert started["kind"] == "open" and started["target"] == "acme"
    done = wait_for(started["job_id"])
    assert done["state"] == "done"
    assert done["result"] == {"uri": "vscode-remote://x"}
    assert done["error"] is None


def test_the_on_log_sink_becomes_the_log():
    def work(on_log):
        on_log("Cloning acme")
        on_log("Container ready")
        return "ok"

    done = wait_for(jobs.start("open", "acme", work)["job_id"])
    assert done["log"] == ["Cloning acme", "Container ready"]


def test_an_exception_fails_the_job_with_its_type():
    """The type is kept because 'ActionError: …' and 'OSError: …' mean different
    things to whoever is reading the log on a phone."""
    def work(on_log):
        raise ValueError("docker is not installed")

    done = wait_for(jobs.start("open", "acme", work)["job_id"])
    assert done["state"] == "failed"
    assert done["error"] == "ValueError: docker is not installed"
    assert done["result"] is None


def test_the_log_is_a_capped_tail():
    def work(on_log):
        for i in range(jobs.MAX_LINES + 50):
            on_log(f"line {i}")

    done = wait_for(jobs.start("open", "acme", work)["job_id"])
    assert len(done["log"]) == jobs.MAX_LINES
    # The tail, not the head — the beginning of a build is the part nobody needs.
    assert done["log"][-1] == f"line {jobs.MAX_LINES + 49}"


def test_get_returns_none_for_an_unknown_id():
    assert jobs.get("nope") is None


def test_running_filters_by_kind_and_target():
    release = threading.Event()
    stuck = jobs.start("open", "acme", lambda on_log: release.wait(5))
    jobs.start("open", "example-one", lambda on_log: None)
    try:
        assert [j["job_id"] for j in jobs.running(target="acme")] == [stuck["job_id"]]
        assert jobs.running(kind="provision") == []
        assert len(jobs.running(kind="open")) >= 1
    finally:
        release.set()
    # A finished job is not "running", which is what makes the 409 finite.
    wait_for(stuck["job_id"])
    assert jobs.running(target="acme") == []


def test_the_table_is_bounded_but_never_drops_a_running_job():
    release = threading.Event()
    stuck = jobs.start("open", "stuck", lambda on_log: release.wait(5))
    try:
        for i in range(jobs.MAX_JOBS + 10):
            done = jobs.start("open", f"p{i}", lambda on_log: None)
            wait_for(done["job_id"])
        assert len(jobs.running()) + len(_finished_ids()) <= jobs.MAX_JOBS + 1
        assert jobs.get(stuck["job_id"]) is not None
    finally:
        release.set()


def _finished_ids():
    # There is no public "all jobs" reader on purpose: the API only ever needs
    # running ones and a single id. The cap is checked through the private table.
    with jobs._lock:
        return [jid for jid, job in jobs._jobs.items() if job.done]
