"""Verify the live diagnostic protocol, without pretending mock products are live."""
from __future__ import annotations

import httpx
import pytest

from scripts.probe_live_wb import ProbeFailure, probe
from wb_serp.client import WildberriesClient
from tests.conftest import product


def client_for(handler):
    return WildberriesClient(
        search_url="https://example.test/search", destination=-1257786,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retries=1, delay_seconds=0,
    )


@pytest.mark.parametrize("payload_style", ["top_level", "nested"])
def test_probe_confirms_200_unique_ranked_products_and_coverage(payload_style):
    requested = []

    def handler(request):
        page = int(request.url.params["page"])
        requested.append(page)
        assert request.url.params["dest"] == "-1257786"
        items = [product(i, price=152350 if i % 2 else None)
                 for i in range((page - 1) * 100 + 1, page * 100 + 1)]
        body = {"products": items} if payload_style == "top_level" else {"data": {"products": items}}
        return httpx.Response(200, json=body)

    with client_for(handler) as client:
        result = probe(client, query="постельное белье 2 спальное")
    assert result["status"] == "PASS"
    assert result["unique_nm_ids"] == 200
    assert result["sequential_rank_range"] == [1, 200]
    assert result["page_sizes"] == [100, 100]
    assert result["non_null_attribute_counts"]["price_rub"] == 100
    assert result["non_null_attribute_counts"]["delivery_days"] == 0
    assert result["sample_products"][0]["price_rub"] == "1523.50"
    assert requested == [1, 2]


def test_probe_fails_when_second_page_repeats_first():
    def handler(request):
        return httpx.Response(200, json={"products": [product(i) for i in range(1, 101)]})

    with client_for(handler) as client, pytest.raises(ProbeFailure, match="Duplicate nm_id"):
        probe(client, query="белье")


def test_probe_fails_on_partial_first_page():
    def handler(request):
        first = int(request.url.params["page"]) == 1
        return httpx.Response(200, json={"products": [product(i) for i in range(1, 51)] if first else []})

    with client_for(handler) as client, pytest.raises(ProbeFailure, match="only 50/200"):
        probe(client, query="белье")
