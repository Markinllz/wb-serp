"""Inter-process collector mutex on the shared local Docker volume (POSIX/Linux)."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import logging
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


@contextmanager
def exclusive_collector(path: Path) -> Iterator[None]:
    """Serialize prepare + cycles across `docker compose exec` and worker.

    Lock is released by the OS on process death; not held by web readers.
    Only valid on the shared local filesystem (not network-mounted SQLite).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".collector.lock"
    with lock_path.open("a+b") as lock_file:
        if not _try_lock(lock_file):
            log.info("Another collector is active; waiting for the shared lock")
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _try_lock(file: object) -> bool:
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
