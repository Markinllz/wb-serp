"""Short source catalogs remain usable without making claims about missing ranks."""
from datetime import datetime, timedelta, timezone

import pytest

from wb_serp.models import Product
from wb_serp.store import Store


def write(store, qid, run_id, ids, status, hour):
    at = datetime(2026, 10, 16, tzinfo=timezone.utc) + timedelta(hours=hour)
    store.start_run(run_id=run_id, query_id=qid, scheduled_for=at.isoformat(),
                    started_at=at, requested_top_n=3, destination=23)
    store.complete_run(run_id=run_id, status=status, finished_at=at,
        products=[Product(nm, i, None, None, None, None, f"Продавец {run_id}", f"{run_id} {nm}")
                  for i, nm in enumerate(ids, 1)])


def test_partial_ranks_are_known_but_missing_partial_rank_is_unknown(tmp_path):
    store = Store(tmp_path / 'history.sqlite')
    store.initialize()
    store.sync_queries(['запрос'])
    qid = store.enabled_queries()[0]['id']
    write(store, qid, 'full', [10, 20, 30], 'success', 1)
    write(store, qid, 'short', [10, 40], 'partial', 2)
    write(store, qid, 'failed', [99], 'failed', 3)
    assert store.product_count(qid) == 4
    assert [p['nm_id'] for p in store.search_products(qid, 'short 40')] == [40]
    assert store.products_by_ids(qid, [40])[0]['last_title'] == 'short 40'
    assert store.available_depths(qid, include_partial=True) == [3]
    assert store.latest_observations(qid)[0]['nm_id'] == 10
    assert [p['nm_id'] for p in store.latest_observations(qid, include_partial=True)] == [10, 40]
    history = store.rank_history(qid, [20, 40], include_partial=True)
    assert [(r['nm_id'], r['position'], r['run_status']) for r in history] == [
        (20, 2, 'success'), (20, None, 'partial'),
        (40, None, 'success'), (40, 2, 'partial'),
    ]
    assert store.count_history_runs(qid, include_partial=True) == 2
    assert store.count_success_runs(qid) == 1
    assert store.rank_history(qid, [40])[0]['position'] is None


def test_v2_catalog_migration_preserves_observations_and_newer_titles(tmp_path):
    store = Store(tmp_path / 'legacy.sqlite')
    store.initialize()
    store.sync_queries(['запрос'])
    qid = store.enabled_queries()[0]['id']
    write(store, qid, 'old-short', [10, 20], 'partial', 1)
    write(store, qid, 'full', [10, 30, 40], 'success', 2)
    write(store, qid, 'new-short', [20, 50], 'partial', 3)
    write(store, qid, 'failed', [99], 'failed', 4)
    with store.connect() as db:
        db.execute('DELETE FROM product_catalog WHERE nm_id IN (20,50)')
        db.execute('PRAGMA user_version=2')
        before = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')]
                  for t in ('collection_runs', 'observations')}
    store.initialize()
    readonly = Store(store.path, readonly=True)
    assert readonly.product_count(qid) == 5
    catalog = {p['nm_id']: p for p in readonly.known_products(qid)}
    assert catalog[10]['last_title'] == 'full 10'
    assert catalog[20]['last_title'] == 'new-short 20'
    assert catalog[50]['last_title'] == 'new-short 50'
    with readonly.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        after = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')]
                 for t in before}
        assert before == after
        first = db.execute('SELECT first_seen FROM product_catalog WHERE nm_id=20').fetchone()[0]
        assert first.startswith('2026-10-16T01:')
    store.initialize()  # migration is idempotent
    assert store.product_count(qid) == 5


def test_valid_partial_snapshot_also_fixes_dataset_region(tmp_path):
    store = Store(tmp_path / 'region.sqlite')
    store.initialize()
    store.sync_queries(['запрос'])
    qid = store.enabled_queries()[0]['id']
    write(store, qid, 'short', [10], 'partial', 1)
    with pytest.raises(ValueError, match='WB_DESTINATION'):
        store.start_run(run_id='wrong-region', query_id=qid, scheduled_for='slot',
                        started_at=datetime.now(timezone.utc), requested_top_n=3, destination=999)
