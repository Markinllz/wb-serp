from __future__ import annotations

from datetime import datetime, timezone

from wb_serp.client import FetchError
from wb_serp.models import SearchPage
from wb_serp.service import Collector
from tests.conftest import product


class FakeClient:
    def __init__(self, pages=None, *, error=None):
        self.pages = pages or []
        self.error = error
        self.calls = []

    def page(self, query, page_number):
        self.calls.append((query, page_number))
        if self.error:
            raise self.error
        return SearchPage(tuple(self.pages[page_number - 1]), page_number)


def test_complete_run_retains_history_and_exact_positions(store):
    client = FakeClient([[product(1), product(2)], [product(3), product(4)]])
    query = store.enabled_queries()[0]
    collector = Collector(store, client, destination=-1257786, top_n=4)
    first = collector.collect(query["id"], query["phrase"])
    assert first.status == "success"
    assert first.observed == 4
    assert [r["position"] for r in store.latest_observations(query["id"])] == [1, 2, 3, 4]
    assert [x[1] for x in client.calls] == [1, 2]

    client.pages = [[product(2), product(1), product(5), product(6)]]
    second = collector.collect(query["id"], query["phrase"])
    assert second.status == "success"
    assert second.run_id != first.run_id
    assert len(store.recent_runs(query["id"])) == 2
    assert [r["nm_id"] for r in store.latest_observations(query["id"])] == [2, 1, 5, 6]
    assert {p["nm_id"] for p in store.known_products(query["id"])} == {1, 2, 3, 4, 5, 6}


def test_partial_run_never_appears_in_rank_timeline(store):
    query = store.enabled_queries()[0]
    client = FakeClient([[product(1), product(2)], []])
    result = Collector(store, client, destination=1, top_n=3).collect(query["id"], query["phrase"])
    assert result.status == "partial"
    assert result.observed == 2
    assert store.rank_history(query["id"], [1]) == []
    assert store.latest_observations(query["id"]) == []
    assert store.recent_runs(query["id"])[0]["observed_count"] == 2


def test_failed_schema_does_not_create_success(store):
    query = store.enabled_queries()[0]
    result = Collector(store, FakeClient([[product(1), product(1)]]),
                       destination=1, top_n=2).collect(query["id"], query["phrase"])
    assert result.status == "failed"
    assert "Duplicate" in result.error
    assert store.known_products(query["id"]) == []


def test_blocked_request_does_not_pretend_serp_is_empty(store):
    query = store.enabled_queries()[0]
    error = FetchError("HTTP 429", blocked=True)
    result = Collector(store, FakeClient(error=error), destination=1, top_n=2).collect(
        query["id"], query["phrase"])
    assert result.status == "blocked"
    assert store.rank_history(query["id"], [1]) == []


def test_null_gaps_when_product_disappears_and_returns(store):
    query = store.enabled_queries()[0]
    client = FakeClient([[product(1), product(2)]])
    collector = Collector(store, client, destination=1, top_n=2)
    collector.collect(query["id"], query["phrase"])
    client.pages = [[product(2), product(3)]]
    collector.collect(query["id"], query["phrase"])
    client.pages = [[product(4), product(1)]]
    collector.collect(query["id"], query["phrase"])
    hist = store.rank_history(query["id"], [1])
    assert [row["position"] for row in hist] == [1, None, 2]
    assert all(row["requested_top_n"] == 2 for row in hist)


def test_interrupted_status_is_visible_but_not_analytics(store):
    query = store.enabled_queries()[0]
    store.start_run(run_id="dead", query_id=query["id"],
                    scheduled_for="2026-10-16T01:00:00+00:00",
                    started_at=datetime.now(timezone.utc), requested_top_n=2, destination=1)
    assert store.interrupt_abandoned_runs() == 1
    assert store.recent_runs(query["id"])[0]["status"] == "interrupted"


def test_region_is_immutable_for_same_dataset(store):
    import pytest

    query = store.enabled_queries()[0]
    collector = Collector(store, FakeClient([[product(1)]]), destination=-1257786, top_n=1)
    assert collector.collect(query["id"], query["phrase"]).status == "success"
    other = Collector(store, FakeClient([[product(1)]]), destination=999, top_n=1)
    with pytest.raises(ValueError, match="WB_DESTINATION"):
        other.collect(query["id"], query["phrase"])
    assert len(store.recent_runs(query["id"])) == 1


def test_migrates_legacy_schema_v1(tmp_path):
    import sqlite3
    from wb_serp.store import Store

    path = tmp_path / "legacy.db"
    from contextlib import closing
    store = Store(path)
    store.initialize()
    # Model a real v1 database: all original tables exist, dataset_settings not yet.
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE dataset_settings")
        db.execute("PRAGMA user_version=1")
        db.commit()
    store.initialize()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE name='dataset_settings'").fetchone()
