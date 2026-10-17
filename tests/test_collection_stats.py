from datetime import datetime, timedelta, timezone
import json
import time

from wb_serp.models import Product
from wb_serp.runtime import WorkerHeartbeat, read_worker_status, status_path
from wb_serp.store import Store


def test_collection_stats_deduplicate_products_retries_and_freeze_cycle_plan(tmp_path):
    store = Store(tmp_path / 'stats.sqlite')
    store.initialize()
    store.sync_queries(['первый', 'второй', 'третий'])
    ids = [q['id'] for q in store.enabled_queries()]
    at = datetime(2026, 10, 17, 3, tzinfo=timezone.utc)
    slot = at.isoformat(timespec='seconds')

    def run(name, qid, status, products, minute):
        start = at + timedelta(minutes=minute)
        store.start_run(run_id=name, query_id=qid, scheduled_for=slot, started_at=start,
                        requested_top_n=2, destination=23)
        if status != 'running':
            store.complete_run(run_id=name, status=status, products=[
                Product(nm, i, None, None, None, None, None, f'Товар {nm}')
                for i, nm in enumerate(products, 1)], finished_at=start)

    run('failed', ids[0], 'failed', [], 0)
    run('retry', ids[0], 'success', [11, 12], 1)
    run('partial', ids[1], 'partial', [12], 2)
    run('active', ids[2], 'running', [], 3)
    store.set_query_enabled(ids[1], False)
    store.add_query('со следующего часа')
    result = Store(store.path, readonly=True).collection_stats(slot=slot, depth=2, query_ids=ids)
    assert result['observations'] == 3
    assert result['products'] == 2  # SKU 12 appeared in two queries.
    assert result['snapshots'] == 2
    assert result['active_queries'] == 3
    assert result['target'] == 3 and result['processed'] == 2
    assert result['counts']['failed'] == 0 and result['counts']['running'] == 1
    assert result['cycle_observations'] == 3 and result['current_query'] == 'третий'
    empty = store.collection_stats(slot='2026-10-17T04:00:00+00:00', depth=2, query_ids=[])
    assert empty['processed'] == 0 and empty['target'] == 0
    assert empty['observations'] == 3 and empty['current_query'] is None


def test_heartbeat_updates_while_worker_is_busy_and_stops_truthfully(tmp_path):
    db = tmp_path / 'runtime.sqlite'
    assert read_worker_status(db)['alive'] is False
    with WorkerHeartbeat(db, interval=0.02) as heartbeat:
        heartbeat.update('collecting', scheduled_for='2026-10-17T03:00:00+00:00', query_ids=[1, 2])
        first = read_worker_status(db)
        assert first['alive'] and first['state'] == 'collecting'
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = read_worker_status(db)
            if current['updated_at'] > first['updated_at']:
                break
            time.sleep(0.01)
        assert current['updated_at'] > first['updated_at']
        assert current['query_ids'] == [1, 2]
        later = datetime.fromisoformat(current['updated_at']) + timedelta(seconds=21)
        assert read_worker_status(db, now=later)['alive'] is False
        heartbeat.update('waiting')
    assert read_worker_status(db)['state'] == 'stopped'
    assert read_worker_status(db)['alive'] is False
    assert not list(tmp_path.glob('*.tmp'))
    status_path(db).write_text('{unfinished')
    assert read_worker_status(db) == {'state': 'unknown', 'alive': False}


def test_heartbeat_records_unhandled_worker_error(tmp_path):
    db = tmp_path / 'error.sqlite'
    try:
        with WorkerHeartbeat(db):
            raise RuntimeError('test failure')
    except RuntimeError:
        pass
    assert read_worker_status(db)['state'] == 'error'
    assert json.loads(status_path(db).read_text())['state'] == 'error'
