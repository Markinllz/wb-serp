"""Rate-limited and replaceable adapter to an *undocumented* WB search endpoint.

The response contract is NOT guaranteed by Wildberries. We fail explicitly
on schema and blocking errors rather than silently manufacturing search data.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, replace
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
import time
from typing import Callable
from urllib.parse import urlencode

import httpx

from wb_serp.models import SearchPage
from wb_serp.parsing import SchemaError, extract_products
from wb_serp.browser_session import load_session

log = logging.getLogger(__name__)
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
AUTH_COOKIES = {'x_wbaas_token', 'x_guardium_token'}


def retry_after_seconds(value: str | None, now: float) -> float | None:
    """RFC 9110: delay-seconds or an HTTP date; never retry before that time."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        try:
            return float(value)
        except (ValueError, OverflowError):
            return float('inf')
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, date.timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return None


class FetchError(RuntimeError):
    def __init__(self, message: str, *, blocked: bool = False,
                 http_status: int | None = None, response_headers: dict | None = None,
                 cause: str | None = None) -> None:
        super().__init__(message)
        self.blocked = blocked
        self.http_status = http_status
        self.response_headers = response_headers or {}
        self.cause = cause


class WildberriesClient:
    def __init__(
        self,
        *,
        search_url: str,
        destination: int,
        timeout: float = 20,
        retries: int = 3,
        delay_seconds: float = 5,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        http_transport: str = 'httpx',
        session_file: Path | None = None,
        retry_base_seconds: float = 5,
        retry_max_seconds: float = 60,
        retry_jitter_seconds: float = 1,
        wall_clock: Callable[[], float] = time.time,
        random_source: Callable[[], float] = random.random,
        browser_profile_path: Path = Path('/app/data/browser-profile'),
        browser_timeout_seconds: float = 45,
        browser_engine: str = 'playwright',
    ) -> None:
        values = (delay_seconds, retry_base_seconds, retry_max_seconds, retry_jitter_seconds)
        if not 1 <= retries <= 8 or any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("Invalid retry/delay configuration")
        if retry_max_seconds < retry_base_seconds:
            raise ValueError('Retry maximum must be at least the base delay')
        if http_transport not in ('httpx', 'curl_cffi', 'browser'):
            raise ValueError('WB_HTTP_TRANSPORT must be httpx, curl_cffi or browser')
        if http_transport == 'browser':
            if browser_engine not in ('playwright', 'cdp'):
                raise ValueError('WB_BROWSER_ENGINE must be playwright or cdp')
            from wb_serp.browser_transport import validate_browser_url
            validate_browser_url(search_url)
            if session_file is not None:
                raise ValueError('Browser mode uses its own profile; unset WB_SESSION_FILE')
        headers = {
            'user-agent': 'WBSerpResearch/1.0 (+historical-ranking-monitor)',
            'accept': 'application/json', 'referer': 'https://www.wildberries.ru/',
        }
        self._session_params: dict[str, str] = {}
        self._session_file = session_file
        self._session_cookies: SimpleCookie | None = None
        self._persisted_cookie = ''
        if session_file is not None:
            session = load_session(session_file)
            if search_url != session.endpoint:
                raise ValueError('WB_SEARCH_URL differs from the imported session endpoint')
            if destination != session.destination:
                raise ValueError('WB_DESTINATION differs from the imported browser region')
            headers.update(session.headers)
            self._session_params = session.params
            self._persisted_cookie = headers.pop('cookie', '')
            self._session_cookies = SimpleCookie()
            self._session_cookies.load(self._persisted_cookie)
        transport = None
        if client is None and http_transport == 'curl_cffi':
            from wb_serp.curl_transport import CurlTransport
            transport = CurlTransport(user_agent=headers.get('user-agent', ''))
        if client is None and http_transport == 'browser':
            from wb_serp.browser_transport import BrowserTransport
            launcher = BrowserTransport
            if browser_engine == 'cdp':
                from wb_serp.cdp_transport import CdpBrowserTransport
                launcher = CdpBrowserTransport
            transport = launcher(profile_path=browser_profile_path, timeout=browser_timeout_seconds)
        self._browser_transport = transport if http_transport == 'browser' else None
        self.search_url = search_url
        self.destination = destination
        self.retries = retries
        self._delay_seconds = delay_seconds
        self._clock = clock
        self._last_request: float | None = None
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers=headers, transport=transport,
        )
        self._owns_client = client is None
        if client is not None:
            self._client.headers.update(headers)
        if self._session_cookies is not None:
            self._client.headers.pop('cookie', None)
        self._sleep = sleeper
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._retry_jitter = retry_jitter_seconds
        self._wall_clock = wall_clock
        self._random = random_source
        self.metrics = {'requests': 0, 'retries': 0, 'session_refreshes': 0}

    def __enter__(self) -> WildberriesClient:
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def _pace(self) -> None:
        if self._last_request is not None:
            remaining = self._delay_seconds - (self._clock() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._clock()

    def _cookie_header(self) -> str:
        return '; '.join(m.OutputString(attrs=[]) for m in self._session_cookies.values())

    def _refresh_cookies(self, response: httpx.Response) -> bool:
        if self._session_cookies is None:
            return False
        host = response.request.url.host
        path = response.request.url.path
        auth_rotated = False
        for header in response.headers.get_list('set-cookie'):
            cookies = SimpleCookie()
            try:
                cookies.load(header)
            except Exception:
                continue
            for name, morsel in cookies.items():
                domain = morsel['domain'].lstrip('.').lower()
                cookie_path = morsel['path'] or '/'
                if domain and host != domain and not host.endswith('.' + domain):
                    continue
                if not (path == cookie_path or (path.startswith(cookie_path)
                        and (cookie_path.endswith('/') or path[len(cookie_path):].startswith('/')))):
                    continue
                if morsel['secure'] and response.request.url.scheme != 'https':
                    continue
                expired = False
                if morsel['max-age']:
                    try:
                        expired = int(morsel['max-age']) <= 0
                    except ValueError:
                        continue
                elif morsel['expires']:
                    expired = retry_after_seconds(morsel['expires'], self._wall_clock()) == 0
                previous = self._session_cookies.get(name)
                previous_value = previous.value if previous is not None else None
                if expired:
                    self._session_cookies.pop(name, None)
                else:
                    self._session_cookies[name] = morsel.value
                    if name in AUTH_COOKIES and morsel.value and (
                            previous_value != morsel.value):
                        auth_rotated = True
        return auth_rotated

    def _persist_cookies(self) -> None:
        if self._session_file is None or self._session_cookies is None:
            return
        header = self._cookie_header()
        if header == self._persisted_cookie:
            return
        temp_name = None
        try:
            current = load_session(self._session_file)
            if current.headers.get('cookie', '') != self._persisted_cookie:
                # A manual import takes precedence over an older running client.
                return
            updated = replace(current, headers={**current.headers, 'cookie': header})
            fd, temp_name = tempfile.mkstemp(prefix='.session-', dir=self._session_file.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(asdict(updated), stream, ensure_ascii=False)
            os.replace(temp_name, self._session_file)
            self._persisted_cookie = header
        except (OSError, ValueError):
            log.warning('Could not persist refreshed browser session; credentials omitted')
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float | None:
        server_delay = retry_after_seconds(response.headers.get('retry-after'), self._wall_clock()) \
            if response is not None else None
        if server_delay is not None and server_delay > self._retry_max:
            return None
        backoff = min(self._retry_base * 2 ** attempt, self._retry_max)
        jitter = max(0.0, min(1.0, self._random())) * self._retry_jitter
        return min(self._retry_max, max(server_delay or 0, backoff + jitter))

    def _wait_retry(self, delay: float, reason: str) -> None:
        self.metrics['retries'] += 1
        log.warning('Transient WB error (%s); next attempt after %.1fs', reason, delay)
        self._sleep(delay)

    def page(self, query: str, page_number: int) -> SearchPage:
        if page_number < 1:
            raise ValueError("page_number must be positive")
        params = {
            "appType": 1,
            "curr": "rub",
            "dest": self.destination,
            "lang": "ru",
            "page": page_number,
            "query": query,  # HTTPX URL-encodes while preserving the original phrase.
            "resultset": "catalog",
            "sort": "popular",
            "spp": 30,
        }
        params.update(self._session_params)
        params.update(query=query, page=page_number, dest=self.destination,
                      curr='rub', sort='popular')
        if self._browser_transport is not None:
            params.update(inheritFilters='false', limit=100)
        session_retry_used = False
        for attempt in range(self.retries):
            self._pace()
            try:
                request_headers = {}
                if self._session_params:
                    request_headers = {'referer': 'https://www.wildberries.ru/catalog/0/search.aspx?'
                                       + urlencode({'search': query})}
                if self._session_cookies is not None:
                    request_headers['cookie'] = self._cookie_header()
                self.metrics['requests'] += 1
                try:
                    response = self._client.get(self.search_url, params=params, headers=request_headers)
                finally:
                    # Browser bootstrap may take longer than the pacing interval.
                    # Count the pause from completion, not from before that bootstrap.
                    self._last_request = self._clock()
                auth_rotated = self._refresh_cookies(response)
                challenge = (response.content.lstrip().startswith(b'<')
                             and b'/__wbaas/challenges/' in response.content[:16384])
                if response.status_code in (401, 403, 429, 498) or challenge:
                    # Stop on denial; diagnostics never include cookies or response bodies.
                    safe_headers = {
                        key: value for key in ('server', 'status-no-id', 'x-request-id', 'retry-after')
                        if (value := response.headers.get(key))
                        and re.fullmatch(r'[A-Za-z0-9 .,:_+/-]{1,128}', value)
                    }
                    if challenge or response.status_code == 498:
                        cause = 'browser_challenge'
                    elif response.status_code == 429:
                        cause = 'rate_limit'
                    else:
                        cause = 'access_denied'
                    if attempt + 1 < self.retries:
                        delay = self._retry_delay(attempt, response)
                        if response.status_code == 429 and delay is not None:
                            self._delay_seconds = min(max(self._delay_seconds * 2, self._retry_base),
                                                      self._retry_max)
                            self._wait_retry(delay, 'HTTP 429')
                            continue
                        if auth_rotated and not session_retry_used and delay is not None:
                            # Only a new server-issued auth cookie permits one session retry.
                            session_retry_used = True
                            self.metrics['session_refreshes'] += 1
                            self._wait_retry(delay, 'server refreshed session')
                            continue
                        if (self._browser_transport is not None and not session_retry_used
                                and cause == 'browser_challenge' and delay is not None):
                            session_retry_used = True
                            if self._browser_transport.refresh_session():
                                self.metrics['session_refreshes'] += 1
                                self._wait_retry(delay, 'container browser refreshed session')
                                continue
                    details = ', '.join(f'{k}={v}' for k, v in safe_headers.items())
                    hint = ('; container browser could not complete WB verification'
                            if self._browser_transport is not None else
                            '; import a fresh successful browser search request'
                            if cause == 'browser_challenge' or safe_headers.get('server') == 'wbaas'
                            else '')
                    raise FetchError(
                        f'WB denied/rate-limited request: HTTP {response.status_code}'
                        f' ({details}){hint}', blocked=True, http_status=response.status_code,
                        response_headers=safe_headers, cause=cause,
                    )
                if response.status_code in RETRYABLE_HTTP and attempt + 1 < self.retries:
                    delay = self._retry_delay(attempt, response)
                    if delay is None:
                        raise FetchError('WB Retry-After exceeds the bounded retry budget',
                                         blocked=True, http_status=response.status_code,
                                         cause='retry_after_exceeds_budget')
                    self._wait_retry(delay, f'HTTP {response.status_code}')
                    continue
                response.raise_for_status()
                try:
                    payload = response.json()
                    products = extract_products(payload)
                except (ValueError, SchemaError) as exc:
                    raise FetchError(f"Search response schema error: {exc}") from exc
                self._persist_cookies()
                nested = payload.get('data')
                total = payload.get('total', nested.get('total') if isinstance(nested, dict) else None)
                if type(total) is not int or total < 0:
                    total = None
                return SearchPage(products=products, page_number=page_number, total=total)
            except httpx.DecodingError:
                raise FetchError('WB response decompression failed') from None
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in RETRYABLE_HTTP:
                    raise FetchError(f"WB permanent HTTP error: {exc.response.status_code}") from exc
                if attempt + 1 == self.retries:
                    raise FetchError(f"WB request failed after {self.retries} attempts: {exc}") from exc
                self._wait_retry(self._retry_delay(attempt), type(exc).__name__)
        raise AssertionError("Unreachable")
