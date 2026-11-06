"""Bounded single-worker admission for process-local model inference.

The actor intentionally knows nothing about FastAPI or model libraries.  One
daemon thread owns every worker invocation, while asyncio callers are notified
through thread-safe futures rather than per-request threads.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Generic, TypeVar, Union

InputT = TypeVar("InputT")
ResultT = TypeVar("ResultT")
ChunkT = TypeVar("ChunkT")


class InferenceActorError(RuntimeError):
    """Base class for actor-owned failures."""


class AdmissionError(InferenceActorError):
    """Raised synchronously when one active plus the waiting bound is full."""


class ActorClosedError(InferenceActorError):
    """Raised synchronously after shutdown has stopped admission."""


class ResultModeError(InferenceActorError):
    """Raised when scalar and streaming consumption modes are mixed."""


class ShutdownTimeoutError(InferenceActorError):
    """Raised when the non-preemptible active worker exceeds shutdown's bound."""


class _JobState(Enum):
    QUEUED = "queued"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    FINISHED = "finished"


@dataclass(frozen=True)
class ActorSnapshot:
    accepting: bool
    active: int
    waiting: int
    worker_alive: bool


def _wake_async_waiter(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


class _StreamBuffer(Generic[ChunkT]):
    """A bounded thread-to-asyncio hand-off without executor waiters."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._condition = threading.Condition()
        self._items: Deque[ChunkT] = deque()
        self._finished = False
        self._cancelled = False
        self._error: BaseException | None = None
        self._async_waiters: set[
            tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]
        ] = set()

    def _notify_async_waiters_locked(self) -> None:
        waiters = tuple(self._async_waiters)
        self._async_waiters.clear()
        for loop, waiter in waiters:
            try:
                loop.call_soon_threadsafe(_wake_async_waiter, waiter)
            except RuntimeError:
                # A closed client loop is equivalent to an abandoned stream. The
                # handle cancellation path wakes the producer independently.
                continue

    def put(self, item: ChunkT) -> bool:
        with self._condition:
            while (
                len(self._items) >= self._capacity
                and not self._finished
                and not self._cancelled
                and self._error is None
            ):
                self._condition.wait()
            if self._finished or self._cancelled or self._error is not None:
                return False
            self._items.append(item)
            self._notify_async_waiters_locked()
            return True

    def finish(self) -> None:
        with self._condition:
            if self._finished or self._cancelled or self._error is not None:
                return
            self._finished = True
            self._condition.notify_all()
            self._notify_async_waiters_locked()

    def fail(self, error: BaseException) -> None:
        with self._condition:
            if self._finished or self._cancelled or self._error is not None:
                return
            self._error = error
            self._condition.notify_all()
            self._notify_async_waiters_locked()

    def cancel(self) -> None:
        with self._condition:
            if self._cancelled:
                return
            self._cancelled = True
            self._items.clear()
            self._condition.notify_all()
            self._notify_async_waiters_locked()

    async def get(self) -> ChunkT:
        while True:
            loop = asyncio.get_running_loop()
            with self._condition:
                if self._items:
                    item = self._items.popleft()
                    self._condition.notify_all()
                    return item
                if self._cancelled:
                    raise asyncio.CancelledError
                if self._error is not None:
                    raise self._error
                if self._finished:
                    raise StopAsyncIteration
                waiter = loop.create_future()
                waiter_key = (loop, waiter)
                self._async_waiters.add(waiter_key)
            try:
                await waiter
            finally:
                with self._condition:
                    self._async_waiters.discard(waiter_key)


class _InferenceStream(AsyncIterator[ChunkT], Generic[ResultT, ChunkT]):
    def __init__(self, handle: InferenceHandle[ResultT, ChunkT]) -> None:
        self._handle = handle
        self._closed = False

    def __aiter__(self) -> _InferenceStream[ResultT, ChunkT]:
        return self

    async def __anext__(self) -> ChunkT:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._handle._next_stream_chunk()
        except StopAsyncIteration:
            self._closed = True
            raise
        except asyncio.CancelledError:
            self._closed = True
            self._handle.cancel()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._handle.cancel()

    async def __aenter__(self) -> _InferenceStream[ResultT, ChunkT]:
        return self

    async def __aexit__(
        self, _exc_type: object, _exc: object, _traceback: object
    ) -> None:
        await self.aclose()

    def __del__(self) -> None:
        if not self._closed:
            self._handle.cancel()


class InferenceHandle(Generic[ResultT, ChunkT]):
    """One admitted inference and its scalar or streaming delivery."""

    def __init__(
        self, actor: BoundedInferenceActor[object, ResultT, ChunkT], sequence: int
    ) -> None:
        self._actor = actor
        self._sequence = sequence
        self._completion: concurrent.futures.Future[ResultT] = (
            concurrent.futures.Future()
        )
        self._stream_buffer = _StreamBuffer[ChunkT](actor.stream_buffer_size)
        self._delivery_lock = threading.Lock()
        self._cancel_requested = False
        self._stream_claimed = False
        self._job: _Job[object, ResultT, ChunkT] | None = None

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def cancelled(self) -> bool:
        with self._delivery_lock:
            return self._cancel_requested

    @property
    def done(self) -> bool:
        return self._completion.done()

    def cancel(self) -> bool:
        return self._actor._cancel_handle(self)

    async def result(self) -> ResultT:
        try:
            return await asyncio.wrap_future(self._completion)
        except asyncio.CancelledError:
            self.cancel()
            raise

    def stream(self) -> AsyncIterator[ChunkT]:
        with self._delivery_lock:
            if self._stream_claimed:
                raise ResultModeError("the inference stream can only be consumed once")
            self._stream_claimed = True
        return _InferenceStream(self)

    async def _next_stream_chunk(self) -> ChunkT:
        return await self._stream_buffer.get()

    def _attach_job(self, job: _Job[object, ResultT, ChunkT]) -> None:
        self._job = job

    def _request_cancel(self) -> None:
        with self._delivery_lock:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            self._completion.cancel()
        self._stream_buffer.cancel()

    def _publish_stream_mode(self) -> bool:
        with self._delivery_lock:
            if self._cancel_requested:
                return False
            if not self._completion.done():
                self._completion.set_exception(
                    ResultModeError("worker returned a stream; use stream()")
                )
            return True

    def _publish_scalar(self, result: ResultT) -> None:
        with self._delivery_lock:
            if self._cancel_requested:
                return
            if not self._completion.done():
                self._completion.set_result(result)
        self._stream_buffer.fail(
            ResultModeError("worker returned a scalar result; use result()")
        )

    def _publish_error(self, error: BaseException) -> None:
        with self._delivery_lock:
            if self._cancel_requested:
                return
            if not self._completion.done():
                self._completion.set_exception(error)
        self._stream_buffer.fail(error)

    def _finish_stream(self) -> None:
        self._stream_buffer.finish()

    def _fail_stream(self, error: BaseException) -> None:
        self._stream_buffer.fail(error)


@dataclass(eq=False)
class _Job(Generic[InputT, ResultT, ChunkT]):
    value: InputT
    handle: InferenceHandle[ResultT, ChunkT]
    state: _JobState = _JobState.QUEUED


WorkerResult = Union[ResultT, Iterator[ChunkT]]


class BoundedInferenceActor(Generic[InputT, ResultT, ChunkT]):
    """Run at most one inference with four waiting admissions by default."""

    def __init__(
        self,
        worker: Callable[[InputT], WorkerResult[ResultT, ChunkT]],
        *,
        max_waiting: int = 4,
        stream_buffer_size: int = 2,
        name: str = "inference",
    ) -> None:
        if not callable(worker):
            raise TypeError("worker must be callable")
        if (
            isinstance(max_waiting, bool)
            or not isinstance(max_waiting, int)
            or max_waiting < 0
        ):
            raise ValueError("max_waiting must be a non-negative integer")
        if (
            isinstance(stream_buffer_size, bool)
            or not isinstance(stream_buffer_size, int)
            or stream_buffer_size < 1
        ):
            raise ValueError("stream_buffer_size must be a positive integer")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")

        self._worker_callable = worker
        self._max_waiting = max_waiting
        self._stream_buffer_size = stream_buffer_size
        self._condition = threading.Condition()
        self._queue: Deque[_Job[InputT, ResultT, ChunkT]] = deque()
        self._active: _Job[InputT, ResultT, ChunkT] | None = None
        self._accepting = True
        self._next_sequence = 0
        self._stopped: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._worker_thread = threading.Thread(
            target=self._run,
            name=f"{name}-actor",
            daemon=True,
        )
        self._worker_thread.start()

    @property
    def max_waiting(self) -> int:
        return self._max_waiting

    @property
    def stream_buffer_size(self) -> int:
        return self._stream_buffer_size

    def snapshot(self) -> ActorSnapshot:
        with self._condition:
            return ActorSnapshot(
                accepting=self._accepting,
                active=int(self._active is not None),
                waiting=len(self._queue),
                worker_alive=self._worker_thread.is_alive(),
            )

    def submit(self, value: InputT) -> InferenceHandle[ResultT, ChunkT]:
        with self._condition:
            if not self._accepting:
                raise ActorClosedError("inference actor is closed")
            outstanding = int(self._active is not None) + len(self._queue)
            if outstanding >= 1 + self._max_waiting:
                raise AdmissionError("inference actor capacity is full")

            self._next_sequence += 1
            handle: InferenceHandle[ResultT, ChunkT] = InferenceHandle(
                self,  # type: ignore[arg-type]
                self._next_sequence,
            )
            job = _Job(value=value, handle=handle)
            handle._attach_job(job)  # type: ignore[arg-type]
            self._queue.append(job)
            self._condition.notify()
            return handle

    def _cancel_handle(self, handle: InferenceHandle[ResultT, ChunkT]) -> bool:
        with self._condition:
            job = handle._job
            if job is None or job.state in {_JobState.CANCELLED, _JobState.FINISHED}:
                return False
            if job.state is _JobState.QUEUED:
                try:
                    self._queue.remove(job)  # type: ignore[arg-type]
                except ValueError:
                    return False
            job.state = _JobState.CANCELLED
            handle._request_cancel()
            self._condition.notify_all()
            return True

    def _take_next(self) -> _Job[InputT, ResultT, ChunkT] | None:
        with self._condition:
            while not self._queue:
                if not self._accepting:
                    return None
                self._condition.wait()
            job = self._queue.popleft()
            if job.state is not _JobState.QUEUED:
                raise RuntimeError("inference actor dequeued a non-queued job")
            job.state = _JobState.ACTIVE
            self._active = job
            return job

    def _finish_scalar(
        self, job: _Job[InputT, ResultT, ChunkT], result: ResultT
    ) -> None:
        with self._condition:
            if job.state is not _JobState.CANCELLED:
                job.handle._publish_scalar(result)
                job.state = _JobState.FINISHED
            self._active = None
            self._condition.notify_all()

    def _finish_error(
        self, job: _Job[InputT, ResultT, ChunkT], error: BaseException
    ) -> None:
        with self._condition:
            if job.state is not _JobState.CANCELLED:
                job.handle._publish_error(error)
                job.state = _JobState.FINISHED
            self._active = None
            self._condition.notify_all()

    def _finish_stream(
        self,
        job: _Job[InputT, ResultT, ChunkT],
        error: BaseException | None,
    ) -> None:
        with self._condition:
            if job.state is not _JobState.CANCELLED:
                if error is None:
                    job.handle._finish_stream()
                else:
                    job.handle._fail_stream(error)
                job.state = _JobState.FINISHED
            self._active = None
            self._condition.notify_all()

    def _run_stream(
        self, job: _Job[InputT, ResultT, ChunkT], iterator: Iterator[ChunkT]
    ) -> None:
        error: BaseException | None = None
        if job.handle._publish_stream_mode():
            try:
                for chunk in iterator:
                    if not job.handle._stream_buffer.put(chunk):
                        break
            except BaseException as exc:
                error = exc
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                close()
            except BaseException as exc:
                if error is None:
                    error = exc
        self._finish_stream(job, error)

    def _execute(self, job: _Job[InputT, ResultT, ChunkT]) -> None:
        try:
            result = self._worker_callable(job.value)
        except BaseException as exc:
            self._finish_error(job, exc)
            return
        if isinstance(result, Iterator):
            self._run_stream(job, result)
            return
        self._finish_scalar(job, result)

    def _run(self) -> None:
        try:
            while True:
                job = self._take_next()
                if job is None:
                    return
                self._execute(job)
        finally:
            if not self._stopped.done():
                self._stopped.set_result(None)

    async def shutdown(self, timeout: float = 5.0) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("timeout must be a non-negative finite number")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout)
        with self._condition:
            self._accepting = False
            queued = tuple(self._queue)
            self._queue.clear()
            for job in queued:
                job.state = _JobState.CANCELLED
                job.handle._request_cancel()
            if self._active is not None and self._active.state is _JobState.ACTIVE:
                self._active.state = _JobState.CANCELLED
                self._active.handle._request_cancel()
            self._condition.notify_all()

        if not self._stopped.done():
            wrapped = asyncio.wrap_future(self._stopped)
            try:
                await asyncio.wait_for(
                    asyncio.shield(wrapped),
                    timeout=max(0.0, deadline - loop.time()),
                )
            except asyncio.TimeoutError as exc:
                raise ShutdownTimeoutError(
                    "active inference did not stop before the shutdown timeout"
                ) from exc
        while self._worker_thread.is_alive():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ShutdownTimeoutError(
                    "active inference did not stop before the shutdown timeout"
                )
            await asyncio.sleep(min(0.001, remaining))
        self._worker_thread.join(timeout=0)


__all__ = [
    "ActorClosedError",
    "ActorSnapshot",
    "AdmissionError",
    "BoundedInferenceActor",
    "InferenceActorError",
    "InferenceHandle",
    "ResultModeError",
    "ShutdownTimeoutError",
]
