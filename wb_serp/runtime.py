"""Small, atomic heartbeat shared by the worker and read-only dashboard."""
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

log = logging.getLogger(__name__)


def status_path(database: Path) -> Path:
    return database.with_name(database.name + '.worker.json')


def read_worker_status(database: Path, *, now: datetime | None = None) -> dict:
    try:
        state = json.loads(status_path(database).read_text())
        updated = datetime.fromisoformat(state['updated_at'])
        age = ((now or datetime.now(timezone.utc)) - updated).total_seconds()
        state['alive'] = 0 <= age <= 20 and state['state'] not in ('stopped', 'error')
        return state
    except (OSError, ValueError, TypeError, KeyError):
        return {'state': 'unknown', 'alive': False}


class WorkerHeartbeat:
    def __init__(self, database: Path, *, interval: float = 5):
        self.path = status_path(database)
        self.interval = interval
        self._stop = Event()
        self._lock = Lock()
        self._state = {'state': 'waiting', 'pid': os.getpid()}
        self._thread = Thread(target=self._run, daemon=True, name='worker-heartbeat')

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._publish()
        self._thread.start()
        return self

    def update(self, state: str, **fields) -> None:
        with self._lock:
            self._state.update(state=state, **fields)
        self._publish()

    def _publish(self) -> None:
        # Publish under the same lock so an older ticker cannot replace a newer state.
        with self._lock:
            data = {**self._state, 'updated_at': datetime.now(timezone.utc).isoformat()}
            temporary = self.path.with_name(self.path.name + '.' + uuid4().hex + '.tmp')
            try:
                with temporary.open('x') as file:
                    os.chmod(temporary, 0o600)
                    json.dump(data, file, ensure_ascii=False)
                os.replace(temporary, self.path)
            except OSError:
                log.exception('Cannot publish worker heartbeat')
            finally:
                temporary.unlink(missing_ok=True)

    def _run(self):
        while not self._stop.wait(self.interval):
            self._publish()

    def __exit__(self, exc_type, *_):
        self._stop.set()
        self._thread.join(timeout=2)
        self.update('error' if exc_type else 'stopped')
