"""Deterministic unit coverage for the model-free inference actor."""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from bounded_inference_actor import (
    ActorClosedError,
    AdmissionError,
    BoundedInferenceActor,
    ResultModeError,
    ShutdownTimeoutError,
)


async def _wait_for_event(event: threading.Event, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() >= deadline:
            raise AssertionError("timed out waiting for worker event")
        await asyncio.sleep(0.001)


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("timed out waiting for actor state")
        await asyncio.sleep(0.001)


def test_actor_module_has_no_fastapi_or_model_imports() -> None:
    source_path = Path(__file__).resolve().parents[1] / "bounded_inference_actor.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint(
        {
            "fastapi",
            "numpy",
            "qwen_asr",
            "qwen_tts",
            "torch",
            "transformers",
            "uvicorn",
        }
    )


def test_exact_capacity_fifo_and_one_daemon_worker() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[int] = []
        worker_thread_ids: set[int] = set()

        def worker(value: int) -> int:
            calls.append(value)
            worker_thread_ids.add(threading.get_ident())
            if value == 0:
                started.set()
                assert release.wait(2)
            return value * 10

        actor: BoundedInferenceActor[int, int, bytes] = BoundedInferenceActor(
            worker, name="fifo-test"
        )
        try:
            first = actor.submit(0)
            await _wait_for_event(started)
            waiting = [actor.submit(value) for value in range(1, 5)]

            assert actor.snapshot().active == 1
            assert actor.snapshot().waiting == 4
            assert actor.snapshot().worker_alive is True
            assert actor._worker_thread.daemon is True  # noqa: SLF001 - thread-count invariant
            with pytest.raises(AdmissionError):
                actor.submit(5)

            release.set()
            results = await asyncio.gather(
                first.result(), *(handle.result() for handle in waiting)
            )

            assert results == [0, 10, 20, 30, 40]
            assert calls == [0, 1, 2, 3, 4]
            assert worker_thread_ids == {actor._worker_thread.ident}  # noqa: SLF001
            assert (
                sum(
                    thread.name == "fifo-test-actor" for thread in threading.enumerate()
                )
                == 1
            )
        finally:
            release.set()
            await actor.shutdown(timeout=1)

    asyncio.run(scenario())


def test_queued_cancel_removes_job_and_releases_one_admission_in_fifo_order() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[int] = []

        def worker(value: int) -> int:
            calls.append(value)
            if value == 0:
                started.set()
                assert release.wait(2)
            return value

        actor: BoundedInferenceActor[int, int, bytes] = BoundedInferenceActor(worker)
        try:
            first = actor.submit(0)
            await _wait_for_event(started)
            queued = [actor.submit(value) for value in range(1, 5)]

            assert queued[1].cancel() is True
            assert queued[1].cancel() is False
            replacement = actor.submit(5)
            assert actor.snapshot().waiting == 4
            with pytest.raises(asyncio.CancelledError):
                await queued[1].result()

            release.set()
            results = await asyncio.gather(
                first.result(),
                queued[0].result(),
                queued[2].result(),
                queued[3].result(),
                replacement.result(),
            )

            assert results == [0, 1, 3, 4, 5]
            assert calls == [0, 1, 3, 4, 5]
        finally:
            release.set()
            await actor.shutdown(timeout=1)

    asyncio.run(scenario())


def test_active_cancel_is_non_preemptive_but_discards_result_before_next_job() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        second_started = threading.Event()

        def worker(value: int) -> str:
            if value == 0:
                started.set()
                assert release.wait(2)
                return "must-be-discarded"
            second_started.set()
            return "second"

        actor: BoundedInferenceActor[int, str, bytes] = BoundedInferenceActor(worker)
        try:
            active = actor.submit(0)
            await _wait_for_event(started)
            second = actor.submit(1)

            assert active.cancel() is True
            assert active.cancel() is False
            with pytest.raises(asyncio.CancelledError):
                await active.result()
            await asyncio.sleep(0.02)
            assert second_started.is_set() is False
            assert actor.snapshot().active == 1

            release.set()
            assert await second.result() == "second"
            assert active.cancelled is True
        finally:
            release.set()
            await actor.shutdown(timeout=1)

    asyncio.run(scenario())


class _TrackedInfiniteIterator(Iterator[bytes]):
    def __init__(self) -> None:
        self.produced = 0
        self.closed = threading.Event()
        self.close_thread_id: int | None = None
        self.close_calls = 0

    def __next__(self) -> bytes:
        self.produced += 1
        return f"chunk-{self.produced}".encode()

    def close(self) -> None:
        self.close_calls += 1
        self.close_thread_id = threading.get_ident()
        self.closed.set()


def test_stream_buffer_is_bounded_and_aclose_closes_iterator_inside_worker() -> None:
    async def scenario() -> None:
        iterator = _TrackedInfiniteIterator()
        worker_thread_id: list[int] = []

        def worker(value: str):
            worker_thread_id.append(threading.get_ident())
            if value == "stream":
                return iterator
            return value.upper()

        actor: BoundedInferenceActor[str, str, bytes] = BoundedInferenceActor(
            worker, stream_buffer_size=2
        )
        try:
            streaming = actor.submit("stream")
            following = actor.submit("next")
            stream = streaming.stream()

            await _wait_for(lambda: iterator.produced >= 3)
            assert iterator.produced == 3
            assert actor.snapshot().active == 1
            assert await stream.__anext__() == b"chunk-1"
            await _wait_for(lambda: iterator.produced >= 4)

            await stream.aclose()  # type: ignore[attr-defined] - concrete stream supports cooperative close
            await _wait_for_event(iterator.closed)
            assert iterator.close_calls == 1
            assert iterator.close_thread_id == worker_thread_id[0]
            assert iterator.close_thread_id == actor._worker_thread.ident  # noqa: SLF001
            assert await following.result() == "NEXT"
            assert streaming.cancelled is True
        finally:
            await actor.shutdown(timeout=1)

    asyncio.run(scenario())


def test_scalar_and_stream_worker_exceptions_are_delivered_unchanged() -> None:
    async def scenario() -> None:
        scalar_error = ValueError("scalar failed")
        stream_error = LookupError("stream failed")

        class FailingStream(Iterator[bytes]):
            def __init__(self) -> None:
                self.iterations = 0
                self.close_calls = 0

            def __next__(self) -> bytes:
                self.iterations += 1
                if self.iterations == 1:
                    return b"first"
                raise stream_error

            def close(self) -> None:
                self.close_calls += 1

        failing_stream = FailingStream()

        def worker(value: str):
            if value == "scalar-error":
                raise scalar_error
            if value == "stream-error":
                return failing_stream
            return value

        actor: BoundedInferenceActor[str, str, bytes] = BoundedInferenceActor(worker)
        try:
            scalar = actor.submit("scalar-error")
            with pytest.raises(ValueError) as scalar_caught:
                await scalar.result()
            assert scalar_caught.value is scalar_error

            streaming = actor.submit("stream-error")
            stream = streaming.stream()
            assert await stream.__anext__() == b"first"
            with pytest.raises(LookupError) as stream_caught:
                await stream.__anext__()
            assert stream_caught.value is stream_error
            assert failing_stream.close_calls == 1

            scalar_result = actor.submit("ok")
            assert await scalar_result.result() == "ok"
            with pytest.raises(ResultModeError):
                await scalar_result.stream().__anext__()
        finally:
            await actor.shutdown(timeout=1)

    asyncio.run(scenario())


def test_successful_shutdown_joins_the_worker_after_stop_notification() -> None:
    class SlowReturningFuture(concurrent.futures.Future[None]):
        def set_result(self, result: None) -> None:
            super().set_result(result)
            time.sleep(0.04)

    async def scenario() -> None:
        actor: BoundedInferenceActor[int, int, bytes] = BoundedInferenceActor(
            lambda value: value
        )
        actor._stopped = SlowReturningFuture()  # noqa: SLF001 - opens the notification/thread-exit race

        started_at = asyncio.get_running_loop().time()
        await actor.shutdown(timeout=1)

        assert asyncio.get_running_loop().time() - started_at >= 0.03
        assert actor._worker_thread.is_alive() is False  # noqa: SLF001

    asyncio.run(scenario())


def test_shutdown_stops_admission_cancels_queue_and_waits_only_to_bound() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[int] = []

        def worker(value: int) -> int:
            calls.append(value)
            started.set()
            assert release.wait(2)
            return value

        actor: BoundedInferenceActor[int, int, bytes] = BoundedInferenceActor(worker)
        active = actor.submit(0)
        await _wait_for_event(started)
        queued = [actor.submit(value) for value in range(1, 5)]

        loop = asyncio.get_running_loop()
        started_shutdown = loop.time()
        with pytest.raises(ShutdownTimeoutError):
            await actor.shutdown(timeout=0.02)
        assert loop.time() - started_shutdown < 0.2
        assert actor.snapshot().accepting is False
        assert actor.snapshot().active == 1
        assert actor.snapshot().waiting == 0
        assert calls == [0]
        with pytest.raises(ActorClosedError):
            actor.submit(5)
        with pytest.raises(asyncio.CancelledError):
            await active.result()
        for handle in queued:
            with pytest.raises(asyncio.CancelledError):
                await handle.result()

        release.set()
        await actor.shutdown(timeout=1)
        assert actor.snapshot().worker_alive is False
        assert calls == [0]

    asyncio.run(scenario())
