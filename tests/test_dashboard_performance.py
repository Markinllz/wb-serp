"""Live counters must use run summaries, even when the history becomes large."""
from datetime import datetime, timezone

from wb_serp.models import Product
from wb_serp.store import Store
from wb_serp import cli
from wb_serp.client import FetchError
from dataclasses import replace
from wb_serp.config import Settings


def test_counters_match_valid_rows_without_reading_observation_history(tmp_path, monkeypatch):
    store = Store(tmp_path/'stats.db')
    store.initialize()
    store.sync_queries(['one','two'])
    now = datetime.now(timezone.utc)
    for run, status, ids in [('good','success',[1,2]),('short','partial',[2]),('bad','failed',[3])]:
        store.start_run(run_id=run,query_id=1,scheduled_for=now.isoformat(),started_at=now,
                        requested_top_n=2,destination=99)
        store.complete_run(run_id=run,products=[Product(n,i,None,None,None,None,None,'title')
                            for i,n in enumerate(ids,1)],status=status,finished_at=now)
    # Reject even a COUNT or a JOIN against observations, independently of timing/hardware.
    original=store.connect
    from contextlib import contextmanager
    import sqlite3
    @contextmanager
    def guarded():
        with original() as db:
            db.set_authorizer(lambda action,table,*_:sqlite3.SQLITE_DENY
                              if action==sqlite3.SQLITE_READ and table=='observations'
                              else sqlite3.SQLITE_OK)
            yield db
    monkeypatch.setattr(store,'connect',guarded)
    stats=store.collection_stats(slot=now.isoformat(),depth=2)
    assert stats['observations']==3 and stats['snapshots']==2 and stats['products']==2
    assert stats['active_queries']==2


def test_repeated_connection_failures_stop_cycle_without_disabling_queries(tmp_path,monkeypatch):
    cfg=replace(Settings.from_env(),database_path=tmp_path/'retry.db',top_n=1,query_delay_seconds=0)
    store=Store(cfg.database_path)
    store.initialize()
    store.sync_queries(['one','two','three','four'])
    calls=[]
    class Client:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def page(self,query,number):
            calls.append(query)
            raise FetchError('WB request failed after 3 attempts: page did not load')
    monkeypatch.setattr(cli,'build_client',lambda cfg:Client())
    assert cli.run_cycle(cfg,store)==(3,0,3)
    assert calls==['one','two','three'] and len(store.enabled_queries())==4
    assert all(r['status']=='failed' for r in store.recent_runs())
