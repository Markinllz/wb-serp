from datetime import datetime, timedelta, timezone
from dataclasses import replace

from wb_serp import cli
from wb_serp.client import FetchError
from wb_serp.config import Settings
from wb_serp.models import Product, SearchPage
from wb_serp.store import Store


def test_queue_prioritizes_untried_failed_and_old_valid_data(tmp_path):
    store = Store(tmp_path/'queue.db')
    store.initialize()
    store.sync_queries(['fresh','old','failed','new','short'])
    now = datetime.now(timezone.utc)
    for qid,status,at in [(1,'success',now),(2,'success',now-timedelta(hours=2)),
                          (3,'failed',now),(5,'partial',now-timedelta(hours=1))]:
        store.start_run(run_id=str(qid),query_id=qid,scheduled_for=at.isoformat(),started_at=at,
                        requested_top_n=2,destination=99)
        store.complete_run(run_id=str(qid),status=status,finished_at=at,
                           products=[Product(n,n,None,None,None,None,None,'x')
                                     for n in range(1,3 if status=='success' else 2)]
                           if status!='failed' else [])
    assert [q['id'] for q in store.collection_queue()]==[4,3,2,5,1]
    assert [q['id'] for q in store.enabled_queries()]==[1,2,3,4,5]
    assert next(q for q in store.queries_dashboard() if q['id']==5)['last_valid'] is not None


def test_missing_second_page_retries_whole_snapshot_once(tmp_path,monkeypatch):
    cfg=replace(Settings.from_env(),database_path=tmp_path/'retry.db',top_n=4,query_delay_seconds=0)
    store=Store(cfg.database_path)
    store.initialize()
    store.sync_queries(['query'])
    calls=[]
    class Client:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def page(self,query,number):
            calls.append(number)
            if len(calls)==2:
                raise FetchError('WB request failed after 3 attempts: WB did not render search page 2')
            start=1 if len(calls)==1 else (10 if number==1 else 12)
            return SearchPage(tuple({'id':n} for n in range(start,start+2)),number,total=100)
    monkeypatch.setattr(cli,'build_client',lambda cfg:Client())
    assert cli.run_cycle(cfg,store)==(1,0,0)
    assert calls==[1,2,1,2]
    assert [r['nm_id'] for r in store.latest_observations(1)]==[10,11,12,13]
    assert [r['status'] for r in store.recent_runs()]==['success','failed']
