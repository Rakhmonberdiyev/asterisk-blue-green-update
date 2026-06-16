"""
Worker pool for Asterisk ARI sessions.

- N workers (default 5), each holding up to M sessions (default 2).
- "First available" assignment: fill W1 to capacity, then W2, etc.
- If all workers are full, incoming sessions are queued and dispatched
  as slots free up (FIFO).
- Each worker runs as an asyncio task in the same event loop.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional
from handover import r

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, worker_id: int, max_sessions: int = 2):
        self.id = worker_id
        self.max_sessions = max_sessions
        # session_key -> asyncio.Task running that session's media handler
        self.sessions: dict[str, asyncio.Task] = {}

    @property
    def load(self) -> int:
        return len(self.sessions)

    @property
    def has_slot(self) -> bool:
        return self.load < self.max_sessions

    def assign(self, session_key: str, coro_factory: Callable[[], Awaitable[None]],
               on_done: Callable[[int, str], None]) -> asyncio.Task:
        """
        Start a session on this worker. `coro_factory` is called now to
        produce the coroutine; `on_done` is invoked when it finishes so
        the pool can free the slot and pull the next queued session.
        """
        if not self.has_slot:
            raise RuntimeError(f"Worker {self.id} is full")

        task = asyncio.create_task(coro_factory(), name=f"worker-{self.id}-session-{session_key}")
        self.sessions[session_key] = task

        def _cleanup(_t: asyncio.Task):
            self.sessions.pop(session_key, None)
            try:
                on_done(self.id, session_key)
            except Exception as e:
                logger.exception(f"on_done callback failed for worker {self.id}: {e}")

        task.add_done_callback(_cleanup)
        logger.info(
            f"Worker {self.id}: assigned session {session_key} "
            f"(load {self.load}/{self.max_sessions})"
        )
        return task


class WorkerPool:
    def __init__(self, num_workers: int = 8, max_sessions_per_worker: int = 2):
        self.workers: list[Worker] = [
            Worker(i + 1, max_sessions_per_worker) for i in range(num_workers)
        ]
        # Queued sessions waiting for a free slot.
        # Each item: (session_key, coro_factory)
        self._queue: asyncio.Queue[tuple[str, Callable[[], Awaitable[None]]]] = asyncio.Queue()
        # Manual session_key -> worker_id index, used for explicit removal/lookups.
        self._index: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def total_capacity(self) -> int:
        return sum(w.max_sessions for w in self.workers)

    @property
    def total_load(self) -> int:
        return sum(w.load for w in self.workers)
    
    def _update_redis_stats(self):
        """Push current pool load and queue size to Redis."""
        try:
            r.set("active_sessions", f"{self.total_load}/{self.total_capacity}")
        except Exception as e:
            logger.error(f"Failed to update Redis active_sessions: {e}")
        try:
            r.set("queued_sessions", str(self._queue.qsize()))
        except Exception as e:
            logger.error(f"Failed to update Redis queued_sessions: {e}")


    def _first_available(self) -> Optional[Worker]:
        """First-available strategy: lowest-index worker with a free slot."""
        for w in self.workers:
            if w.has_slot:
                return w
        return None

    def _on_session_done(self, worker_id: int, session_key: str):
        """Called when a session task finishes. Frees the slot and dispatches queued work."""

        self._index.pop(session_key, None)
        self._update_redis_stats()


        logger.info(
            f"Worker {worker_id}: session {session_key} finished "
            f"(pool load {self.total_load}/{self.total_capacity}, "
            f"queue size {self._queue.qsize()})"
        )
        # If there's queued work, dispatch the next one onto whatever slot just freed up.
        if not self._queue.empty():
            try:
                next_key, next_factory = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            # Re-dispatch on the same event loop tick.
            asyncio.create_task(self._dispatch(next_key, next_factory))

    async def _dispatch(self, session_key: str, coro_factory: Callable[[], Awaitable[None]]):
        """Place a session on the first available worker, or queue it."""
        async with self._lock:
            worker = self._first_available()
            if worker is not None:
                self._index[session_key] = worker.id
                worker.assign(session_key, coro_factory, self._on_session_done)
                self._update_redis_stats()
                return

            # All full — queue it.
            await self._queue.put((session_key, coro_factory))
            logger.info(
                f"Pool full ({self.total_load}/{self.total_capacity}). "
                f"Session {session_key} queued (queue size {self._queue.qsize()})."
            )
            self._update_redis_stats()

    async def submit(self, session_key: str, coro_factory: Callable[[], Awaitable[None]]):
        """
        Submit a session to the pool. `coro_factory` is a zero-arg callable
        that returns the coroutine to run for that session (so we can defer
        coroutine creation until a slot is free).
        """
        await self._dispatch(session_key, coro_factory)

    def status(self) -> str:
        parts = [f"W{w.id}:{w.load}/{w.max_sessions}" for w in self.workers]
        return f"[{' '.join(parts)}] queued={self._queue.qsize()}"