"""Import a user-supplied search request without executing shell commands."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
from urllib.parse import parse_qsl, urlsplit, urlunsplit


ALLOWED_HOSTS = {'www.wildberries.ru', 'search.wb.ru', 'u-search.wb.ru'}
ALLOWED_HEADERS = {
    'accept', 'accept-language', 'user-agent', 'cookie', 'origin', 'referer',
    'x-requested-with', 'x-spa-version', 'x-userid', 'x-wbaas-token',
    'x-guardium-token', 'wbx-validation-key', 'sec-ch-ua', 'sec-ch-ua-mobile',
    'sec-ch-ua-platform', 'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site',
    'deviceid', 'x-queryid',
}
# Preserve only search settings, never arbitrary account/session URL parameters.
ALLOWED_PARAMS = {
    'appType', 'curr', 'dest', 'lang', 'page', 'limit', 'query', 'resultset',
    'sort', 'spp', 'ab_testing', 'suppressSpellcheck', 'hide_dtype',
    'hide_vflags', 'locale', 'inheritFilters',
}
TRANSIENT_QUERY_PARAMS = {'q1', 'q2', 'inheritFilters'}
BANNER_PATH = '/__internal/banners/shelfs/search'
PRODUCT_SEARCH_PATH = '/__internal/u-search/exactmatch/ru/common/v18/search'
BANNER_ONLY_PARAMS = {'gender', 'scale', 'minquantity', 'limit'}


@dataclass(frozen=True)
class BrowserSession:
    endpoint: str
    destination: int
    headers: dict[str, str]
    params: dict[str, str]


def validate_search_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str):
        raise ValueError('Session endpoint must be a URL')
    url = urlsplit(endpoint)
    if (url.scheme != 'https' or url.hostname not in ALLOWED_HOSTS
            or url.username or url.password or url.port not in (None, 443)
            or not url.path.endswith('/search') or '/exactmatch/' not in url.path
            or url.query or url.fragment):
        raise ValueError('Session must target an HTTPS Wildberries exactmatch search endpoint')


def validate_session(value: dict) -> BrowserSession:
    endpoint = value.get('endpoint', '')
    validate_search_endpoint(endpoint)
    headers = value.get('headers', {})
    params = value.get('params', {})
    if not isinstance(headers, dict) or not isinstance(params, dict):
        raise ValueError('Session headers and params must be objects')
    if any(not isinstance(k, str) or not isinstance(v, str) or '\r' in v or '\n' in v
           for k, v in headers.items()):
        raise ValueError('Invalid session headers')
    headers = {k.lower(): v for k, v in headers.items() if k.lower() in ALLOWED_HEADERS}
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in params.items()):
        raise ValueError('Invalid session parameters')
    params = {k: v for k, v in params.items() if k in ALLOWED_PARAMS}
    if params.get('curr', 'rub') != 'rub' or params.get('sort', 'popular') != 'popular':
        raise ValueError('Use a RUB search with popular sorting and no filters')
    if params.get('inheritFilters', 'false').lower() != 'false':
        raise ValueError('Session must not inherit filters from a previous query')
    destination = value.get('destination')
    if type(destination) is not int or params.get('dest') != str(destination):
        raise ValueError('Session must contain the exact delivery destination from the request')
    return BrowserSession(endpoint, destination, headers, params)


def parse_curl(text: str) -> BrowserSession:
    """Parse Copy as cURL from Safari/Chrome. Never pass input to a shell."""
    if len(text) > 1_000_000:
        raise ValueError('Copied request is too large')
    if "$'" in text:
        raise ValueError('Use Copy as cURL (bash) with ordinary quoted strings')
    try:
        tokens = shlex.split(text.replace('\\\n', ''))
    except ValueError:
        raise ValueError('Invalid cURL quoting') from None
    if not tokens or tokens.pop(0) not in ('curl', '/usr/bin/curl'):
        raise ValueError('Expected Copy as cURL (bash)')
    url = None
    headers = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ('-H', '--header', '-b', '--cookie', '--url', '-X', '--request'):
            index += 1
            if index == len(tokens):
                raise ValueError('Missing cURL option value')
            val = tokens[index]
            if token in ('-H', '--header'):
                key, sep, content = val.partition(':')
                if not sep:
                    raise ValueError('Invalid header in copied request')
                headers[key.strip().lower()] = content.strip()
            elif token in ('-b', '--cookie'):
                if '=' not in val:
                    raise ValueError('Cookie filenames are not supported')
                headers['cookie'] = val
            elif token == '--url':
                if url is not None:
                    raise ValueError('Expected exactly one request URL')
                url = val
            elif val.upper() != 'GET':
                raise ValueError('Only GET search requests are supported')
        elif token in ('--compressed', '--globoff', '--http2', '--http1.1'):
            pass
        elif token.startswith('https://'):
            if url is not None:
                raise ValueError('Expected exactly one request URL')
            url = token
        else:
            raise ValueError('Unsupported cURL option; copy a GET search request from DevTools')
        index += 1
    if url is None:
        raise ValueError('Missing search URL')
    parsed = urlsplit(url)
    banner = parsed.hostname == 'www.wildberries.ru' and parsed.path == BANNER_PATH
    path = PRODUCT_SEARCH_PATH if banner else parsed.path
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    # Validate the origin before accepting any session headers, including for banners.
    validate_search_endpoint(endpoint)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    allowed = ALLOWED_PARAMS | TRANSIENT_QUERY_PARAMS | (BANNER_ONLY_PARAMS if banner else set())
    if any(key not in allowed for key, _ in pairs):
        raise ValueError('Search has unsupported parameters; use an unfiltered popular search')
    discarded = TRANSIENT_QUERY_PARAMS | (BANNER_ONLY_PARAMS if banner else set())
    params = {key: value for key, value in pairs if key not in discarded}
    if any(key in TRANSIENT_QUERY_PARAMS for key, _ in pairs):
        params['inheritFilters'] = 'false'
    if banner:
        if not headers.get('cookie'):
            raise ValueError('Banner request has no cookies to import as a search session')
        params.update(page='1', limit='100', resultset='catalog', sort='popular',
                      inheritFilters='false')
    if not params.get('query') or params.get('page', '1') != '1':
        raise ValueError('Copy the first page of a search request with a nonempty query')
    try:
        destination = int(params['dest'])
    except (KeyError, ValueError):
        raise ValueError('Search request must include its delivery destination (dest)') from None
    return validate_session({
        'endpoint': endpoint,
        'destination': destination, 'headers': headers, 'params': params,
    })


def load_session(path: Path) -> BrowserSession:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return validate_session(value)
    except (OSError, ValueError):
        # A JSON parse error can include source text containing credentials.
        raise ValueError('Cannot load browser search session; re-import the DevTools request') from None
