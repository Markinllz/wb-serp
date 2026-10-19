from __future__ import annotations

import base64
import sys
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from wb_serp.browser_transport import BrowserTransport, validate_browser_url
from wb_serp.client import WildberriesClient

ENDPOINT = 'https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search'


@pytest.mark.parametrize('url', [
    'http://www.wildberries.ru/__internal/u-search/exactmatch/a/search',
    'https://evil.test/__internal/u-search/exactmatch/a/search',
    'https://www.wildberries.ru:8080/__internal/u-search/exactmatch/a/search',
    'https://user:secret@www.wildberries.ru/__internal/u-search/exactmatch/a/search',
    'https://www.wildberries.ru/__internal/banners/search',
])
def test_browser_restricts_host_and_product_endpoint(url):
    with pytest.raises(ValueError):
        validate_browser_url(url)


def test_browser_does_not_mix_safari_session(tmp_path):
    with pytest.raises(ValueError, match='unset WB_SESSION_FILE'):
        WildberriesClient(search_url=ENDPOINT, destination=1, http_transport='browser',
                          session_file=tmp_path / 'safari.json')


def test_decoded_browser_body_and_headers(monkeypatch, tmp_path):
    calls = []
    body = b'{"products": [{"id": 1}]}'

    def evaluate(script, arg):
        calls.append(arg)
        return {'status': 200, 'headers': [('content-encoding', 'gzip'),
                ('content-length', '9'), ('content-type', 'application/json')],
                'body': base64.b64encode(body).decode()}
    fake = ModuleType('playwright.sync_api')
    fake.Error = RuntimeError
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', fake)
    transport = BrowserTransport(profile_path=tmp_path)
    monkeypatch.setattr(transport, '_start', lambda: None)
    transport._ready = True
    transport._page = SimpleNamespace(evaluate=evaluate)
    request = httpx.Request('GET', ENDPOINT, headers={'cookie': 'safari=must-not-leak'})
    response = transport.handle_request(request)
    assert response.json()['products'][0]['id'] == 1
    assert 'content-encoding' not in response.headers
    assert 'cookie' not in calls[0]['headers']
    assert transport.metrics['fetches'] == 1


def test_unresolved_browser_check_is_retryable_timeout(monkeypatch, tmp_path):
    transport = BrowserTransport(profile_path=tmp_path)
    monkeypatch.setattr(transport, '_start', lambda: None)
    monkeypatch.setattr(transport, '_warmup', lambda: False)
    with pytest.raises(httpx.ReadTimeout, match='denial was not confirmed'):
        transport.handle_request(httpx.Request('GET', ENDPOINT))
    assert transport.metrics['fetches'] == 0


@pytest.mark.parametrize('diagnostics,status', [
    ({'navigation_status': 403}, 403), ({'catalog_status': 429}, 429),
    ({'challenge_visible': True}, 498),
])
def test_confirmed_browser_denial_remains_blocked(monkeypatch, tmp_path, diagnostics, status):
    transport = BrowserTransport(profile_path=tmp_path)
    transport.diagnostics.update(diagnostics)
    monkeypatch.setattr(transport, '_start', lambda: None)
    monkeypatch.setattr(transport, '_warmup', lambda: False)
    assert transport.handle_request(httpx.Request('GET', ENDPOINT)).status_code == status


def test_bootstrap_failure_does_not_restart_site_countdown(monkeypatch, tmp_path):
    transport = BrowserTransport(profile_path=tmp_path)
    def unexpected():
        raise AssertionError('must not navigate again after failed bootstrap')
    monkeypatch.setattr(transport, '_warmup', unexpected)
    assert transport.refresh_session() is False
    assert transport.metrics['refreshes'] == 0


@pytest.mark.parametrize('recover', [False, True])
def test_browser_session_refresh_is_bounded(recover):
    requests = []
    refreshes = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1 or not recover:
            return httpx.Response(498)
        return httpx.Response(200, json={'products': [{'id': 123}]})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WildberriesClient(search_url=ENDPOINT, destination=1, client=http,
                               retries=3, delay_seconds=0, sleeper=lambda _: None)
    def refresh():
        refreshes.append(1)
        return True
    client._browser_transport = SimpleNamespace(refresh_session=refresh)
    with http:
        if recover:
            assert client.page('example', 1).products[0]['id'] == 123
        else:
            from wb_serp.client import FetchError
            with pytest.raises(FetchError) as error:
                client.page('example', 1)
            assert error.value.cause == 'browser_challenge'
    assert len(refreshes) == 1
    assert len(requests) == 2
