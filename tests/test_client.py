from __future__ import annotations

import httpx
import pytest

from wb_serp.client import FetchError, WildberriesClient


def test_escaped_query_preserves_exact_utf8():
    captures = []

    def handler(request):
        captures.append(request)
        return httpx.Response(200, json={"data": {"products": [{"id": 123}]}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WildberriesClient(search_url="https://example.test/search", destination=-1,
                               client=http, delay_seconds=0)
    assert client.page("постельное бельё 1,5", 1).products[0]["id"] == 123
    assert captures[0].url.params["query"] == "постельное бельё 1,5"
    assert captures[0].url.params["page"] == "1"
    assert captures[0].url.params["dest"] == "-1"
    http.close()


@pytest.mark.parametrize("status", [401, 403, 429])
def test_denied_access_stops_without_evasion(status):
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status)))
    client = WildberriesClient(search_url="https://example.test/search", destination=-1,
                               client=http, retries=3, delay_seconds=0, sleeper=lambda _: None)
    with pytest.raises(FetchError, match="denied/rate-limited") as exc:
        client.page("бельё", 1)
    assert exc.value.blocked is True
    http.close()


@pytest.mark.parametrize('total,expected', [(106, 106), (0, 0), (-1, None), ('106', None),
                                          (True, None), (None, None)])
def test_source_total_is_validated_without_changing_product_order(total, expected):
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={'products': [{'id': 2}, {'id': 1}], 'total': total})))
    client = WildberriesClient(search_url='https://example.test/search',destination=1,
                               client=http,delay_seconds=0)
    with http:
        page = client.page('literal query', 1)
    assert page.total == expected
    assert [p['id'] for p in page.products] == [2, 1]
