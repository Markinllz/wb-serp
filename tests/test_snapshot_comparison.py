"""Compare real persisted ranks without inventing numeric ranks for absent items."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wb_serp.models import Product
from wb_serp.store import Store


def snapshot(store, qid, run_id, ids, hour, *, status='success', depth=3):
    at = datetime(2026, 10, 16, tzinfo=timezone.utc) + timedelta(hours=hour)
    store.start_run(run_id=run_id, query_id=qid, scheduled_for=at.isoformat(),
                    started_at=at, requested_top_n=depth, destination=23)
    store.complete_run(run_id=run_id, status=status, finished_at=at, products=[
        Product(nm, i, Decimal('123.45') if nm != 10 else None,
                4.5, 12, None, 'Продавец', f'Товар {nm}') for i, nm in enumerate(ids, 1)])


@pytest.fixture
def dataset(tmp_path):
    store = Store(tmp_path / 'comparison.sqlite')
    store.initialize()
    store.sync_queries(['запрос', 'другой запрос'])
    qid = store.enabled_queries()[0]['id']
    snapshot(store, qid, 'before', [10, 20, 30], 1)
    snapshot(store, qid, 'partial', [999], 2, status='partial')
    snapshot(store, qid, 'after', [20, 40, 30], 3)
    snapshot(store, qid, 'other-depth', [50], 4, depth=1)
    snapshot(store, qid, 'failed', [888], 5, status='failed')
    return store, qid


def test_comparison_excludes_partial_failed_and_other_depth(dataset):
    store, qid = dataset
    result = store.compare_latest_snapshots(qid, depth=3)
    assert (result['before']['id'], result['after']['id']) == ('before', 'after')
    by_id = {r['nm_id']: r for r in result['products']}
    assert set(by_id) == {10, 20, 30, 40}
    assert by_id[20]['delta'] == 1 and by_id[20]['change'] == 'up'
    assert by_id[30]['delta'] == 0 and by_id[30]['change'] == 'unchanged'
    assert by_id[10]['after'] is None and by_id[10]['delta'] is None and by_id[10]['change'] == 'left'
    assert by_id[40]['before'] is None and by_id[40]['delta'] is None and by_id[40]['change'] == 'entered'
    assert store.compare_latest_snapshots(qid, depth=1) is None
    assert store.compare_latest_snapshots(store.enabled_queries()[1]['id'], depth=3) is None
    assert store.compare_latest_snapshots(qid, depth=3, since='2026-10-16T02:00:00+00:00') is None
    assert store.compare_latest_snapshots(qid, depth=3, until='2026-10-16T02:00:00+00:00') is None


def test_comparison_rejects_mixed_destinations_in_legacy_data(dataset):
    store, qid = dataset
    with store.connect() as db:
        db.execute("UPDATE collection_runs SET destination=999 WHERE id='before'")
    with pytest.raises(ValueError, match='destinations'):
        store.compare_latest_snapshots(qid, depth=3)


def test_price_history_preserves_missing_product_and_missing_price(dataset):
    store, qid = dataset
    history = store.rank_history(qid, [10, 20], depth=3, include_partial=True)
    one = [r for r in history if r['nm_id'] == 10]
    two = [r for r in history if r['nm_id'] == 20]
    assert [r['price_kopecks'] for r in one] == [None, None, None]
    assert [r['position'] for r in one] == [1, None, None]
    assert [r['price_kopecks'] for r in two] == [12345, None, 12345]
    assert [r['run_status'] for r in two] == ['success', 'partial', 'success']
    assert two[1]['rating'] is None and two[1]['reviews'] is None
    assert two[0]['title'] == 'Товар 20'
    assert two[0]['reviews'] == 12 and two[0]['delivery_days'] is None
