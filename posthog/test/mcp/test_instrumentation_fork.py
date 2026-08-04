import asyncio
import os
import signal
import threading
import warnings

import pytest

import posthog.mcp._instrumentation as instrumentation


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires os.fork and os.register_at_fork",
)
def test_sync_capture_completes_after_fork():
    parent_capture_started = threading.Event()
    finish_parent_capture = threading.Event()

    async def pending_parent_capture():
        parent_capture_started.set()
        while not finish_parent_capture.is_set():
            await asyncio.sleep(0.01)

    owner = object()
    instrumentation.fire_and_forget(pending_parent_capture(), owner)
    assert parent_capture_started.wait(timeout=2)
    parent_loop = instrumentation._bg_loop
    assert parent_loop is not None
    assert instrumentation._BACKGROUND_TASKS

    read_fd, write_fd = os.pipe()
    instrumentation._bg_loop_lock.acquire()
    instrumentation._tasks_lock.acquire()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            signal.alarm(5)
            try:
                inherited_work_cleared = not instrumentation._BACKGROUND_TASKS
                child_capture_completed = []

                async def child_capture():
                    child_capture_completed.append(True)

                instrumentation.fire_and_forget(child_capture(), owner)
                instrumentation.drain_pending_sync(owner, timeout=2)
                new_loop_created = instrumentation._bg_loop is not parent_loop

                if (
                    inherited_work_cleared
                    and child_capture_completed == [True]
                    and new_loop_created
                ):
                    result = "ok"
                else:
                    result = (
                        f"inherited_work_cleared={inherited_work_cleared}, "
                        f"child_capture_completed={child_capture_completed}, "
                        f"new_loop_created={new_loop_created}"
                    )
            except BaseException as error:
                result = f"exception: {error!r}"
            finally:
                signal.alarm(0)
                os.write(write_fd, result.encode())
                os.close(write_fd)
                os._exit(0)

        os.close(write_fd)
        result = os.read(read_fd, 4096).decode()
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
    finally:
        instrumentation._tasks_lock.release()
        instrumentation._bg_loop_lock.release()
        finish_parent_capture.set()
        instrumentation.drain_pending_sync(owner, timeout=2)

    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, result
    assert result == "ok"
