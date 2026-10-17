from datetime import datetime, timezone

import pytest

from wb_serp.presentation import next_collection, product_summaries


def test_next_collection_is_next_boundary_in_any_timezone():
    now = datetime.fromisoformat("2026-10-17T05:20:00+03:00")
    assert next_collection(now) == datetime(2026, 10, 17, 3, tzinfo=timezone.utc)
    assert next_collection(now.replace(minute=0)) == datetime(2026, 10, 17, 3, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        next_collection(datetime(2026, 10, 17))


def test_product_summary_does_not_invent_rank_or_bridge_unknown_snapshot():
    def row(rank, status="success", price=10099):
        return {
            "nm_id": 7,
            "position": rank,
            "run_status": status,
            "price_kopecks": price,
            "started_at": "2026-10-17T02:00:00+00:00",
            "requested_top_n": 200,
        }

    history = [row(4), row(None), row(2)]
    summary = product_summaries(history, [7], metric="position")[0]
    assert summary["value"] == 2 and summary["delta"] is None
    assert product_summaries([row(4), row(2)], [7], metric="position")[0]["delta"] == 2
    missing = product_summaries([row(None)], [7], metric="position")[0]
    assert missing["value"] is None and missing["state"] == "outside"
    assert product_summaries([row(None, "partial")], [7], metric="position")[0]["state"] == "unknown"
    assert product_summaries([row(1, price=0)], [7], metric="price")[0]["value"] == 0
    assert (
        product_summaries([row(1, price=10099), row(1, price=11001)], [7], metric="price")[0]["delta"]
        == 902
    )
