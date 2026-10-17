from dataclasses import asdict
import io
import json
import stat

import httpx
import pytest

from scripts.import_browser_request import main as import_main
from wb_serp.browser_session import load_session, parse_curl
from wb_serp.client import FetchError, WildberriesClient


ENDPOINT = 'https://www.wildberries.ru/__internal/u-search/exactmatch/sng/common/v18/search'
CURL = (
    f"curl '{ENDPOINT}?query=test&dest=-1257786&curr=rub&sort=popular&page=1&appType=64' "
    "-H 'user-agent: TestBrowser/1.0' -H 'x-spa-version: 13.15.1' "
    "-H 'authorization: do-not-import-account-token' -b 'x_wbaas_token=test-secret' --compressed"
)


def save_session(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(asdict(parse_curl(CURL))))
    return path


def test_copy_as_curl_preserves_endpoint_and_session_without_account_authorization():
    session = parse_curl(CURL)
    assert session.endpoint == ENDPOINT
    assert session.destination == -1257786
    assert session.headers['cookie'] == 'x_wbaas_token=test-secret'
    assert session.headers['user-agent'] == 'TestBrowser/1.0'
    assert 'authorization' not in session.headers
    assert session.params['appType'] == '64'


def test_known_wb_banner_request_can_supply_session_but_never_banner_product_filters():
    copied = (
        "curl 'https://www.wildberries.ru/__internal/banners/shelfs/search?"
        "appType=1&curr=rub&dest=1259570991&spp=30&hide_vflags=4294967296"
        "&ab_testing=false&locale=ru&gender=common&lang=ru&scale=5"
        "&query=test&minquantity=13&limit=26' -X 'GET' "
        "-H 'Cookie: x_wbaas_token=synthetic-token'"
    )
    session = parse_curl(copied)
    assert session.endpoint == (
        'https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search'
    )
    assert session.destination == 1259570991
    assert session.headers['cookie'] == 'x_wbaas_token=synthetic-token'
    assert session.params['limit'] == '100'
    assert session.params['sort'] == 'popular'
    assert session.params['resultset'] == 'catalog'
    assert session.params['inheritFilters'] == 'false'
    assert not {'gender', 'scale', 'minquantity'} & session.params.keys()
    for unsafe in (copied.replace('https://', 'http://'),
                   copied.replace('www.wildberries.ru', 'example.test'),
                   copied.replace('www.wildberries.ru', 'attacker@www.wildberries.ru')):
        with pytest.raises(ValueError):
            parse_curl(unsafe)


@pytest.mark.parametrize('copied', [
    CURL.replace('www.wildberries.ru', 'example.test'),
    CURL.replace('https://', 'http://'),
    CURL.replace('/search?', '/account?'),
    CURL + ' --data-raw private',
    CURL + ' ; touch /tmp/wb-should-never-exist',
    CURL.replace('sort=popular', 'sort=priceup'),
    CURL.replace('curr=rub', 'curr=byn'),
    CURL.replace('page=1', 'page=2'),
    CURL.replace('query=test', 'query=test&fbrand=123'),
    CURL.replace("-b 'x_wbaas_token=test-secret'", "-b '/private/cookies.txt'"),
])
def test_rejects_non_search_requests_shell_operations_and_filtered_results(copied):
    with pytest.raises(ValueError):
        parse_curl(copied)


def test_import_stores_private_file_and_prints_only_nonsecret_configuration(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'session.json'
    monkeypatch.setattr('sys.stdin', io.StringIO(CURL))
    assert import_main(['--stdin', '--output', str(path)]) == 0
    captured = capsys.readouterr()
    assert 'test-secret' not in captured.out + captured.err
    assert 'do-not-import-account-token' not in path.read_text()
    assert 'WB_HTTP_TRANSPORT=curl_cffi' in captured.out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_session(path).headers['cookie'] == 'x_wbaas_token=test-secret'


def test_invalid_session_never_echoes_json_contents(tmp_path):
    path = tmp_path / 'invalid.json'
    path.write_text('{"cookie":"private-token", broken JSON')
    with pytest.raises(ValueError) as exc:
        load_session(path)
    assert 'private-token' not in str(exc.value)


def test_reuses_browser_headers_and_real_search_parameters_on_each_page(tmp_path):
    path = save_session(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'products': [{'id': int(request.url.params['page'])}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with WildberriesClient(search_url=ENDPOINT, destination=-1257786,
                               session_file=path, client=http, delay_seconds=0) as client:
            client.page('постельное белье 2 спальное', 1)
            client.page('постельное белье 2 спальное', 2)
    assert [r.url.params['page'] for r in calls] == ['1', '2']
    for request in calls:
        assert request.url.params['query'] == 'постельное белье 2 спальное'
        assert request.url.params['appType'] == '64'
        assert request.url.params['dest'] == '-1257786'
        assert request.headers['user-agent'] == 'TestBrowser/1.0'
        assert request.headers['cookie'] == 'x_wbaas_token=test-secret'
        assert 'authorization' not in request.headers


def test_safari_session_uses_safari_network_profile(tmp_path):
    path = save_session(tmp_path)
    session = json.loads(path.read_text())
    session['headers']['user-agent'] = (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15'
    )
    path.write_text(json.dumps(session))
    with WildberriesClient(search_url=ENDPOINT, destination=-1257786,
                           session_file=path, http_transport='curl_cffi') as client:
        assert client._client._transport.session.impersonate == 'safari'
        assert client._client.headers['user-agent'] == session['headers']['user-agent']


def test_safari_search_import_preserves_context_but_removes_previous_query_filters(tmp_path):
    copied = CURL.replace('dest=-1257786', 'dest=1259570991').replace(
        'query=test', 'query=test&locale=ru&hide_vflags=4294967296'
        '&inheritFilters=true&q1=old+tires&q2=test',
    ) + " -H 'deviceid: synthetic-device' -H 'x-queryid: synthetic-request'"
    session = parse_curl(copied)
    assert session.destination == 1259570991
    assert session.params['locale'] == 'ru'
    assert session.params['hide_vflags'] == '4294967296'
    assert session.params['inheritFilters'] == 'false'
    assert 'q1' not in session.params and 'q2' not in session.params
    path = tmp_path / 'safari.json'
    path.write_text(json.dumps(asdict(session)))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'products': [{'id': 1}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with WildberriesClient(search_url=ENDPOINT, destination=1259570991,
                               session_file=path, client=http, delay_seconds=0) as client:
            client.page('new bedding', 1)
    request = calls[0]
    assert request.url.params['query'] == 'new bedding'
    assert request.url.params['inheritFilters'] == 'false'
    assert 'q1' not in request.url.params and 'q2' not in request.url.params
    assert request.headers['referer'].endswith('search=new+bedding')
    assert request.headers['deviceid'] == 'synthetic-device'


@pytest.mark.parametrize('endpoint,dest', [(ENDPOINT, -1),
                                          ('https://search.wb.ru/exactmatch/ru/common/v4/search',
                                           -1257786)])
def test_session_cannot_silently_change_region_or_send_cookies_to_another_endpoint(tmp_path, endpoint, dest):
    with pytest.raises(ValueError):
        WildberriesClient(search_url=endpoint, destination=dest,
                          session_file=save_session(tmp_path))


@pytest.mark.parametrize('status,body,cause', [
    (403, '', 'access_denied'),
    (429, '', 'rate_limit'),
    (498, '<html>challenge</html>', 'browser_challenge'),
    (200, '<html><script src="/__wbaas/challenges/antibot/test.js"></script>',
     'browser_challenge'),
])
def test_block_diagnostics_stop_without_retries_or_secret_disclosure(status, body, cause):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=body, headers={
            'server': 'wbaas', 'status-no-id': 'PG-41-EL', 'x-request-id': 'request-123',
            'set-cookie': 'x_wbaas_token=never-print', 'x-private': 'never-print',
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with WildberriesClient(search_url=ENDPOINT, destination=-1257786, client=http,
                               retries=3, delay_seconds=0, sleeper=lambda _: None) as client:
            with pytest.raises(FetchError) as error:
                client.page('test', 1)
    assert len(calls) == (3 if status == 429 else 1)
    assert error.value.blocked
    assert error.value.http_status == status
    assert error.value.cause == cause
    assert error.value.response_headers['x-request-id'] == 'request-123'
    assert 'never-print' not in str(error.value) + str(error.value.response_headers)
