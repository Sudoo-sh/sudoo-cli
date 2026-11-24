"""Following a job to its end: live through the event stream, or by polling when the stream is not available.

An `instances` token may not be allowed to open the stream, and a proxy may cut it. `follow` is the main entry
point; `check` turns a failed job into an error.
"""

from __future__ import annotations

import http.client
import sys
import time
from typing import IO, Any

from .client import Client, parse_events
from .errors import JOB_FAILED, CliError

# The job statuses that mean the job is over.
FINISHED = ("success", "failed")


def finished(job: dict[str, Any]) -> bool:
    """Tell whether a job has ended.

    Args:
        job: The job object from the panel.

    Returns:
        True when its status is "success" or "failed".
    """
    return job.get("status") in FINISHED


class Progress:
    """Prints a line whenever the job's status, step or progress changes, and every new note (to stderr).

    Attributes:
        quiet: When True nothing is printed.
        out: The stream to print to (stderr by default).
        last: The last status, step title and progress that were shown.
        notes: How many notes were shown already.
        started: Monotonic clock time at which the progress started, used for the duration in `done`.
    """

    def __init__(self, quiet: bool = False, out: IO[str] | None = None) -> None:
        """Start a progress display.

        Args:
            quiet: When True nothing is printed.
            out: The stream to print to; stderr when None.
        """
        self.quiet = quiet
        self.out = out or sys.stderr
        self.last: tuple[Any, ...] = ()
        self.notes = 0
        self.started = time.monotonic()

    def show(self, job: dict[str, Any]) -> None:
        """Print the job's line and any new notes, only when something changed since the last call.

        Args:
            job: The job object from the panel.
        """
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
        """Print the closing line: the duration on success, the reason on failure.

        Args:
            job: The final job object.
        """
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
    """Wait until the job has finished; returns its last state.

    Tries the live event stream first (unless `live` is False) and falls back to polling when the stream cannot be
    used.

    Args:
        client: The API client.
        job: The job object to follow.
        quiet: When True no progress lines are printed.
        live: When False, poll instead of using the event stream.
        poll_every: Seconds between two reads when polling.
        max_wait: Seconds after which to give up, or None to wait without a limit.

    Returns:
        The last state of the job.

    Raises:
        CliError: When `max_wait` passes before the job has finished.
    """
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
    """Follow a job through the event stream until it finishes, the stream ends or the deadline passes.

    Args:
        client: The API client.
        job: The job to follow.
        progress: Where to show the changes.
        deadline: Monotonic clock time to stop at, or None.

    Returns:
        The last known state of the job; it may not be finished if the stream ended.
    """
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
    """Raise when a followed job failed (exit code JOB_FAILED).

    Args:
        job: The final job object.

    Raises:
        CliError: When the job's status is "failed".
    """
    if job.get("status") == "failed":
        raise CliError(f"{job.get('action', 'Job')} failed: {job.get('error') or 'no reason given'}", JOB_FAILED)
