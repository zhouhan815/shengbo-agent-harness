import asyncio
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from shengbo_prelive.execution import InstanceLock, ExecutionConflict, GraphExecutionRegistry


def _second_instance(root, queue):
    try:
        with InstanceLock(root):
            queue.put("acquired")
    except ExecutionConflict:
        queue.put("blocked")


class InstanceTests(unittest.TestCase):
    def test_second_process_blocked_and_release_allows_new_owner(self):
        with tempfile.TemporaryDirectory() as root:
            first = InstanceLock(root).acquire()
            with self.assertRaises(ExecutionConflict):
                InstanceLock(root).acquire()
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            second = context.Process(target=_second_instance, args=(root, queue))
            second.start()
            second.join(15)
            self.assertFalse(second.is_alive())
            self.assertEqual(queue.get(timeout=1), "blocked")
            first.release()
            with InstanceLock(root):
                pass


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_thread_serial_different_threads_parallel(self):
        registry = GraphExecutionRegistry()
        started, release = [], asyncio.Event()
        async def sdk(label):
            started.append(label)
            await release.wait()
            return label
        async def invoke(thread, label):
            async with registry.execution(thread, label, "run1") as handle:
                return await handle.invoke(sdk(label))
        first = asyncio.create_task(invoke("main", "first"))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(invoke("main", "second"))
        third = asyncio.create_task(invoke("child", "third"))
        await asyncio.sleep(0.01)
        self.assertEqual(started, ["first", "third"])
        with self.assertRaises(ExecutionConflict):
            registry.assert_no_live_owner("main")
        release.set()
        self.assertEqual(await asyncio.gather(first, second, third), ["first", "second", "third"])
        self.assertTrue(registry.assert_no_live_owner())
    async def test_cancelled_owner_retains_lock_until_sdk_really_exits(self):
        registry = GraphExecutionRegistry()
        sdk_started, cleanup_started, cleanup_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        started = []
        async def slow_sdk():
            sdk_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_done.wait()
                return "cleaned"
        async def first():
            async with registry.execution("main", "one", "run1") as handle:
                await handle.invoke(slow_sdk())
        async def second():
            async with registry.execution("main", "two", "run2") as handle:
                started.append("two")
                await handle.invoke(asyncio.sleep(0))
        owner = asyncio.create_task(first())
        await sdk_started.wait()
        owner.cancel()
        await cleanup_started.wait()
        waiter = asyncio.create_task(second())
        await asyncio.sleep(0.01)
        self.assertEqual(started, [])
        self.assertFalse(owner.done())
        cleanup_done.set()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        await waiter
        self.assertTrue(registry.assert_no_live_owner())
    async def test_cancel_join_warning_keeps_pending_and_coordinator_alive(self):
        registry = GraphExecutionRegistry()
        started, cleanup, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def sdk():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cleanup.set()
                await finish.wait()
        async def owner():
            async with registry.execution("main", "one", "run1") as handle:
                await handle.invoke(sdk())
        running = asyncio.create_task(owner())
        await started.wait()
        result = await registry.cancel_and_join_run("run1", warn_after_seconds=0.01)
        self.assertFalse(result["all_exited"])
        self.assertEqual(result["pending_invocations"], ["one"])
        self.assertFalse(asyncio.current_task().cancelling())
        with self.assertRaises(ExecutionConflict):
            registry.assert_no_live_owner("main")
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await running
        final = await registry.cancel_and_join_run("run1", warn_after_seconds=0.01)
        self.assertTrue(final["all_exited"])
    async def test_run_closed_rejects_waiting_and_shutdown_drains(self):
        registry = GraphExecutionRegistry()
        started = asyncio.Event()
        async def sdk():
            started.set()
            await asyncio.sleep(30)
        async def owner():
            async with registry.execution("main", "one", "run1") as handle:
                await handle.invoke(sdk())
        running = asyncio.create_task(owner())
        await started.wait()
        result = await registry.drain_all_until_exited()
        self.assertTrue(result["all_exited"])
        with self.assertRaises(asyncio.CancelledError):
            await running
        with self.assertRaises(ExecutionConflict):
            async with registry.execution("other", "two", "run2"):
                pass
        self.assertTrue(registry.assert_no_live_owner())


if __name__ == "__main__":
    unittest.main()
