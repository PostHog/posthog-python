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

``SdkQueue`` is that pure-Python implementation carried by the SDK itself,
adapted from CPython's ``Lib/queue.py`` (PSF license). It builds only on
``threading`` primitives, which gevent patches compatibly, so it behaves
identically on stock CPython and under monkey-patching — and its private
surface can't be swapped out from under the SDK.
"""

import threading
from collections import deque
from queue import Empty, Full
from time import monotonic


class SdkQueue:
    """A FIFO queue with the full CPython ``queue.Queue`` interface.

    ``maxsize`` bounds the queue; a ``maxsize`` of zero or less means the
    queue is unbounded.
    """

    def __init__(self, maxsize: int = 0):
        self.maxsize = maxsize
        self._queue: deque = deque()
        self.mutex = threading.Lock()
        self.not_empty = threading.Condition(self.mutex)
        self.not_full = threading.Condition(self.mutex)
        self.all_tasks_done = threading.Condition(self.mutex)
        self.unfinished_tasks = 0

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
            if self.maxsize > 0:
                if not block:
                    if self._qsize() >= self.maxsize:
                        raise Full
                elif timeout is None:
                    while self._qsize() >= self.maxsize:
                        self.not_full.wait()
                elif timeout < 0:
                    raise ValueError("'timeout' must be a non-negative number")
                else:
                    endtime = monotonic() + timeout
                    while self._qsize() >= self.maxsize:
                        remaining = endtime - monotonic()
                        if remaining <= 0.0:
                            raise Full
                        self.not_full.wait(remaining)
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def get(self, block: bool = True, timeout=None):
        with self.not_empty:
            if not block:
                if not self._qsize():
                    raise Empty
            elif timeout is None:
                while not self._qsize():
                    self.not_empty.wait()
            elif timeout < 0:
                raise ValueError("'timeout' must be a non-negative number")
            else:
                endtime = monotonic() + timeout
                while not self._qsize():
                    remaining = endtime - monotonic()
                    if remaining <= 0.0:
                        raise Empty
                    self.not_empty.wait(remaining)
            item = self._get()
            self.not_full.notify()
            return item

    def put_nowait(self, item) -> None:
        self.put(item, block=False)

    def get_nowait(self):
        return self.get(block=False)

    def _qsize(self) -> int:
        return len(self._queue)

    def _put(self, item) -> None:
        self._queue.append(item)

    def _get(self):
        return self._queue.popleft()
