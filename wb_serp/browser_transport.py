"""Autonomous WB browser in the container; never opens the user's browser.

Cookies, JS execution and fetch share one Chromium profile and network route.
Normal site challenges may complete in Chromium. Manual checks fail explicitly.
"""
from __future__ import annotations

import base64
import fcntl
import os
from pathlib import Path
import time
from urllib.parse import urlparse, urlencode

import httpx

ORIGIN = 'https://www.wildberries.ru'
DECODED_HEADERS = {'content-encoding', 'content-length', 'transfer-encoding'}


def validate_browser_url(url: str) -> None:
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'www.wildberries.ru'
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or not parsed.path.startswith('/__internal/u-search/exactmatch/')
            or not parsed.path.endswith('/search')):
        raise ValueError('Browser transport requires the same-origin WB product search endpoint')


class BrowserTransport(httpx.BaseTransport):
    def __init__(self, *, profile_path: Path, timeout: float = 45, headless: bool = True) -> None:
        self.profile_path = profile_path
        self.timeout = timeout
        self.headless = headless
        self._playwright = self._context = self._page = self._lock = None
        self._ready = False
        self._headers = {'accept': 'application/json', 'x-requested-with': 'XMLHttpRequest'}
        self.metrics = {'navigations': 0, 'fetches': 0, 'refreshes': 0}
        self.diagnostics = {'navigation_status': None, 'search_input_found': False}
        self._search_params = {}
        self._current_query = ''

    def _capture_headers(self, request) -> None:
        if request.url.startswith(ORIGIN + '/__internal/u-search/exactmatch/'):
            for name in ('deviceid', 'x-spa-version', 'x-userid'):
                value = request.headers.get(name)
                if value:
                    self._headers[name] = value

    def _start(self) -> None:
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright
        self._acquire_profile()
        try:
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_path), channel='chromium', headless=self.headless,
                locale='ru-RU', timezone_id='Europe/Moscow',
                viewport={'width': 1440, 'height': 900}, timeout=self.timeout * 1000,
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            for page in self._context.pages[1:]:
                page.close()
            self._page.on('request', self._capture_headers)
            self._page.set_default_timeout(self.timeout * 1000)
        except Exception:
            self.close()
            raise httpx.TransportError('Could not start container Chromium; check browser installation') from None

    def _acquire_profile(self) -> None:
        self.profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.profile_path, 0o700)
        self._lock = (self.profile_path / '.collector-browser.lock').open('a+b')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            self._lock = None
            raise httpx.TransportError('Browser profile is in use by another collector') from None
        # Chromium's hostname/PID symlinks can survive a stopped Docker container.
        # Our shared-volume advisory lock has already excluded another app browser.
        for name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
            path = self.profile_path / name
            if path.is_symlink():
                path.unlink()

    def _warmup(self) -> bool:
        from playwright.sync_api import Error
        self.metrics['navigations'] += 1
        deadline = time.monotonic() + self.timeout
        try:
            response = self._page.goto(ORIGIN, wait_until='domcontentloaded',
                                       timeout=self.timeout * 1000)
            self.diagnostics['navigation_status'] = response.status if response else None
            # A resolved site must show a search input; a challenge HTML page is insufficient.
            self._page.locator('#searchInput').wait_for(
                state='attached', timeout=max(1, (deadline - time.monotonic()) * 1000))
            self._ready = True
        except Error:
            self._ready = False
        self.diagnostics['search_input_found'] = self._ready
        return self._ready

    def refresh_session(self) -> bool:
        if not self._ready:
            # Bootstrap already waited for the site's JS; do not reset its countdown.
            return False
        self.metrics['refreshes'] += 1
        return self._warmup()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        validate_browser_url(str(request.url))
        if request.method != 'GET':
            raise ValueError('Browser search transport only supports GET')
        self._current_query = request.url.params.get('query', '')
        self._start()
        if not self._ready and not self._warmup():
            return httpx.Response(498, headers={'server': 'browser-session'},
                                  content=b'Browser site verification did not finish', request=request)
        from playwright.sync_api import Error
        self.metrics['fetches'] += 1
        timeouts = request.extensions.get('timeout', {})
        request_timeout = max((v for v in timeouts.values() if v is not None), default=self.timeout)
        params = {**self._search_params, **dict(request.url.params)}
        fetch_url = str(request.url.copy_with(query=urlencode(params).encode()))
        try:
            result = self._page.evaluate('''async ({url, headers, timeout, pageUrl}) => {
                history.replaceState(history.state, '', pageUrl);
                const r = await fetch(url, {credentials: 'include', headers,
                    redirect: 'error', signal: AbortSignal.timeout(timeout)});
                const bytes = new Uint8Array(await r.arrayBuffer());
                let binary = '';
                for (let i = 0; i < bytes.length; i += 8192)
                    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
                return {status: r.status, headers: [...r.headers], body: btoa(binary)};
            }''', {'url': fetch_url, 'headers': self._headers,
                   'pageUrl': ORIGIN + '/catalog/0/search.aspx?' + urlencode({'search': self._current_query}),
                   'timeout': int(request_timeout * 1000)})
        except Error:
            raise httpx.TransportError('WB browser fetch failed or timed out', request=request) from None
        # Browser fetch has already decoded gzip/br/zstd, like curl_cffi.
        headers = [(k, v) for k, v in result['headers'] if k.lower() not in DECODED_HEADERS]
        return httpx.Response(result['status'], headers=headers,
                              content=base64.b64decode(result['body']), request=request)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = self._page = None
            self._ready = False
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self._playwright = None
                if self._lock is not None:
                    self._lock.close()
                    self._lock = None
