"""The background job manager: order, progress, cancelling, failures and the job cap."""

from __future__ import annotations

import threading
import time

import pytest

from clipstudio import engine
from clipstudio.jobs import LOG_HINT, Job, JobCancelled, JobFailed, JobManager, JobQueueFull

WAIT = 5.0


@pytest.fixture
def logs():
    return []


@pytest.fixture
def manager(logs):
    made = JobManager(log=logs.append)
    yield made
    made.shutdown()


class Gate:
    """A job function that waits until the test lets it through."""

    def __init__(self, result=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = result

    def __call__(self, job: Job):
        self.entered.set()
        assert self.release.wait(WAIT), "the test never released this job"
        job.check()
        return self.result


def test_a_job_runs_and_reports_its_result(manager):
    def work(job: Job):
        job.update("Reading the video details", 0.25)
        return {"answer": 42}

    job = manager.submit("analyze", work)
    done = manager.wait(job.id, WAIT)
    assert done == {
        "id": job.id,
        "kind": "analyze",
        "state": "done",
        "step": "Done",
        "progress": 1.0,
        "result": {"answer": 42},
    }
    assert job.created <= job.started <= job.finished


def test_ids_are_unguessable_and_different(manager):
    ids = {manager.submit("analyze", lambda job: None).id for _ in range(5)}
    assert len(ids) == 5
    assert all(len(job_id) >= 16 for job_id in ids)


def test_a_waiting_job_is_queued_with_no_result_or_error(manager):
    gate = Gate()
    first = manager.submit("analyze", gate)
    second = manager.submit("analyze", lambda job: "second")
    assert gate.entered.wait(WAIT)
    assert manager.snapshot(first.id)["state"] == "running"
    assert manager.snapshot(second.id) == {
        "id": second.id,
        "kind": "analyze",
        "state": "queued",
        "step": "Waiting to start",
        "progress": 0.0,
    }
    gate.release.set()
    assert manager.wait(second.id, WAIT)["result"] == "second"


def test_step_and_progress_are_visible_while_running_and_progress_is_clamped(manager):
    seen = threading.Event()
    carry_on = threading.Event()

    def work(job: Job):
        job.update("Listening for loud moments", 0.4)
        seen.set()
        carry_on.wait(WAIT)
        job.update(progress=7)
        job.update(progress=-3)
        return None

    job = manager.submit("render", work)
    assert seen.wait(WAIT)
    snapshot = manager.snapshot(job.id)
    assert (snapshot["state"], snapshot["step"], snapshot["progress"]) == ("running", "Listening for loud moments", 0.4)
    assert "result" not in snapshot and "error" not in snapshot
    carry_on.set()
    assert manager.wait(job.id, WAIT)["progress"] == 1.0


def test_jobs_of_one_kind_run_one_at_a_time_in_the_order_they_arrived(manager):
    order: list[int] = []
    running = 0
    most_at_once = 0
    lock = threading.Lock()

    def make(number: int):
        def work(job: Job):
            nonlocal running, most_at_once
            with lock:
                running += 1
                most_at_once = max(most_at_once, running)
            time.sleep(0.01)
            order.append(number)
            with lock:
                running -= 1

        return work

    jobs = [manager.submit("analyze", make(number)) for number in range(8)]
    for job in jobs:
        manager.wait(job.id, WAIT)
    assert order == list(range(8))
    assert most_at_once == 1


def test_an_analyze_job_and_a_render_job_run_at_the_same_time(manager):
    analyze, render = Gate("a"), Gate("r")
    first = manager.submit("analyze", analyze)
    second = manager.submit("render", render)
    assert analyze.entered.wait(WAIT) and render.entered.wait(WAIT)
    assert manager.snapshot(first.id)["state"] == manager.snapshot(second.id)["state"] == "running"
    assert len(manager.active()) == 2
    analyze.release.set()
    render.release.set()
    assert manager.wait(first.id, WAIT)["result"] == "a"
    assert manager.wait(second.id, WAIT)["result"] == "r"
    assert manager.active() == []


def test_cancelling_a_waiting_job_stops_it_before_it_starts(manager):
    gate = Gate()
    ran = []
    manager.submit("analyze", gate)
    waiting = manager.submit("analyze", lambda job: ran.append("waiting"))
    last = manager.submit("analyze", lambda job: ran.append("last"))
    assert gate.entered.wait(WAIT)
    assert manager.cancel(waiting.id) == {"id": waiting.id, "state": "cancelled"}
    assert waiting.cancelled
    gate.release.set()
    manager.wait(last.id, WAIT)
    assert ran == ["last"]
    snapshot = manager.snapshot(waiting.id)
    assert snapshot["state"] == "cancelled" and "result" not in snapshot


def test_cancelling_a_running_job_stops_it_at_the_next_step(manager):
    reached = threading.Event()
    carry_on = threading.Event()
    steps = []

    def work(job: Job):
        steps.append("one")
        reached.set()
        carry_on.wait(WAIT)
        job.update("Step two", 0.5)  # raises JobCancelled here
        steps.append("two")

    job = manager.submit("render", work)
    assert reached.wait(WAIT)
    assert not job.cancelled
    answer = manager.cancel(job.id)
    assert answer == {"id": job.id, "state": "running"}  # it stops after the step it is in
    assert job.cancelled
    assert manager.snapshot(job.id)["step"] == "Stopping after the current step"
    carry_on.set()
    final = manager.wait(job.id, WAIT)
    assert final["state"] == "cancelled" and final["step"] == "Cancelled"
    assert "result" not in final and "error" not in final
    assert steps == ["one"]


def test_check_raises_only_after_a_cancel(manager):
    job = Job("analyze", threading.RLock())
    job.check()
    job._cancel.set()
    with pytest.raises(JobCancelled):
        job.check()


def test_cancelling_a_finished_or_unknown_job_changes_nothing(manager):
    job = manager.submit("analyze", lambda job: "kept")
    manager.wait(job.id, WAIT)
    assert manager.cancel(job.id) == {"id": job.id, "state": "done"}
    assert manager.snapshot(job.id)["result"] == "kept"
    assert manager.cancel("no-such-job") is None
    assert manager.snapshot("no-such-job") is None
    assert manager.get("no-such-job") is None


def test_a_job_that_raises_becomes_an_error_with_friendly_text_and_the_traceback_only_in_the_log(manager, logs):
    def work(job: Job):
        job.update("Reading the video details", 0.1)
        raise RuntimeError("ERROR: Private video. Sign in if you've been granted access")

    job = manager.submit("analyze", work)
    failed = manager.wait(job.id, WAIT)
    assert failed["state"] == "error"
    assert failed["error"] == {"message": "This video is private, so it cannot be opened.", "hint": LOG_HINT}
    assert failed["error"]["message"] == engine.friendly_error(RuntimeError("Private video"))
    assert "result" not in failed
    assert "Traceback" not in str(failed)
    log_text = "\n".join(logs)
    assert "Traceback (most recent call last)" in log_text and "RuntimeError" in log_text


def test_engine_messages_written_for_the_user_are_passed_on(manager):
    def work(job: Job):
        raise ValueError("This stream is still live. Wait until it has ended, then paste the link again.")

    failed = manager.wait(manager.submit("analyze", work).id, WAIT)
    assert (
        failed["error"]["message"] == "This stream is still live. Wait until it has ended, then paste the link again."
    )


def test_job_failed_carries_its_own_message_and_hint(manager):
    def work(job: Job):
        raise JobFailed("That video file is no longer on hand.", "Choose the file again.")

    failed = manager.wait(manager.submit("analyze", work).id, WAIT)
    assert failed["error"] == {"message": "That video file is no longer on hand.", "hint": "Choose the file again."}


@pytest.mark.parametrize(
    "error",
    [
        KeyError("heatmap"),
        TypeError("'NoneType' object is not subscriptable"),
        AttributeError("x"),
        ZeroDivisionError(),
    ],
)
def test_program_mistakes_get_a_general_message_not_their_own_text(manager, logs, error):
    def work(job: Job):
        raise error

    failed = manager.wait(manager.submit("render", work).id, WAIT)
    assert failed["state"] == "error"
    assert failed["error"]["message"] == "Something went wrong on this computer while doing that."
    assert failed["error"]["hint"] == LOG_HINT
    assert type(error).__name__ in "\n".join(logs)


def test_a_broken_error_describer_still_gives_a_message(logs):
    def broken(error):
        raise RuntimeError("the describer broke")

    def os_failure(job: Job):
        raise OSError("disk trouble")

    manager = JobManager(log=logs.append, friendly_error=broken)
    try:
        failed = manager.wait(manager.submit("analyze", os_failure).id, WAIT)
        assert failed["error"]["message"] == "Something went wrong on this computer while doing that."
        assert "the describer broke" in "\n".join(logs)
    finally:
        manager.shutdown()


def test_long_or_multi_line_error_text_is_trimmed_to_one_line(logs):
    manager = JobManager(log=logs.append, friendly_error=lambda error: "line one\nline two " + "x" * 1000)
    try:

        def work(job: Job):
            raise RuntimeError("boom")

        message = manager.wait(manager.submit("analyze", work).id, WAIT)["error"]["message"]
        assert "\n" not in message and message.startswith("line one line two") and len(message) <= 400
    finally:
        manager.shutdown()


def test_the_worker_keeps_going_after_a_failure(manager):
    def bad(job: Job):
        raise RuntimeError("boom")

    first = manager.submit("analyze", bad)
    second = manager.submit("analyze", lambda job: "fine")
    assert manager.wait(first.id, WAIT)["state"] == "error"
    assert manager.wait(second.id, WAIT) == {
        "id": second.id,
        "kind": "analyze",
        "state": "done",
        "step": "Done",
        "progress": 1.0,
        "result": "fine",
    }


def test_even_system_exit_inside_a_job_does_not_stop_the_worker(manager):
    def bad(job: Job):
        raise SystemExit(3)

    first = manager.submit("analyze", bad)
    second = manager.submit("analyze", lambda job: "fine")
    assert manager.wait(first.id, WAIT)["state"] == "error"
    assert manager.wait(second.id, WAIT)["state"] == "done"


def test_old_finished_jobs_are_forgotten_at_the_cap(logs):
    manager = JobManager(max_jobs=5, log=logs.append)
    try:
        jobs = []
        for number in range(12):
            job = manager.submit("analyze", lambda job, number=number: number)
            manager.wait(job.id, WAIT)
            jobs.append(job)
            assert manager.count() <= 5
        assert manager.count() == 5
        assert [manager.snapshot(job.id) is None for job in jobs] == [True] * 7 + [False] * 5
        assert manager.snapshot(jobs[-1].id)["result"] == 11
    finally:
        manager.shutdown()


def test_default_cap_is_fifty_jobs(manager):
    last = None
    for _ in range(60):
        last = manager.submit("analyze", lambda job: None)
        manager.wait(last.id, WAIT)
    assert manager.count() == 50
    assert manager.snapshot(last.id)["state"] == "done"


def test_running_and_waiting_jobs_are_never_forgotten(logs):
    manager = JobManager(max_jobs=3, max_waiting=10, log=logs.append)
    gate = Gate("first")
    try:
        finished = manager.submit("render", lambda job: "old")
        manager.wait(finished.id, WAIT)
        running = manager.submit("analyze", gate)
        assert gate.entered.wait(WAIT)
        waiting = manager.submit("analyze", lambda job: "second")
        # The cap is reached. The finished job makes room; the running and waiting ones stay.
        extra = manager.submit("analyze", lambda job: "third")
        assert manager.snapshot(finished.id) is None
        with pytest.raises(JobQueueFull):
            manager.submit("analyze", lambda job: "fourth")
        gate.release.set()
        assert manager.wait(running.id, WAIT)["result"] == "first"
        assert manager.wait(waiting.id, WAIT)["result"] == "second"
        assert manager.wait(extra.id, WAIT)["result"] == "third"
    finally:
        gate.release.set()
        manager.shutdown()


def test_only_so_many_jobs_of_a_kind_may_wait(logs):
    manager = JobManager(max_waiting=2, log=logs.append)
    gate = Gate()
    try:
        manager.submit("analyze", gate)
        assert gate.entered.wait(WAIT)
        manager.submit("analyze", lambda job: None)
        manager.submit("analyze", lambda job: None)
        with pytest.raises(JobQueueFull):
            manager.submit("analyze", lambda job: None)
        manager.submit("render", lambda job: None)  # the other kind has its own queue
    finally:
        gate.release.set()
        manager.shutdown()


def test_unknown_kind_is_refused(manager):
    with pytest.raises(ValueError):
        manager.submit("upload", lambda job: None)


def test_on_finish_runs_after_every_job_and_its_failure_is_only_logged(logs):
    finished = []

    def on_finish(job: Job):
        finished.append((job.kind, job.state))
        raise RuntimeError("tidying up broke")

    manager = JobManager(log=logs.append, on_finish=on_finish)
    try:

        def bad(job: Job):
            raise RuntimeError("boom")

        first = manager.submit("analyze", lambda job: 1)
        second = manager.submit("analyze", bad)
        third = manager.submit("analyze", lambda job: 3)
        for job in (first, second, third):
            manager.wait(job.id, WAIT)
        deadline = time.monotonic() + WAIT
        while len(finished) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert finished == [("analyze", "done"), ("analyze", "error"), ("analyze", "done")]
        assert "tidying up broke" in "\n".join(logs)
    finally:
        manager.shutdown()


def test_snapshots_are_safe_to_take_from_many_threads(manager):
    stop = threading.Event()
    problems: list[BaseException] = []

    def work(job: Job):
        for number in range(200):
            job.update(f"Step {number}", number / 200)
        return list(range(10))

    job = manager.submit("analyze", work)

    def reader():
        try:
            while not stop.is_set():
                snapshot = manager.snapshot(job.id)
                assert snapshot["state"] in ("queued", "running", "done")
                assert 0.0 <= snapshot["progress"] <= 1.0
                assert ("result" in snapshot) == (snapshot["state"] == "done")
        except BaseException as error:
            problems.append(error)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()
    manager.wait(job.id, WAIT)
    stop.set()
    for thread in readers:
        thread.join(WAIT)
    assert problems == []


def test_wait_times_out_on_a_stuck_job(manager):
    gate = Gate()
    job = manager.submit("analyze", gate)
    with pytest.raises(TimeoutError):
        manager.wait(job.id, 0.05)
    gate.release.set()
    manager.wait(job.id, WAIT)


def test_shutdown_cancels_what_is_left_and_refuses_new_jobs(logs):
    manager = JobManager(log=logs.append)
    gate = Gate()
    running = manager.submit("analyze", gate)
    waiting = manager.submit("analyze", lambda job: None)
    assert gate.entered.wait(WAIT)
    manager.shutdown()
    assert manager.snapshot(waiting.id)["state"] == "cancelled"
    gate.release.set()
    assert manager.wait(running.id, WAIT)["state"] == "cancelled"
    with pytest.raises(JobQueueFull):
        manager.submit("analyze", lambda job: None)
