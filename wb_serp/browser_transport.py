"""Autonomous WB browser in the container; never opens the user's browser.

Cookies, JS execution and fetch share one Chromium profile and network route.
Normal site challenges may complete in Chromium. Manual checks fail explicitly.
"""
from __future__ import annotations

import base64
import fcntl
import json
import logging
from datetime import date
import os
from pathlib import Path
import time
from urllib.parse import urlparse, urlencode

import httpx

log = logging.getLogger(__name__)

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
        self._rendered_query = None
        self._rendered_destination = None
        self._card_prices = {}
        self._card_snapshot_open = False
        self._renders_cards = False

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

    def _verification_failure(self, request: httpx.Request) -> httpx.Response:
        status = self.diagnostics.get('catalog_status') or self.diagnostics.get('navigation_status')
        if status in (401, 403, 429, 498) or self.diagnostics.get('challenge_visible'):
            return httpx.Response(status if status in (401, 403, 429, 498) else 498,
                                  headers={'server': 'browser-session'},
                                  content=b'WB browser access check failed', request=request)
        raise httpx.ReadTimeout('WB search page did not finish loading; access denial was not confirmed',
                                request=request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        validate_browser_url(str(request.url))
        if request.method != 'GET':
            raise ValueError('Browser search transport only supports GET')
        self._current_query = request.url.params.get('query', '')
        self._start()
        if self._renders_cards and (self._rendered_query != self._current_query
                or (request.url.params.get('page', '1') == '1' and self._card_snapshot_open)):
            self._ready = False
        if not self._ready and not self._warmup():
            return self._verification_failure(request)
        from playwright.sync_api import Error
        if request.url.params.get('page', '1') == '1':
            self._card_prices = {}
            self._card_snapshot_open = True
        self.metrics['fetches'] += 1
        timeouts = request.extensions.get('timeout', {})
        request_timeout = max((v for v in timeouts.values() if v is not None), default=self.timeout)
        params = {**self._search_params, **dict(request.url.params)}
        fetch_url = str(request.url.copy_with(query=urlencode(params).encode()))
        try:
            if self._renders_cards:
                result = self._fetch_frontend_page(request)
            else:
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
        body = base64.b64decode(result['body'])
        if result['status'] == 200:
            body = self._with_delivery(body)
            body = self._with_card_prices(body, request.url.params.get('dest'))
        return httpx.Response(result['status'], headers=headers, content=body, request=request)

    def _with_delivery(self, body: bytes) -> bytes:
        from wb_serp.delivery import calendar_date
        try:
            payload = json.loads(body)
            products = payload.get('data', payload).get('products')
            if not isinstance(products, list) or not products:
                return body
            script = (Path(__file__).parent / 'assets/delivery.js').read_text()
            display = self._page.evaluate(script, products)
            if not isinstance(display, dict) or len(display.get('products', [])) != len(products):
                log.warning('WB delivery display function is unavailable; delivery remains empty')
                return body
            today = date.fromisoformat(display['today'])
            for product, label in zip(products, display['products'], strict=True):
                delivered = calendar_date(label, today)
                if delivered:
                    product['_wb_delivery_date'] = delivered
            return json.dumps(payload, ensure_ascii=False).encode('utf-8')
        except Exception:
            # A changed frontend cannot invalidate otherwise usable ranks and prices.
            log.warning('WB delivery enrichment unavailable; keeping original response')
            return body


    def _with_card_prices(self, body: bytes, destination: str | None) -> bytes:
        if (not self._renders_cards or self._rendered_query != self._current_query
                or str(self._rendered_destination) != destination):
            return body
        from wb_serp.card_prices import displayed_kopecks
        try:
            payload = json.loads(body)
            products = payload.get('data', payload).get('products')
            if not isinstance(products, list) or not products:
                return body
            wanted = {p['id'] for p in products if type(p.get('id')) is int}
            script = (Path(__file__).parent / 'assets/card_prices.js').read_text()
            deadline = time.monotonic() + min(self.timeout, 35)
            stalled, previous = 0, None
            for _ in range(80):
                for card in self._page.evaluate(script):
                    price = displayed_kopecks(card.get('text'))
                    if (price is not None and type(card.get('nm_id')) is int
                            and card.get('kind') in ('wallet', 'displayed')):
                        self._card_prices[card['nm_id']] = {'kopecks':price, 'kind':card['kind']}
                if wanted <= self._card_prices.keys() or time.monotonic() >= deadline:
                    break
                state = self._page.evaluate("() => ({y:scrollY, height:document.documentElement.scrollHeight,\n                    count:document.querySelectorAll('article[data-nm-id]').length})")
                signature = (state['y'], state['height'], state['count'], len(self._card_prices))
                stalled = stalled + 1 if signature == previous else 0
                if stalled >= 10:
                    break
                previous = signature
                self._page.evaluate('window.scrollBy(0, innerHeight * 0.9)')
                self._page.wait_for_timeout(400)
            for product in products:
                observed = self._card_prices.get(product.get('id'))
                if observed is not None:
                    product['_wb_card_price'] = observed
            found = sum('_wb_card_price' in p for p in products)
            log.info('Read displayed WB card prices: %d/%d', found, len(products))
            return json.dumps(payload, ensure_ascii=False).encode('utf-8')
        except Exception:
            log.warning('WB card prices could not be read; leaving prices empty')
            return body

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
