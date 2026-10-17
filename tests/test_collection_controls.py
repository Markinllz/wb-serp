"""Persistent controls: pause boundaries, resume, frozen depth and archive safety."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from wb_serp import cli
from wb_serp.config import Settings
from wb_serp.models import SearchPage
from wb_serp.store import Store


def setup(tmp_path):
    cfg = replace(Settings.from_env(), database_path=tmp_path/'controls.db', top_n=2,
                  page_limit=1, query_delay_seconds=0)
    store = Store(cfg.database_path)
    store.initialize()
    store.sync_queries(['первая', 'вторая'])
    return cfg, store


def test_stop_finishes_current_query_resume_skips_completed(tmp_path, monkeypatch):
    cfg, store = setup(tmp_path)
    calls = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def page(self, query, number):
            calls.append(query)
            if len(calls) == 1:
                store.set_collection_options(enabled=False)
            return SearchPage([{'id': 1}, {'id': 2}], page_number=number, total=2)
    monkeypatch.setattr(cli, 'build_client', lambda cfg: Client())
    slot = datetime.now(timezone.utc).isoformat()
    assert cli.run_cycle(cfg, store, scheduled_for=slot) == (1, 0, 0)
    assert cli.run_cycle(cfg, store, scheduled_for=slot) == (0, 0, 0)
    store.set_collection_options(enabled=True)
    assert store.collection_options()['revision'] == 1
    assert cli.run_cycle(cfg, store, scheduled_for=slot) == (1, 1, 0)
    assert calls == ['первая', 'вторая']


def test_depth_changes_are_frozen_per_cycle_and_support_300(tmp_path, monkeypatch):
    cfg, store = setup(tmp_path)
    store.set_collection_options(top_n=300)
    calls = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def page(self, query, page):
            calls.append((query, page))
            store.set_collection_options(top_n=50)
            return SearchPage([{'id': i} for i in range((page-1)*100+1, page*100+1)], page_number=page, total=1000)
    monkeypatch.setattr(cli, 'build_client', lambda cfg: Client())
    assert cli.run_cycle(cfg, store) == (2, 0, 0)
    assert len(calls) == 6
    assert all(r['requested_top_n'] == 300 and r['observed_count'] == 300
               for q in store.enabled_queries() for r in store.recent_runs(q['id']))
    assert store.collection_options()['top_n'] == 50


def test_archived_query_keeps_history_and_is_not_reactivated_by_bootstrap(tmp_path):
    cfg, store = setup(tmp_path)
    qid = store.enabled_queries()[0]['id']
    at = datetime.now(timezone.utc)
    store.start_run(run_id='old', query_id=qid, scheduled_for=at.isoformat(), started_at=at,
                    requested_top_n=1, destination=cfg.destination)
    from wb_serp.models import Product
    store.complete_run(run_id='old', products=[Product(7, 1, None, None, None, None, None, 'Товар')],
                       status='success', finished_at=at)
    store.archive_query(qid)
    store.sync_queries(['первая', 'вторая'], preserve_choices=True)
    assert len(store.queries_dashboard(include_paused=True)) == 1
    assert store.product_count(qid) == 1
    assert store.rank_history(qid, [7])[0]['position'] == 1
    store.archive_query(qid, False)
    assert not store.queries_dashboard(include_paused=True)[0]['enabled']
    assert store.add_query('первая') == (qid, False)


def test_v4_migration_preserves_history_and_default_controls(tmp_path):
    cfg, store = setup(tmp_path)
    with store.connect() as db:
        before = [tuple(r) for r in db.execute('SELECT id,phrase,enabled,enabled_since FROM search_queries')]
        db.execute('ALTER TABLE search_queries DROP COLUMN archived')
        db.execute('PRAGMA user_version=4')
    store.initialize()
    store.initialize()
    with store.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT id,phrase,enabled,enabled_since FROM search_queries')] == before
        assert db.execute('PRAGMA user_version').fetchone()[0] == 6
    assert store.collection_options() == {'enabled': True, 'top_n': 200, 'revision': 0}
    for bad in (0, 1001, True, 3.5):
        with pytest.raises(ValueError):
            store.set_collection_options(top_n=bad)
    with pytest.raises(ValueError):
        Store(store.path, readonly=True).set_collection_options(enabled=False)
