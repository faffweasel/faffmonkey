"""Locked read-modify-write for queue.json.

The runtime reads this same file under an fcntl lock during bootstrap
(runtime/bootstrap.py:_locked_queue). These scripts did not, so a
bootstrap running while the agent added an item could lose whichever
write finished second.
"""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked_queue(queue_path: Path):
    """Yield the queue list, then a writer callback, under an exclusive lock.

    Yields (queue, write) where write(new_queue) persists atomically.
    The queue is [] when the file is missing or unreadable.
    """
    lock_path = queue_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        queue: list[dict] = []
        if queue_path.exists():
            try:
                loaded = json.loads(queue_path.read_text())
                if isinstance(loaded, list):
                    queue = [i for i in loaded if isinstance(i, dict)]
            except (json.JSONDecodeError, OSError):
                queue = []

        def write(new_queue: list[dict]) -> None:
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = queue_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(new_queue, indent=2) + "\n")
            os.replace(tmp, queue_path)

        yield queue, write
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
