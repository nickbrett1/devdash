"""Background jobs, for work that outlives an HTTP request.

Opening a project runs `git fetch`, `devcontainer up` and a VS Code launch.
The first build of a container takes minutes, and a phone's HTTP request — or
the browser that made it — will not wait that long. So a long action starts a
job and answers immediately with an id; the log is polled from there.

Two things this is deliberately not:

  * **Not a queue.** There is one user and one machine. A job is a thread with
    a buffer, and a second request for the same target is refused rather than
    serialised behind the first.
  * **Not durable.** A restart loses running jobs, and the honest thing is to
    say so rather than pretend a job survived. Nothing here is safe to resume
    anyway: `devcontainer up` is the part you would have to redo.
"""

import threading
import time
import uuid

# The log is a tail, not a transcript. A capped buffer is what keeps a poll
# every second from being an unbounded read, and the beginning of a devcontainer
# build is the part nobody needs.
MAX_LINES = 400
MAX_JOBS = 40

_jobs = {}
_lock = threading.Lock()


class Job:
    def __init__(self, kind, target):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.target = target
        self.state = "running"
        self.result = None
        self.error = None
        self.started = time.time()
        self.finished = None
        self._lines = []
        # A job can stop and ask for something a phone has to do — completing a
        # Tailscale login, for instance. That is not a log line (a URL buried in
        # a build transcript is not tappable) and not a failure; it is structured
        # state the UI renders as a prompt. None when there is nothing to do.
        self._attention = None
        self._lock = threading.Lock()

    def attention(self, payload):
        """Set (or clear, with None) what this job is waiting on the human for."""
        with self._lock:
            self._attention = payload

    def log(self, line):
        """The `on_log` sink devopen and devreap write to. Called from the job's
        own thread, so the buffer has its own lock — the snapshot reader is a
        different thread and must never see a half-appended line."""
        with self._lock:
            self._lines.append(str(line))
            del self._lines[:-MAX_LINES]

    def finish(self, result=None, error=None):
        with self._lock:
            self.state = "failed" if error is not None else "done"
            self.result = result
            self.error = error
            self.finished = time.time()
            # A finished job is no longer waiting on anyone: the prompt would be
            # a stale "please authenticate" over a job that has already moved on.
            self._attention = None

    def snapshot(self):
        with self._lock:
            return {
                "job_id": self.id,
                "kind": self.kind,
                "target": self.target,
                "state": self.state,
                "log": list(self._lines),
                "attention": self._attention,
                "result": self.result,
                "error": self.error,
                "running_for": (self.finished or time.time()) - self.started,
            }

    @property
    def done(self):
        return self.state != "running"


def _sweep():
    """Drop the oldest finished jobs once the table is full. Caller holds the
    lock. Running jobs are never dropped — there would be nothing left to poll."""
    if len(_jobs) <= MAX_JOBS:
        return
    for job in sorted(_jobs.values(), key=lambda j: j.started):
        if len(_jobs) <= MAX_JOBS:
            break
        if job.done:
            del _jobs[job.id]


def start(kind, target, fn):
    """Run `fn(on_log=…, on_attention=…)` in a thread. Returns the first snapshot.

    `fn`'s return value becomes `result`; anything it raises becomes `error`,
    because a job thread that dies silently is worse than a job that fails —
    the phone keeps polling a job that will never change.
    """
    job = Job(kind, target)
    with _lock:
        _jobs[job.id] = job
        _sweep()

    def run():
        try:
            result = fn(on_log=job.log, on_attention=job.attention)
        except Exception as e:  # noqa: BLE001 — a job must always reach a state
            job.finish(error=f"{type(e).__name__}: {e}")
        else:
            job.finish(result=result)

    thread = threading.Thread(target=run, name=f"devdash-{kind}-{job.id}", daemon=True)
    thread.start()
    return job.snapshot()


def get(job_id):
    with _lock:
        job = _jobs.get(job_id)
    return job.snapshot() if job else None


def running(kind=None, target=None):
    """Any state other than done/failed, optionally filtered to one target."""
    with _lock:
        jobs = list(_jobs.values())
    return [j.snapshot() for j in jobs
            if not j.done
            and (kind is None or j.kind == kind)
            and (target is None or j.target == target)]


def clear():
    """Tests only: the registry is module-level state."""
    with _lock:
        _jobs.clear()
