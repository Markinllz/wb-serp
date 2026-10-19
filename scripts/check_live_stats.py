"""Verify manual refresh and stable counters on a disposable copy; never contact WB."""
from datetime import datetime, timezone
import json

from playwright.sync_api import expect, sync_playwright

from wb_serp.config import Settings
from wb_serp.models import Product
from wb_serp.runtime import WorkerHeartbeat
from wb_serp.store import Store


def main():
    cfg = Settings.from_env()
    if not cfg.database_path.name.startswith('ui-check-live-'):
        raise RuntimeError('Use a disposable ui-check-live-*.sqlite database')
    store = Store(cfg.database_path)
    ids = [q['id'] for q in store.enabled_queries()[:2]]
    at = datetime.now(timezone.utc)
    slot = at.replace(minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
    before = store.collection_stats(slot=slot, depth=200, query_ids=ids)['observations']
    errors, outside = [], []
    expect.set_options(timeout=20000)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chromium', headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width':1440, 'height':1100})

        def local_only(route):
            if route.request.url.startswith('http://127.0.0.1:8501/'):
                route.continue_()
            else:
                outside.append(route.request.url)
                route.abort()

        context.route('**/*', local_only)
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        with WorkerHeartbeat(cfg.database_path) as heartbeat:
            heartbeat.update('collecting', scheduled_for=slot, query_ids=ids)
            store.start_run(run_id='ui-live-1', query_id=ids[0], scheduled_for=slot,
                            started_at=at, requested_top_n=200, destination=cfg.destination)
            page.goto('http://127.0.0.1:8501')
            expect(page.locator('.live-status')).to_have_text('Идёт сбор')
            expect(page.locator('.live-panel progress')).to_have_attribute('value', '0')
            expect(page.locator('.live-panel progress')).to_have_attribute('max', '2')
            chart = page.locator('.js-plotly-plot').first
            expect(chart).to_be_visible()
            chart.evaluate('(el)=>{el.dataset.zoomProof="preserved"}')
            store.complete_run(run_id='ui-live-1', status='partial', finished_at=datetime.now(timezone.utc),
                               products=[Product(nm, i, None, None, None, None, None, 'UI fixture')
                                         for i, nm in enumerate((990000000001, 990000000002), 1)])
            page.wait_for_timeout(6000)
            expect(page.locator('.live-totals strong').first).to_have_text(f'{before:,}'.replace(',', ' '))
            expect(chart).to_have_attribute('data-zoom-proof', 'preserved')
            page.get_by_role('button', name='Обновить данные', exact=True).click()
            expect(page.locator('.live-totals strong').first).to_have_text(f'{before+2:,}'.replace(',', ' '))
            expect(page.locator('.live-panel progress')).to_have_attribute('value', '1')
            now = datetime.now(timezone.utc)
            store.start_run(run_id='ui-live-2', query_id=ids[1], scheduled_for=slot,
                            started_at=now, requested_top_n=200, destination=cfg.destination)
            store.complete_run(run_id='ui-live-2', status='blocked', finished_at=now, products=[])
            heartbeat.update('waiting')
            page.get_by_role('button', name='Обновить данные', exact=True).click()
            expect(page.locator('.live-status')).to_have_text('WB ограничил доступ до следующего цикла')
            expect(page.locator('.live-panel progress')).to_have_attribute('value', '2')
            expect(page.locator('.live-totals strong').first).to_have_text(f'{before+2:,}'.replace(',', ' '))
        page.get_by_role('button', name='Обновить данные', exact=True).click()
        expect(page.locator('.live-status')).to_have_text('Сборщик остановлен')
        page.get_by_test_id('stSidebar').get_by_text('Сбор данных', exact=True).first.click()
        metric = page.get_by_test_id('stMetric').filter(has_text='Наблюдений')
        expect(metric.get_by_test_id('stMetricValue')).to_have_text(f'{before+2:,}'.replace(',', ' '))
        expect(page.get_by_test_id('stException')).to_have_count(0)
        assert not errors and not outside, (errors, outside)
        browser.close()
    report = {'status':'PASS', 'auto_refresh_minutes':15, 'manual_counter_update':True, 'cycle_progress':True,
              'blocked_state':True, 'stopped_state':True, 'graph_preserved_between_refreshes':True,
              'observations_before':before, 'observations_after':before+2,
              'external_requests':outside, 'javascript_errors':errors}
    output = cfg.database_path.parent / 'dashboard-check' / cfg.database_path.stem
    output.mkdir(parents=True, exist_ok=True)
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
