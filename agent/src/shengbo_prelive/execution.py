"""OS-instance exclusion and actual SDK-task lifetime ownership.

A business lease expiry never releases one of these locks. A same-thread invoke
can start only after the previous SDK task has really exited, including tasks
which temporarily suppress cancellation during their cleanup.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import os
from pathlib import Path
import threading
from typing import Any


class ExecutionConflict(RuntimeError):
    pass


_INSTANCE_GUARD = threading.Lock()
_INSTANCE_PATHS: set[str] = set()


class InstanceLock:
    def __init__(self, data_root: str | Path):
        self.root = Path(data_root).resolve()
        self.path = self.root / "instance.lock"
        self._key = os.path.normcase(str(self.path))
        self._handle = None

    def acquire(self):
        if self._handle is not None:
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        with _INSTANCE_GUARD:
            if self._key in _INSTANCE_PATHS:
                raise ExecutionConflict("data_root_already_owned")
            handle = self.path.open("a+b")
            try:
                if handle.seek(0, 2) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise ExecutionConflict("data_root_already_owned") from None
            self._handle = handle
            _INSTANCE_PATHS.add(self._key)
        return self

    def release(self):
        with _INSTANCE_GUARD:
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                _INSTANCE_PATHS.discard(self._key)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


async def _wait_until_done(task):
    """Repeated caller cancellation must not release a still-running task lock."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    # Retrieve failures so never-observed cleanup failures don't leak raw payloads
    # in event-loop warnings. Invocation callers still receive their own result.
    if task.done() and not task.cancelled():
        task.exception()


class ExecutionHandle:
    def __init__(self, registry, thread_id, invocation_id, run_id):
        self.registry, self.thread_id = registry, thread_id
        self.invocation_id, self.run_id = invocation_id, run_id
        self.owner = asyncio.current_task()
        self.sdk_task = None
        self.cancel_requested = False
        self.closed = False

    async def invoke(self, awaitable):
        if self.closed or self.sdk_task is not None or self.cancel_requested:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            if self.cancel_requested:
                raise asyncio.CancelledError()
            raise ExecutionConflict("invocation_handle_not_available")
        started = False
        async def run():
            nonlocal started
            started = True
            return await awaitable
        self.sdk_task = asyncio.create_task(run(), name="agent-sdk-" + self.invocation_id)
        def close_if_never_started(task):
            if not started and inspect.iscoroutine(awaitable):
                awaitable.close()
        self.sdk_task.add_done_callback(close_if_never_started)
        try:
            result = await asyncio.shield(self.sdk_task)
            if self.cancel_requested:
                # A provider may suppress cancellation while draining, but its
                # late return cannot resurrect a revoked invocation.
                raise asyncio.CancelledError()
            return result
        except asyncio.CancelledError:
            self.cancel_requested = True
            self.sdk_task.cancel()
            await _wait_until_done(self.sdk_task)
            raise


class GraphExecutionRegistry:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._entries: dict[str, ExecutionHandle] = {}
        self._closing_runs: set[str] = set()
        self._draining = False

    @asynccontextmanager
    async def execution(self, thread_id: str, invocation_id: str, run_id: str):
        if self._draining or run_id in self._closing_runs:
            raise ExecutionConflict("run_execution_closed")
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        await lock.acquire()
        handle = None
        try:
            if self._draining or run_id in self._closing_runs:
                raise ExecutionConflict("run_execution_closed")
            handle = ExecutionHandle(self, thread_id, invocation_id, run_id)
            self._entries[thread_id] = handle
            yield handle
        finally:
            if handle is not None:
                handle.closed = True
                task = handle.sdk_task
                if task is not None and not task.done():
                    handle.cancel_requested = True
                    task.cancel()
                    await _wait_until_done(task)
                if self._entries.get(thread_id) is handle:
                    self._entries.pop(thread_id, None)
            lock.release()

    def assert_no_live_owner(self, thread_id: str | None = None):
        values = [self._entries[thread_id]] if thread_id in self._entries else ([] if thread_id else list(self._entries.values()))
        if values:
            raise ExecutionConflict("sdk_thread_still_owned")
        return True

    async def cancel_and_join_run(self, run_id: str, warn_after_seconds: float = 5):
        self._closing_runs.add(run_id)
        current = asyncio.current_task()
        handles = [handle for handle in self._entries.values() if handle.run_id == run_id]
        tasks = []
        for handle in handles:
            handle.cancel_requested = True
            task = handle.sdk_task
            if task is not None and not task.done() and task is not current:
                task.cancel()
                tasks.append(task)
        if tasks:
            # A warning threshold is not a cancellation-completed assertion.
            await asyncio.wait(tasks, timeout=max(0, warn_after_seconds))
        pending = [h.invocation_id for h in handles if h.sdk_task is None or not h.sdk_task.done()]
        return {"all_exited": not pending, "pending_invocations": pending,
                "cancelled_invocations": [h.invocation_id for h in handles],
                "thread_ownership_released": all(h.thread_id not in self._entries for h in handles)}

    async def drain_all_until_exited(self):
        self._draining = True
        handles = list(self._entries.values())
        for handle in handles:
            handle.cancel_requested = True
            if handle.sdk_task is not None and not handle.sdk_task.done() and handle.sdk_task is not asyncio.current_task():
                handle.sdk_task.cancel()
        for handle in handles:
            if handle.sdk_task is not None and handle.sdk_task is not asyncio.current_task():
                await _wait_until_done(handle.sdk_task)
        # SDK work exited. Owners finish their context-manager cleanup on the
        # next loop turn; do not cancel the coordinator which invoked this drain.
        await asyncio.sleep(0)
        pending = [h.invocation_id for h in self._entries.values() if h.sdk_task is None or not h.sdk_task.done()]
        return {"all_exited": not pending, "pending_invocations": pending}

    def status(self):
        return {"draining": self._draining, "owners": [
            {"thread_id": h.thread_id, "invocation_id": h.invocation_id, "run_id": h.run_id,
             "sdk_running": h.sdk_task is not None and not h.sdk_task.done(),
             "cancel_requested": h.cancel_requested} for h in self._entries.values()]}
