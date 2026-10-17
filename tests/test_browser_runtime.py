"""Real Chromium integration; run this file inside the application image.

All site responses are intercepted fixtures. No external WB request is sent.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

import httpx

from scripts.probe_live_wb import probe
from wb_serp.browser_transport import BrowserTransport
from wb_serp.client import WildberriesClient

ENDPOINT = 'https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search'


@unittest.skipUnless(os.environ.get('WB_TEST_CONTAINER_BROWSER') == '1',
                     'Run in application image with WB_TEST_CONTAINER_BROWSER=1')
class BrowserRuntimeTest(unittest.TestCase):
    def test_real_chromium_fetches_top200_and_rotates_cookies(self):
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            transport = BrowserTransport(profile_path=Path(folder) / 'profile', timeout=5)
            try:
                transport._start()

                def fixture(route):
                    req = route.request
                    if '/__internal/u-search/exactmatch/' not in req.url:
                        route.fulfill(status=200, content_type='text/html', body=(
                            '<input id="searchInput">'
                            '<script>document.cookie="runtime_token=one;path=/"</script>'))
                        return
                    page = int(parse_qs(urlparse(req.url).query)['page'][0])
                    query = parse_qs(urlparse(req.url).query)['query']
                    self.assertEqual(parse_qs(urlparse(req.headers['referer']).query)['search'], query)
                    seen.append(page)
                    self.assertIn('runtime_token=', req.headers.get('cookie', ''))
                    if page == 1:
                        transport._page.evaluate('document.cookie="runtime_token=two;path=/"')
                    else:
                        self.assertIn('runtime_token=two', req.headers.get('cookie', ''))
                    route.fulfill(status=200, content_type='application/json', body=json.dumps({
                        'data': {'products': [{'id': 100 * (page - 1) + i + 1, 'name': 'Fixture'}
                                              for i in range(100)]}}))

                transport._context.route('**/*', fixture)
                with httpx.Client(transport=transport) as http:
                    client = WildberriesClient(search_url=ENDPOINT, destination=1, client=http,
                                               delay_seconds=0, retries=1)
                    result = probe(client, query='fixture only', top_n=200)
                    self.assertEqual(result['unique_nm_ids'], 200)
                    self.assertEqual(result['page_sizes'], [100, 100])
                    client.page('different phrase', 1)
                self.assertEqual(seen, [1, 2, 1])
            finally:
                transport.close()


    def test_reads_current_card_price_not_crossed_out_price(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = BrowserTransport(profile_path=Path(folder)/'cards', timeout=5)
            try:
                transport._start()
                transport._page.set_content('<article data-nm-id="7"><div class="price--fixture">\n                    <ins data-testid="product-card-current-price">1&nbsp;982&nbsp;₽</ins>\n                    <del>9&nbsp;372&nbsp;₽</del><span>с WB Кошельком</span></div></article>\n                    <article data-nm-id="3"><div class="price--fixture">\n                    <ins data-testid="product-card-current-price">100,50 ₽</ins></div></article>')
                transport._renders_cards = True
                transport._current_query = transport._rendered_query = 'fixture'
                transport._rendered_destination = '99'
                body = json.dumps({'products':[{'id':3},{'id':7}]}).encode()
                observed = json.loads(transport._with_card_prices(body, '99'))['products']
                self.assertEqual(observed[0]['_wb_card_price'], {'kopecks':10050,'kind':'displayed'})
                self.assertEqual(observed[1]['_wb_card_price'], {'kopecks':198200,'kind':'wallet'})
            finally:
                transport.close()


if __name__ == '__main__':
    unittest.main()
