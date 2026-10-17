from types import SimpleNamespace

from wb_serp.cdp_transport import CdpBrowserTransport

ENDPOINT = 'https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search'


def response(query='original', resultset='catalog', status=200):
    return SimpleNamespace(
        url=ENDPOINT + f'?query={query}&resultset={resultset}&dest=99&q1=old&inheritFilters=true'
                       '&appType=1&locale=ru&hide_vflags=123', status=status,
        request=SimpleNamespace(headers={'x-spa-version': '14.28.4', 'deviceid': 'own-profile',
                                        'cookie': 'secret', 'authorization': 'secret'}),
        json=lambda: {'products': [{'id': 1}]})


def test_cdp_learns_frontend_context_without_cookies_or_old_search(tmp_path):
    transport = CdpBrowserTransport(profile_path=tmp_path)
    transport._current_query = 'original'
    transport._capture_product_response(response())
    assert transport._frontend_ready
    assert transport._frontend_headers == {'x-spa-version': '14.28.4', 'deviceid': 'own-profile'}
    assert transport._search_params == {'resultset': 'catalog', 'appType': '1',
                                         'locale': 'ru', 'hide_vflags': '123'}


def test_cdp_does_not_calibrate_from_filters_failed_or_different_query(tmp_path):
    transport = CdpBrowserTransport(profile_path=tmp_path)
    transport._current_query = 'original'
    for r in [response(resultset='filters'), response(status=403), response(query='another')]:
        transport._capture_product_response(r)
    assert not transport._frontend_ready
    assert transport._search_params == {}


def test_cdp_close_releases_all_resources_even_after_cleanup_error(tmp_path):
    transport = CdpBrowserTransport(profile_path=tmp_path)
    calls = []
    def fail():
        calls.append('browser')
        raise RuntimeError('already closed')
    transport._browser = SimpleNamespace(close=fail)
    transport._sb = SimpleNamespace(quit=lambda: calls.append('sb'))
    transport._display = SimpleNamespace(stop=lambda: calls.append('display'))
    transport._acquire_profile()
    transport.close()
    assert calls == ['browser', 'sb', 'display']
    assert transport._lock is None


def test_cdp_returns_the_captured_frontend_page_without_replaying_api(tmp_path):
    import base64
    import json
    import httpx
    import pytest
    transport = CdpBrowserTransport(profile_path=tmp_path)
    transport._rendered_destination = '99'
    transport._frontend_pages = {1:{'products':[{'id':701}]}}
    request = httpx.Request('GET', ENDPOINT, params={'dest':99,'page':1})
    response = transport._fetch_frontend_page(request)
    assert json.loads(base64.b64decode(response['body'])) == {'products':[{'id':701}]}
    with pytest.raises(httpx.TransportError, match='region'):
        transport._fetch_frontend_page(httpx.Request('GET', ENDPOINT, params={'dest':100}))
