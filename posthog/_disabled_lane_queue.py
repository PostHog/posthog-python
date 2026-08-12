import threading
from queue import Empty, Full


class _DisabledLaneQueue:
    """Minimal empty queue used for lifecycle cleanup on an unavailable lane."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self.mutex = threading.Lock()
        self.not_empty = threading.Condition(self.mutex)
        self.unfinished_tasks = 0

    def put(self, item, block: bool = True, timeout=None) -> None:
        raise Full

    def get_nowait(self):
        raise Empty

    def qsize(self) -> int:
        return 0

    def empty(self) -> bool:
        return True

    def task_done(self) -> None:
        return None
