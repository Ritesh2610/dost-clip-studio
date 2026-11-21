"""Background jobs for Clip Studio.

Finding moments and rendering clips take minutes, so they run on worker threads
while the browser asks for progress. Each kind of job has one worker: one
"analyze" job and one "render" job can run at the same time, and further jobs of
a kind wait their turn in the order they arrived.

Cancelling is cooperative. A job's function receives the Job and calls
`job.check()` between steps; once a cancel has been asked for, that call raises
JobCancelled and the job ends as "cancelled".
"""

from __future__ import annotations

import queue
import secrets
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

MAX_JOBS = 50
MAX_WAITING_PER_KIND = 10
LOG_HINT = "The details are in clip-log.txt in the app folder."

QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"
FINISHED_STATES = (DONE, ERROR, CANCELLED)

# Failures of these kinds are mistakes in the program, not something the user did.
# Their text ("'NoneType' object has no attribute ...") helps nobody, so it stays in the log.
_PROGRAM_ERRORS = (TypeError, AttributeError, LookupError, ArithmeticError, AssertionError, NameError, ImportError)


class JobCancelled(Exception):
    """Raised inside a job when the user has asked to stop it."""


class JobQueueFull(Exception):
    """Too many jobs are already waiting."""


class JobFailed(Exception):
    """A failure with a message, and optionally a hint, that is already written for the user."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message, self.hint = message, hint


def default_friendly_error(error: BaseException) -> str:
    from . import engine  # looked up on each call so the engine can be replaced in tests

    return engine.friendly_error(error)  # type: ignore[arg-type]


class Job:
    """One piece of background work. Fields are changed only while holding the manager's lock."""

    def __init__(self, kind: str, lock: threading.RLock):
        self.id = secrets.token_urlsafe(12)
        self.kind = kind
        self.state = QUEUED
        self.step = "Waiting to start"
        self.progress = 0.0
        self.error: dict[str, str] | None = None
        self.result: Any = None
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self._lock = lock
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        """True once a cancel has been asked for."""
        return self._cancel.is_set()

    def check(self) -> None:
        """Call between steps: stops the job here if the user cancelled it."""
        if self._cancel.is_set():
            raise JobCancelled()

    def update(self, step: str | None = None, progress: float | None = None) -> None:
        """Sets the text and progress shown to the user, then stops here if the job was cancelled."""
        with self._lock:
            if self.state == RUNNING and not self._cancel.is_set():
                if step is not None:
                    self.step = str(step)
                if progress is not None:
                    self.progress = max(0.0, min(1.0, float(progress)))
        self.check()

    def snapshot(self) -> dict[str, Any]:
        """A copy of the job's public state, in the shape the HTTP API returns."""
        with self._lock:
            data: dict[str, Any] = {
                "id": self.id,
                "kind": self.kind,
                "state": self.state,
                "step": self.step,
                "progress": round(self.progress, 4),
            }
            if self.error is not None:
                data["error"] = dict(self.error)
            if self.state == DONE and self.result is not None:
                data["result"] = self.result
            return data


class JobManager:
    """Runs jobs one at a time per kind and remembers a limited number of them."""

    def __init__(
        self,
        kinds: tuple[str, ...] = ("analyze", "render"),
        *,
        max_jobs: int = MAX_JOBS,
        max_waiting: int = MAX_WAITING_PER_KIND,
        log: Callable[[str], None] = print,
        friendly_error: Callable[[BaseException], str] = default_friendly_error,
        on_finish: Callable[[Job], None] | None = None,
    ):
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._queues: dict[str, queue.Queue[tuple[Job, Callable[[Job], Any]] | None]] = {}
        self._threads: list[threading.Thread] = []
        self._max_jobs = max(1, int(max_jobs))
        self._max_waiting = max(1, int(max_waiting))
        self._log = log
        self._friendly_error = friendly_error
        self._on_finish = on_finish
        self._closed = False
        for kind in kinds:
            self._queues[kind] = queue.Queue()
            thread = threading.Thread(target=self._work, args=(kind,), name=f"clipstudio-{kind}", daemon=True)
            thread.start()
            self._threads.append(thread)

    # ------------------------------------------------------------ public

    def submit(self, kind: str, func: Callable[[Job], Any]) -> Job:
        """Queues `func(job)` and returns its Job. Raises JobQueueFull when too many are waiting."""
        if kind not in self._queues:
            raise ValueError(f"unknown job kind: {kind}")
        with self._lock:
            if self._closed:
                raise JobQueueFull("The app is shutting down.")
            waiting = sum(1 for job in self._jobs.values() if job.kind == kind and job.state == QUEUED)
            if waiting >= self._max_waiting:
                raise JobQueueFull("Too many jobs are waiting.")
            self._make_room()
            job = Job(kind, self._lock)
            self._jobs[job.id] = job
            # Queued under the lock, so the run order is always the order of arrival.
            self._queues[kind].put((job, func))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        return job.snapshot() if job else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """Asks a job to stop. A waiting job stops at once; a running one stops after its current step."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state == QUEUED:
                job._cancel.set()
                job.state, job.step, job.finished = CANCELLED, "Cancelled", time.time()
            elif job.state == RUNNING and not job.cancelled:
                job._cancel.set()
                job.step = "Stopping after the current step"
            return {"id": job.id, "state": job.state}

    def active(self) -> list[Job]:
        """Jobs that are waiting or running."""
        with self._lock:
            return [job for job in self._jobs.values() if job.state in (QUEUED, RUNNING)]

    def count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def wait(self, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
        """Blocks until the job has finished (used by tests and scripts). Raises TimeoutError."""
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.snapshot(job_id)
            if snapshot is None:
                raise KeyError(job_id)
            if snapshot["state"] in FINISHED_STATES:
                return snapshot
            if time.monotonic() >= deadline:
                raise TimeoutError(f"job {job_id} is still {snapshot['state']}")
            time.sleep(0.01)

    def shutdown(self) -> None:
        """Cancels everything and lets the workers end. Running steps are not interrupted."""
        with self._lock:
            self._closed = True
            for job in list(self._jobs.values()):
                self.cancel(job.id)
        for jobs in self._queues.values():
            jobs.put(None)

    # ----------------------------------------------------------- private

    def _make_room(self) -> None:
        """Forgets the oldest finished jobs so that one more fits under the cap."""
        while len(self._jobs) >= self._max_jobs:
            finished = [job for job in self._jobs.values() if job.state in FINISHED_STATES]
            if not finished:
                raise JobQueueFull("Too many jobs are waiting.")
            oldest = min(finished, key=lambda job: (job.finished or 0.0, job.created))
            del self._jobs[oldest.id]

    def _work(self, kind: str) -> None:
        jobs = self._queues[kind]
        while True:
            item = jobs.get()
            if item is None:
                return
            job, func = item
            with self._lock:
                if job.state != QUEUED:  # cancelled while it waited
                    continue
                job.state, job.started, job.step = RUNNING, time.time(), "Starting"
            self._log(f"[job] {job.kind} {job.id}: started")
            try:
                result = func(job)
            except JobCancelled:
                self._finish(job, CANCELLED, step="Cancelled")
            except BaseException as error:  # a job must never take its worker thread down with it
                self._log(f"[job] {job.kind} {job.id}: failed\n{traceback.format_exc().rstrip()}")
                self._finish(job, ERROR, error=self._describe(error))
            else:
                self._finish(job, DONE, step="Done", result=result)
            if self._on_finish is not None:
                try:
                    self._on_finish(job)
                except Exception:
                    self._log(f"[job] {job.kind} {job.id}: tidying up failed\n{traceback.format_exc().rstrip()}")

    def _finish(
        self, job: Job, state: str, *, step: str | None = None, result: Any = None, error: dict[str, str] | None = None
    ) -> None:
        with self._lock:
            job.state, job.finished = state, time.time()
            if step is not None:
                job.step = step
            if state == DONE:
                job.result, job.progress = result, 1.0
            if error is not None:
                job.error = error
        self._log(f"[job] {job.kind} {job.id}: {state} after {(job.finished or 0) - (job.started or 0):.1f}s")

    def _describe(self, error: BaseException) -> dict[str, str]:
        """The text shown to the user for a failed job. Never contains a traceback."""
        if isinstance(error, JobFailed):
            described = {"message": error.message}
            if error.hint:
                described["hint"] = error.hint
            return described
        message = ""
        if isinstance(error, Exception) and not isinstance(error, _PROGRAM_ERRORS):
            try:
                message = str(self._friendly_error(error) or "")
            except Exception:
                self._log(f"[job] could not describe an error\n{traceback.format_exc().rstrip()}")
        message = " ".join(message.split())[:400]
        if not message or "Traceback (most recent call last)" in message:
            message = "Something went wrong on this computer while doing that."
        return {"message": message, "hint": LOG_HINT}
