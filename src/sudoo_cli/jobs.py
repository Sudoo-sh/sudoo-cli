"""Following a job to its end: live through the event stream, or by polling when the stream is not available
(an `instances` token may not open it, a proxy may cut it)."""

from __future__ import annotations

import http.client
import sys
import time
from typing import IO, Any

from .client import Client, parse_events
from .errors import JOB_FAILED, CliError

FINISHED = ("success", "failed")


def finished(job: dict[str, Any]) -> bool:
    return job.get("status") in FINISHED


class Progress:
    """Prints a line whenever the job's status, step or progress changes, and every new note (to stderr)."""

    def __init__(self, quiet: bool = False, out: IO[str] | None = None) -> None:
        self.quiet = quiet
        self.out = out or sys.stderr
        self.last: tuple[Any, ...] = ()
        self.notes = 0
        self.started = time.monotonic()

    def show(self, job: dict[str, Any]) -> None:
        if self.quiet:
            return
        steps = job.get("steps") or []
        current = next((s for s in steps if s.get("status") == "running"), steps[-1] if steps else {})
        state = (job.get("status"), current.get("title"), job.get("progress_percent"))
        if state != self.last:
            self.last = state
            percent = job.get("progress_percent")
            mark = f"{percent:>3}%" if isinstance(percent, int) else " ...".rjust(4)
            title = current.get("title") or job.get("action") or "Job"
            print(f"  [{mark}] {title} ({job.get('status')})", file=self.out, flush=True)
        notes = job.get("notes") or []
        for note in notes[self.notes :]:
            print(f"  note: {note if isinstance(note, str) else note.get('text', note)}", file=self.out, flush=True)
        self.notes = max(self.notes, len(notes))

    def done(self, job: dict[str, Any]) -> None:
        if self.quiet:
            return
        took = time.monotonic() - self.started
        if job.get("status") == "success":
            print(f"{job.get('action', 'Job')} finished in {took:.0f} s.", file=self.out)
        else:
            print(f"{job.get('action', 'Job')} failed: {job.get('error') or 'no reason given'}", file=self.out)


def follow(
    client: Client,
    job: dict[str, Any],
    *,
    quiet: bool = False,
    live: bool = True,
    poll_every: float = 2.0,
    max_wait: float | None = None,
) -> dict[str, Any]:
    """Wait until the job has finished; returns its last state."""
    progress = Progress(quiet)
    progress.show(job)
    deadline = None if max_wait is None else time.monotonic() + max_wait
    if not finished(job) and live:
        try:
            job = _live(client, job, progress, deadline)
        except (CliError, OSError, http.client.HTTPException) as error:
            client._debug(f"live events unavailable ({error}); polling the job instead")
    while not finished(job):
        if deadline is not None and time.monotonic() > deadline:
            raise CliError(f"The job {job['id']} has not finished yet (still {job.get('status')}).")
        client.sleep(poll_every)
        job = client.get(f"/jobs/{job['id']}").data
        progress.show(job)
    progress.done(job)
    return job


def _live(client: Client, job: dict[str, Any], progress: Progress, deadline: float | None) -> dict[str, Any]:
    job_id = job["id"]
    with client.open_events(["job"]) as stream:
        # Read the job again once the stream is open, so that nothing between the two is missed.
        job = client.get(f"/jobs/{job_id}").data
        progress.show(job)
        if finished(job):
            return job
        for event in parse_events(stream):
            if deadline is not None and time.monotonic() > deadline:
                break
            if event["event"] == "reset":
                job = client.get(f"/jobs/{job_id}").data
            elif event["event"] == "job" and isinstance(event["data"], dict) and event["data"].get("id") == job_id:
                job = event["data"]
            else:
                continue
            progress.show(job)
            if finished(job):
                return job
    return job


def check(job: dict[str, Any]) -> None:
    """Raise when a followed job failed (exit code JOB_FAILED)."""
    if job.get("status") == "failed":
        raise CliError(f"{job.get('action', 'Job')} failed: {job.get('error') or 'no reason given'}", JOB_FAILED)
