from multiprocessing import get_context
import time

from wb_serp.locking import exclusive_collector


def attempt_lock(path, ready, output):
    ready.set()
    started = time.monotonic()
    with exclusive_collector(path):
        output.put(time.monotonic() - started)


def test_two_collectors_are_serialized(tmp_path):
    path = tmp_path / "serp.db"
    ctx = get_context("spawn")
    ready = ctx.Event()
    queue = ctx.Queue()
    with exclusive_collector(path):
        process = ctx.Process(target=attempt_lock, args=(path, ready, queue))
        process.start()
        assert ready.wait(timeout=6)
        time.sleep(0.15)
        assert process.is_alive(), "The second collector must wait on the file lock"
    process.join(timeout=6)
    assert process.exitcode == 0
    assert queue.get(timeout=1) >= 0.12
