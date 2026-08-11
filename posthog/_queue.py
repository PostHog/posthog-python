"""An SDK-owned FIFO queue with CPython ``queue.Queue`` semantics.

The consumer and flush paths synchronize on attributes CPython's pure-Python
``queue.Queue`` exposes but the ``queue`` module does not guarantee: ``mutex``,
``not_empty``, ``not_full``, ``all_tasks_done``, ``unfinished_tasks``,
``_qsize()``, and ``_get()``. Cooperative runtimes swap ``queue.Queue`` out for
their own implementation without those attributes — gevent's
``monkey.patch_all()`` installs ``gevent.queue.Queue``, on which the consumer
thread dies with ``AttributeError: 'Queue' object has no attribute
'not_empty'`` and ``flush()`` raises on ``all_tasks_done``, so a gevent
gunicorn worker buffers every event forever and delivers none (#865).

``LaneQueue`` is that pure-Python implementation carried by the SDK itself,
derived from CPython's ``Lib/queue.py`` (distributed under the PSF-2.0
license — see ``LICENSE-PSF-2.0.txt``), including Python 3.13's
``shutdown()``/``ShutDown`` API. It builds only on ``threading`` primitives,
which gevent patches compatibly, so it behaves identically on stock CPython
and under monkey-patching — and its private surface can't be swapped out from
under the SDK.

Deliberate non-goal: ``LaneQueue`` does not inherit from ``queue.Queue``, so
``isinstance(client.queue, queue.Queue)`` is ``False``. Inheriting would
re-import the bug — under monkey-patching the base would resolve to gevent's
incompatible class — and ``queue.Queue`` is not an ABC, so virtual
registration is unavailable.
"""

import threading
from collections import deque
from queue import Empty, Full
from time import monotonic

try:
    from queue import ShutDown
except ImportError:
    # Python < 3.13 has no queue.ShutDown; carry an equivalent.
    class ShutDown(Exception):  # type: ignore[no-redef]
        """Raised when put/get is called on a shut-down LaneQueue."""


class LaneQueue:
    """A FIFO queue with the full CPython ``queue.Queue`` interface.

    ``maxsize`` bounds the queue; a ``maxsize`` of zero or less means the
    queue is unbounded.
    """

    def __init__(self, maxsize: int = 0):
        self.maxsize = maxsize
        self.queue: deque = deque()  # named `queue` to match CPython's attribute
        self.mutex = threading.Lock()
        self.not_empty = threading.Condition(self.mutex)
        self.not_full = threading.Condition(self.mutex)
        self.all_tasks_done = threading.Condition(self.mutex)
        self.unfinished_tasks = 0
        self.is_shutdown = False

    def task_done(self) -> None:
        with self.all_tasks_done:
            unfinished = self.unfinished_tasks - 1
            if unfinished <= 0:
                if unfinished < 0:
                    raise ValueError("task_done() called too many times")
                self.all_tasks_done.notify_all()
            self.unfinished_tasks = unfinished

    def join(self) -> None:
        with self.all_tasks_done:
            while self.unfinished_tasks:
                self.all_tasks_done.wait()

    def qsize(self) -> int:
        with self.mutex:
            return self._qsize()

    def empty(self) -> bool:
        with self.mutex:
            return not self._qsize()

    def full(self) -> bool:
        with self.mutex:
            return 0 < self.maxsize <= self._qsize()

    def put(self, item, block: bool = True, timeout=None) -> None:
        with self.not_full:
            if self.is_shutdown:
                raise ShutDown
            if self.maxsize > 0:
                if not block:
                    if self._qsize() >= self.maxsize:
                        raise Full
                elif timeout is None:
                    while self._qsize() >= self.maxsize:
                        self.not_full.wait()
                        if self.is_shutdown:
                            raise ShutDown
                elif timeout < 0:
                    raise ValueError("'timeout' must be a non-negative number")
                else:
                    endtime = monotonic() + timeout
                    while self._qsize() >= self.maxsize:
                        remaining = endtime - monotonic()
                        if remaining <= 0.0:
                            raise Full
                        self.not_full.wait(remaining)
                        if self.is_shutdown:
                            raise ShutDown
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def get(self, block: bool = True, timeout=None):
        with self.not_empty:
            if self.is_shutdown and not self._qsize():
                raise ShutDown
            if not block:
                if not self._qsize():
                    raise Empty
            elif timeout is None:
                while not self._qsize():
                    self.not_empty.wait()
                    if self.is_shutdown and not self._qsize():
                        raise ShutDown
            elif timeout < 0:
                raise ValueError("'timeout' must be a non-negative number")
            else:
                endtime = monotonic() + timeout
                while not self._qsize():
                    remaining = endtime - monotonic()
                    if remaining <= 0.0:
                        raise Empty
                    self.not_empty.wait(remaining)
                    if self.is_shutdown and not self._qsize():
                        raise ShutDown
            item = self._get()
            self.not_full.notify()
            return item

    def put_nowait(self, item) -> None:
        self.put(item, block=False)

    def get_nowait(self):
        return self.get(block=False)

    def shutdown(self, immediate: bool = False) -> None:
        """Shut down the queue: further put() raises ShutDown, get() drains.

        With ``immediate=True``, pending items are discarded and blocked
        ``get()``/``join()`` callers are released now.
        """
        with self.mutex:
            self.is_shutdown = True
            if immediate:
                while self._qsize():
                    self._get()
                    if self.unfinished_tasks > 0:
                        self.unfinished_tasks -= 1
                self.all_tasks_done.notify_all()
            self.not_empty.notify_all()
            self.not_full.notify_all()

    def _qsize(self) -> int:
        return len(self.queue)

    def _put(self, item) -> None:
        self.queue.append(item)

    def _get(self):
        return self.queue.popleft()
