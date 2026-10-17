"""Fake-clock tests prove two boundary conditions of the recurring scheduler."""
from dataclasses import replace

import pytest
from wb_serp import cli
from wb_serp.config import Settings


@pytest.mark.parametrize("run_at_startup,expected_slots", [
    (True, ["1970-01-01T00:00:00+00:00", "1970-01-01T00:01:00+00:00"]),
    (False, ["1970-01-01T00:01:00+00:00"]),
])
def test_worker_runs_at_hour_boundary_and_handles_sigterm(monkeypatch, tmp_path, run_at_startup, expected_slots):
    config = replace(Settings.from_env(), collection_period_seconds=60,
                     run_on_startup=run_at_startup, database_path=tmp_path / "worker.sqlite")
    t = [0.0]
    slots = []
    handlers = {}
    def fake_signal(signum, handler):
        handlers[signum] = handler
    def fake_sleep(seconds):
        t[0] += seconds
        if t[0] >= 60:
            # Important: signal should stop the loop at the next iteration.
            # Triggering after the 2nd cycle allows the boundary collection.
            if slots and slots[-1] == "1970-01-01T00:01:00+00:00":
                handlers[cli.signal.SIGTERM]()
    def fake_cycle(cfg, store, *, scheduled_for, should_stop=None, runtime=None):
        slots.append(scheduled_for)
        return (1, 0, 0)
    monkeypatch.setattr(cli.signal, "signal", fake_signal)
    monkeypatch.setattr(cli.time, "time", lambda: t[0])
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli, "run_cycle", fake_cycle)
    cli.run_worker(config, object())
    assert slots == expected_slots
