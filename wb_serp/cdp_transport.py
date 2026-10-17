"""Optional SeleniumBase CDP launcher, isolated inside the browser Docker image."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from wb_serp.browser_session import ALLOWED_PARAMS
from wb_serp.browser_transport import BrowserTransport, ORIGIN, validate_browser_url

log = logging.getLogger(__name__)


class CdpBrowserTransport(BrowserTransport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._sb = self._display = None
        self._bootstrap_ready = False
        self._frontend_ready = False
        self._frontend_headers = {}
        self._frontend_pages = {}
        self._frontend_destination = None
        self._renders_cards = True

    def _start(self) -> None:
        if self._context is not None:
            return
        self._acquire_profile()
        try:
            from sbvirtualdisplay import Display
            from seleniumbase import sb_cdp
            from playwright.sync_api import sync_playwright
            executable = next(Path('/ms-playwright').glob('chromium-*/chrome-linux*/chrome'))
            # This X server and authority file are container-local, never the user's desktop.
            (Path.home() / '.Xauthority').touch(mode=0o600, exist_ok=True)
            self._display = Display(visible=False, size=(1440, 900))
            self._display.start()
            self._sb = sb_cdp.Chrome(
                browser_executable_path=str(executable), user_data_dir=str(self.profile_path),
                headless=False, sandbox=False, lang='ru-RU',
            )
            deadline = time.monotonic() + self.timeout
            self._sb.loop.run_until_complete(asyncio.wait_for(
                self._sb.page.get(ORIGIN), timeout=self.timeout))
            while time.monotonic() < deadline:
                if self._sb.evaluate("Boolean(document.querySelector('#searchInput'))"):
                    self._bootstrap_ready = True
                    break
                time.sleep(0.5)
            endpoint = self._sb.get_endpoint_url()
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(endpoint)
            self._context = self._browser.contexts[0]
            self._page = next(p for p in self._context.pages if p.url.startswith(ORIGIN))
            self._context.new_cdp_session(self._page).send(
                'Emulation.setTimezoneOverride', {'timezoneId': 'Europe/Moscow'})
            self._page.on('request', self._capture_headers)
            self._page.on('response', self._capture_product_response)
            self._page.set_default_timeout(self.timeout * 1000)
        except ImportError:
            self.close()
            raise ValueError('CDP dependencies missing; build the browser Docker target') from None
        except Exception as exc:
            log.debug('CDP startup failed', exc_info=True)
            self.close()
            raise httpx.TransportError(f'Could not start isolated CDP browser ({type(exc).__name__})') from None

    def _capture_product_response(self, response) -> None:
        try:
            url = urlsplit(response.url)
            validate_browser_url(url._replace(query='', fragment='').geturl())
            params = dict(parse_qsl(url.query))
            if (response.status != 200 or params.get('resultset') != 'catalog'
                    or params.get('query') != self._current_query):
                return
            payload = response.json()
            products = payload.get('products', payload.get('data', {}).get('products'))
            if not isinstance(products, list):
                return
            self._search_params = {k: v for k, v in params.items() if k in ALLOWED_PARAMS
                                   and k not in ('query', 'page', 'dest', 'inheritFilters')}
            self._frontend_headers = {
                k: v for k, v in response.request.headers.items()
                if k in ('accept', 'deviceid', 'x-spa-version', 'x-userid', 'x-requested-with')}
            self._frontend_pages[int(params.get('page', 1))] = payload
            self._frontend_destination = params.get('dest')
            self._frontend_ready = True
        except Exception:
            return

    def _warmup(self) -> bool:
        if not self._bootstrap_ready:
            return False
        from playwright.sync_api import Error
        self.metrics['navigations'] += 1
        self._frontend_ready = False
        self._frontend_destination = None
        self._frontend_pages = {}
        try:
            search_page = ORIGIN + '/catalog/0/search.aspx?' + urlencode({'search': self._current_query})
            self._page.goto(search_page, wait_until='domcontentloaded', timeout=self.timeout * 1000)
            deadline = time.monotonic() + self.timeout
            while not self._frontend_ready and time.monotonic() < deadline:
                self._page.wait_for_timeout(250)
            self._ready = self._frontend_ready
            if self._ready:
                self._headers.update(self._frontend_headers)
                self._rendered_query = self._current_query
                self._rendered_destination = self._frontend_destination
                self._card_prices = {}
                self._card_snapshot_open = False
        except Error:
            self._ready = False
        self.diagnostics.update(search_input_found=self._bootstrap_ready,
                                frontend_search_verified=self._ready)
        return self._ready

    def _fetch_frontend_page(self, request: httpx.Request) -> dict:
        """Use the JSON actually loaded by the rendered search, not a parallel API replay."""
        if str(self._rendered_destination) != request.url.params.get('dest'):
            raise httpx.TransportError('Rendered WB region differs from the fixed dataset region', request=request)
        page = int(request.url.params.get('page', 1))
        deadline = time.monotonic() + min(self.timeout, 35)
        while page not in self._frontend_pages and time.monotonic() < deadline:
            self._page.evaluate('window.scrollBy(0, innerHeight * 0.9)')
            self._page.wait_for_timeout(400)
        payload = self._frontend_pages.get(page)
        if payload is None:
            raise httpx.TransportError(f'WB did not render search page {page}', request=request)
        return {'status':200, 'headers':[('content-type','application/json')],
                'body':base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()}

    def close(self) -> None:
        for resource, method in ((getattr(self, '_browser', None), 'close'),
                                 (self._playwright, 'stop'), (self._sb, 'quit'),
                                 (self._display, 'stop')):
            try:
                if resource is not None:
                    getattr(resource, method)()
            except Exception:
                pass
        self._context = self._page = self._playwright = self._sb = self._display = None
        self._browser = None
        self._ready = False
        if self._lock is not None:
            self._lock.close()
            self._lock = None
