from dataclasses import asdict
from datetime import datetime, timezone
from email.utils import format_datetime
import json
import stat

import httpx
import pytest

from wb_serp.browser_session import load_session, parse_curl
from wb_serp.client import FetchError, WildberriesClient, retry_after_seconds

ENDPOINT = 'https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search'


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def make_client(handler, clock, **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WildberriesClient(
        search_url=ENDPOINT, destination=-1257786, client=http,
        clock=lambda: clock.now, wall_clock=lambda: 1700000000 + clock.now,
        sleeper=clock.sleep, random_source=lambda: 0, retry_jitter_seconds=0, **kwargs,
    )
    return client, http


def session_file(tmp_path):
    path = tmp_path / 'session.json'
    s = parse_curl(f"curl '{ENDPOINT}?query=test&dest=-1257786' "
                   "-H 'Cookie: x_wbaas_token=original; _cp=1'")
    path.write_text(json.dumps(asdict(s)))
    return path


def test_slow_browser_bootstrap_does_not_remove_pause_before_next_page():
    clock = Clock()
    calls = []
    def handler(request):
        calls.append(clock.now)
        if len(calls) == 1:
            clock.now += 30
        return httpx.Response(200, json={'products': []})
    client, http = make_client(handler, clock, delay_seconds=5)
    with http, client:
        client.page('same query', 1)
        client.page('same query', 2)
    assert calls == [0, 35]


@pytest.mark.parametrize('value,expected', [('12', 12), ('0', 0), ('invalid', None),
                                           ('-5', None), (None, None)])
def test_retry_after_seconds_parsing(value, expected):
    assert retry_after_seconds(value, 1700000000) == expected


@pytest.mark.parametrize('status', [408, 425, 429, 500, 502, 503, 504])
def test_transient_status_retries_honour_retry_after_and_pacing(status):
    clock = Clock()
    calls = []

    def handler(request):
        calls.append(clock.now)
        if len(calls) == 1:
            return httpx.Response(status, headers={'Retry-After': '7'})
        return httpx.Response(200, json={'products': [{'id': 1}]})

    client, http = make_client(handler, clock, delay_seconds=5, retry_base_seconds=2)
    with http, client:
        assert client.page('test', 1).products[0]['id'] == 1
        client.page('another phrase', 1)
    assert len(calls) == 3
    assert calls[1] - calls[0] >= 7
    assert all(b - a >= 5 for a, b in zip(calls, calls[1:]))
    assert client.metrics == {'requests': 3, 'retries': 1, 'session_refreshes': 0}


def test_http_date_retry_after_uses_wall_clock_without_early_retry():
    clock = Clock()
    calls = []
    date = format_datetime(datetime.fromtimestamp(1700000013, timezone.utc), usegmt=True)

    def handler(request):
        calls.append(clock.now)
        return (httpx.Response(503, headers={'Retry-After': date}) if len(calls) == 1
                else httpx.Response(200, json={'products': []}))

    client, http = make_client(handler, clock, delay_seconds=5)
    with http, client:
        client.page('test', 1)
    assert calls == [0, 13]


@pytest.mark.parametrize('status', [429, 503])
def test_long_retry_after_stops_instead_of_truncating_server_wait(status):
    clock = Clock()
    calls = []

    def handler(request):
        calls.append(clock.now)
        return httpx.Response(status, headers={'Retry-After': '3600'})

    client, http = make_client(handler, clock, delay_seconds=5)
    with http, client, pytest.raises(FetchError) as error:
        client.page('test', 1)
    assert error.value.blocked
    assert calls == [0]
    assert clock.waits == []


def test_retry_budget_exhaustion_is_bounded_and_exponential():
    clock = Clock()
    calls = []

    def handler(request):
        calls.append(clock.now)
        return httpx.Response(503)

    client, http = make_client(handler, clock, delay_seconds=0, retry_base_seconds=2, retries=3)
    with http, client, pytest.raises(FetchError):
        client.page('test', 1)
    assert calls == [0, 2, 6]
    assert client.metrics['retries'] == 2


def test_server_cookie_rotation_is_used_on_next_page_and_persisted_privately(tmp_path):
    path = session_file(tmp_path)
    clock = Clock()
    cookies = []

    def handler(request):
        cookies.append(request.headers.get('cookie', ''))
        return httpx.Response(200, json={'products': [{'id': len(cookies)}]}, headers={
            'Set-Cookie': 'x_wbaas_token=rotated; Domain=.wildberries.ru; Path=/; Secure',
        })

    client, http = make_client(handler, clock, delay_seconds=0, session_file=path)
    with http, client:
        client.page('test', 1)
        client.page('test', 2)
    assert 'x_wbaas_token=original' in cookies[0]
    assert 'x_wbaas_token=rotated' in cookies[1]
    assert 'original' not in cookies[1]
    assert 'x_wbaas_token=rotated' in load_session(path).headers['cookie']
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_498_can_retry_once_only_when_server_supplies_a_different_auth_cookie(tmp_path):
    path = session_file(tmp_path)
    clock = Clock()
    cookies = []

    def handler(request):
        cookies.append(request.headers['cookie'])
        if len(cookies) == 1:
            return httpx.Response(498, headers={'Set-Cookie': 'x_wbaas_token=refreshed; Path=/'})
        return httpx.Response(200, json={'products': [{'id': 1}]})

    client, http = make_client(handler, clock, delay_seconds=0, session_file=path)
    with http, client:
        assert client.page('test', 1).products[0]['id'] == 1
    assert len(cookies) == 2 and 'refreshed' in cookies[1]
    assert client.metrics['session_refreshes'] == 1
    assert clock.waits == [5]


@pytest.mark.parametrize('cookie', [None, '_wbauid=changed; Path=/',
                                  'x_wbaas_token=original; Path=/',
                                  'x_wbaas_token=foreign; Domain=example.test; Path=/',
                                  'x_wbaas_token=wrong-path; Path=/another-api'])
def test_challenge_without_valid_new_auth_cookie_is_not_retried(tmp_path, cookie):
    path = session_file(tmp_path)
    clock = Clock()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(498, headers={'Set-Cookie': cookie} if cookie else {})

    client, http = make_client(handler, clock, delay_seconds=0, session_file=path)
    with http, client, pytest.raises(FetchError) as error:
        client.page('test', 1)
    assert error.value.cause == 'browser_challenge'
    assert len(calls) == 1 and clock.waits == []
    assert 'original' in load_session(path).headers['cookie']


def test_repeated_challenge_with_new_tokens_still_stops_after_one_session_retry(tmp_path):
    path = session_file(tmp_path)
    clock = Clock()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(498, headers={'Set-Cookie': f'x_wbaas_token=new-{len(calls)}; Path=/'})

    client, http = make_client(handler, clock, delay_seconds=0, session_file=path, retries=5)
    with http, client, pytest.raises(FetchError):
        client.page('test', 1)
    assert len(calls) == 2 and client.metrics['session_refreshes'] == 1


def test_manual_session_import_is_not_overwritten_by_an_older_client(tmp_path):
    path = session_file(tmp_path)
    clock = Clock()

    def handler(request):
        return httpx.Response(200, json={'products': []},
                              headers={'Set-Cookie': 'x_wbaas_token=server-update; Path=/'})

    client, http = make_client(handler, clock, delay_seconds=0, session_file=path)
    newer = json.loads(path.read_text())
    newer['headers']['cookie'] = 'x_wbaas_token=manual-new-session'
    path.write_text(json.dumps(newer))
    with http, client:
        client.page('test', 1)
    assert load_session(path).headers['cookie'] == 'x_wbaas_token=manual-new-session'
