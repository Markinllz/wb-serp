"""Check the running dashboard in Chromium against its SQLite data.

Run inside the dashboard container after stopping the collector. Saves screenshots,
CSV downloads and a JSON report; does not modify the database or contact WB.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright

from wb_serp.config import Settings
from wb_serp.store import Store

MSK = ZoneInfo('Europe/Moscow')


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding='utf-8-sig', newline='') as source:
        return list(csv.DictReader(source))


def database_counts(store: Store) -> dict:
    with store.connect() as db:
        return {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('collection_runs', 'observations', 'product_catalog')}


def check(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    cfg = Settings.from_env()
    store = Store(cfg.database_path, readonly=True)
    queries = store.queries_dashboard()
    complete = next((q for q in queries if q['last_success']), None)
    if complete is None:
        raise RuntimeError('At least one successful live snapshot is required')
    expected = store.latest_observations(complete['id'])
    before = database_counts(store)
    outside_requests: list[str] = []
    errors: list[str] = []
    report = {'started_at': datetime.now(timezone.utc).isoformat(),
              'query': complete['phrase'], 'snapshot_rows': len(expected)}

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel='chromium',
                                          args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1366, 'height': 1050},
                                      accept_downloads=True)

        def local_only(route):
            if urlsplit(route.request.url).hostname in ('127.0.0.1', 'localhost'):
                route.continue_()
            else:
                outside_requests.append(route.request.url)
                route.abort()

        context.route('**/*', local_only)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def settled():
            expect(page.get_by_test_id('stApp')).to_have_attribute('data-test-script-state', 'notRunning')
            expect(page.locator('[data-stale="true"]')).to_have_count(0)
        page.goto('http://127.0.0.1:8501', wait_until='domcontentloaded')
        chooser = page.get_by_role('combobox', name='Поисковый запрос')
        expect(chooser).to_be_visible(timeout=30000)

        def select_query(phrase: str) -> None:
            chooser.fill(phrase)
            page.get_by_role('option', name=phrase, exact=True).click()
            expect(page.get_by_role('heading', name=phrase, exact=True)).to_be_visible()
            expect(page.locator('[data-testid="stApp"]')).to_have_attribute('data-test-script-state', 'notRunning')

        select_query(complete['phrase'])
        expect(page.get_by_role('button', name='Скачать историю CSV')).to_be_visible()
        expect(page.locator('[data-testid="stException"]')).to_have_count(0)
        plot = page.locator('.js-plotly-plot').first
        expect(plot).to_be_visible()
        graph = plot.evaluate('(p) => ({range:p._fullLayout.yaxis.range, '
                              'curves:p.data.length, gaps:p.data.map(t=>t.connectgaps)})')
        assert graph['range'][0] > graph['range'][1], graph
        assert graph['curves'] == min(3, len(expected)), graph
        assert not any(graph['gaps']), graph
        selected_ids = [row['nm_id'] for row in expected[:3]]
        current_ids = {row['nm_id'] for row in expected}
        missing = next((row for row in store.known_products(complete['id'])
                        if row['nm_id'] not in current_ids), None)
        if missing:
            selector = page.get_by_test_id('stExpander').filter(has_text='Выбрать товары')
            selector.locator('summary').click()
            page.get_by_role('textbox', name='Название или артикул').fill(str(missing['nm_id']))
            page.get_by_role('button', name='Найти', exact=True).click()
            expect(page.get_by_text('Найдено товаров: 1', exact=True)).to_be_visible()
            page.get_by_role('button', name='Добавить на график', exact=True).click()
            page.get_by_role('heading', name=complete['phrase'], exact=True).click()
            page.wait_for_function('document.querySelector(".js-plotly-plot")?.data?.length === 4')
            selected_ids.append(missing['nm_id'])
            report['historical_product_absent_from_latest'] = missing['nm_id']
            missing_trace = plot.evaluate('''(p, id) => {
                const trace = p.data.find(t => t.meta.nm_id === id);
                return {positions: Array.from(trace.y), connectgaps: trace.connectgaps};
            }''', missing['nm_id'])
            assert None in missing_trace['positions'] and not missing_trace['connectgaps']
            report['historical_product_trace'] = missing_trace
            # A second search must replace its old result and keep existing lines.
            extra_id = expected[3]['nm_id']
            page.get_by_role('textbox', name='Название или артикул').fill(str(extra_id))
            page.get_by_role('button', name='Найти', exact=True).click()
            expect(page.get_by_text('Найдено товаров: 1', exact=True)).to_be_visible()
            page.get_by_role('button', name='Добавить на график', exact=True).click()
            page.wait_for_function('document.querySelector(".js-plotly-plot")?.data?.length === 5')
            ids = plot.evaluate('(p) => p.data.map(t=>t.meta.nm_id)')
            assert set(ids) == {*selected_ids, extra_id}
            selection = page.get_by_role('combobox', name='На графике')
            selection.focus()
            selection.press('Backspace')
            selection.press('Escape')
            page.wait_for_function('document.querySelector(".js-plotly-plot")?.data?.length === 4')
            assert plot.evaluate('(p) => p.data.map(t=>t.meta.nm_id)') == selected_ids
            report['repeated_search_add_remove'] = 'PASS'
            selector.locator('summary').click()
        settled()
        page.screenshot(path=str(output / 'history-desktop.png'), full_page=True, animations='disabled')

        with page.expect_download() as download:
            page.get_by_role('button', name='Скачать историю CSV').click()
        history_path = output / 'rank_history.csv'
        download.value.save_as(history_path)
        history = read_csv(history_path)
        today = datetime.now(MSK).date()
        since = datetime.combine(today - timedelta(days=14), datetime.min.time(), tzinfo=MSK)
        until = datetime.combine(today, datetime.max.time(), tzinfo=MSK)
        expected_history = store.rank_history(complete['id'], selected_ids,
                                              since=since.astimezone(timezone.utc).isoformat(),
                                              until=until.astimezone(timezone.utc).isoformat(),
                                              depth=expected[0]['requested_top_n'], include_partial=True)
        actual_positions = [(r['run_id'], int(r['nm_id']), int(r['position'])
                             if r['position'] else None) for r in history]
        assert actual_positions == [(r['run_id'], r['nm_id'], r['position'])
                                    for r in expected_history]
        assert history, 'History export is empty'
        if missing:
            assert any(r['nm_id'] == missing['nm_id'] and r['position'] is None
                       for r in expected_history)
        report['history_rows'] = len(history)
        report['history_gaps'] = sum(r['position'] is None for r in expected_history)
        assert [int(r['price_kopecks']) if r['price_kopecks'] else None for r in history] == [
            r['price_kopecks'] for r in expected_history]
        page.get_by_test_id('stRadio').get_by_text('Цены', exact=True).click()
        page.wait_for_function('document.querySelector(".js-plotly-plot").layout.yaxis.title.text === "Цена, ₽"')
        price_traces = plot.evaluate('(p) => p.data.map(t => ({id:String(t.meta.nm_id), values:Array.from(t.y), gaps:t.connectgaps}))')
        for trace in price_traces:
            source = [r for r in expected_history if str(r['nm_id']) == trace['id']]
            assert trace['values'] == [r['price_kopecks'] / 100 if r['price_kopecks'] is not None
                                        else None for r in source]
            assert trace['gaps'] is False
        settled()
        page.screenshot(path=str(output / 'prices-desktop.png'), full_page=True, animations='disabled')
        report['price_history'] = {'status': 'PASS', 'products': len(price_traces)}
        page.get_by_test_id('stRadio').get_by_text('Позиции', exact=True).click()
        page.wait_for_function('document.querySelector(".js-plotly-plot").layout.yaxis.title.text === "Позиция"')
        changes = page.get_by_test_id('stExpander').filter(has_text='Изменения выдачи')
        comparison = store.compare_latest_snapshots(complete['id'], depth=expected[0]['requested_top_n'],
                      since=since.astimezone(timezone.utc).isoformat(),
                      until=until.astimezone(timezone.utc).isoformat())
        if comparison:
            changes.locator('summary').click()
            with page.expect_download() as download:
                page.get_by_role('button', name='Скачать сравнение CSV').click()
            changes_path = output / 'rank_changes.csv'
            download.value.save_as(changes_path)
            exported = read_csv(changes_path)
            actual = {int(r['Артикул']): r for r in exported}
            assert set(actual) == {r['nm_id'] for r in comparison['products']}
            for source in comparison['products']:
                row = actual[source['nm_id']]
                for label, key in [('Было', 'before'), ('Стало', 'after'), ('Изменение', 'delta')]:
                    assert (int(row[label]) if row[label] else None) == source[key]
                assert row['Снимок_до'] == comparison['before']['started_at']
                assert row['Снимок_после'] == comparison['after']['started_at']
            changed = [r for r in comparison['products'] if r['delta'] not in (None, 0)]
            if changed:
                bars = changes.locator('.js-plotly-plot')
                expect(bars).to_be_visible()
                plotted = bars.evaluate('(p) => ({x:Array.from(p.data[0].x), ids:p.data[0].customdata.map(r=>r[0])})')
                by_id = {r['nm_id']: r for r in comparison['products']}
                assert len(plotted['ids']) == min(12, len(changed))
                assert all(delta == by_id[int(nm)]['delta'] for nm, delta in
                           zip(plotted['ids'], plotted['x'], strict=True))
                axis_range = bars.evaluate('(p) => Array.from(p._fullLayout.yaxis.range)')
                assert -1 <= min(axis_range) and max(axis_range) <= len(plotted['ids']) + 1
                bars.evaluate("(el) => el.scrollIntoView({block: 'center', behavior: 'instant'})")
                page.wait_for_timeout(300)
                bars.screenshot(path=str(output / 'changes-chart.png'), animations='disabled')
            report['snapshot_comparison'] = {'status': 'PASS', 'rows': len(exported),
                       'entered': sum(r['change'] == 'entered' for r in comparison['products']),
                       'left': sum(r['change'] == 'left' for r in comparison['products'])}
            changes.get_by_text('Обновление топа', exact=True).click()
            turnover_chart = changes.locator('.js-plotly-plot')
            expect(turnover_chart).to_be_visible()
            page.wait_for_function("[...document.querySelectorAll('.js-plotly-plot')].some(p => p.layout.yaxis.title.text === 'Новые товары, %')")
            turnover = store.snapshot_turnover(complete['id'], depth=expected[0]['requested_top_n'],
                      since=since.astimezone(timezone.utc).isoformat(),
                      until=until.astimezone(timezone.utc).isoformat())
            values = turnover_chart.evaluate('(p) => Array.from(p.data[0].y)')
            assert values == [r['percent'] for r in turnover]
            settled()
            turnover_chart.evaluate("(el) => el.scrollIntoView({block: 'center', behavior: 'instant'})")
            page.wait_for_timeout(300)
            turnover_chart.screenshot(path=str(output / 'turnover-chart.png'), animations='disabled')
            report['turnover'] = {'status': 'PASS', 'comparisons': len(values)}
            changes.get_by_text('Перемещения товаров', exact=True).click()
            changes.locator('summary').click()
            page.get_by_role('heading', name=complete['phrase'], exact=True).scroll_into_view_if_needed()
        with store.connect() as db:
            first = db.execute("SELECT min(started_at) FROM collection_runs "
                               "WHERE query_id=? AND status='success'", (complete['id'],)).fetchone()[0]
        empty_date = datetime.fromisoformat(first).astimezone(MSK).date() - timedelta(days=1)
        dates = page.locator('[data-testid="stDateInputField"]')
        original_dates = [dates.nth(i).input_value() for i in range(2)]
        def set_date(index: int, value: str) -> None:
            field = dates.nth(index)
            field.fill(value)
            field.press('Tab')
            expect(field).to_have_value(value)
            expect(page.locator('[data-testid="stApp"]')).to_have_attribute('data-test-script-state', 'notRunning')
        for index in range(2):
            set_date(index, f'{empty_date:%d.%m.%Y}')
        expect(page.get_by_text('За выбранный период нет снимков.', exact=False)).to_be_visible()
        expect(page.get_by_role('button', name='Скачать историю CSV')).to_have_count(0)
        expect(page.locator('[data-testid="stApp"]')).to_have_attribute('data-test-script-state', 'notRunning')
        set_date(0, original_dates[1])
        expect(page.get_by_text('Начало периода должно быть не позже окончания.', exact=True)).to_be_visible()
        report['invalid_period_filter'] = 'PASS'
        for index, value in enumerate(original_dates):
            set_date(index, value)
        expect(page.get_by_role('button', name='Скачать историю CSV')).to_be_visible()
        report['empty_period_filter'] = 'PASS'

        page.get_by_role('tab', name='Товары').click()
        with page.expect_download() as download:
            page.get_by_role('button', name='Скачать выдачу CSV').click()
        snapshot_path = output / 'latest_serp.csv'
        download.value.save_as(snapshot_path)
        snapshot = read_csv(snapshot_path)
        assert len(snapshot) == len(expected)
        for row, source in zip(snapshot, expected, strict=True):
            assert int(row['Позиция']) == source['position']
            assert int(row['Артикул']) == source['nm_id']
            price = Decimal(row['Цена, ₽']) if row['Цена, ₽'] else None
            assert price == (Decimal(source['price_kopecks']) / 100
                             if source['price_kopecks'] is not None else None)
            for label, field in [('Название', 'title'), ('Продавец', 'seller')]:
                assert row[label] == (source[field] or '')
            for label, field in [('Рейтинг', 'rating'), ('Отзывы', 'reviews'),
                                 ('Доставка, дни', 'delivery_days')]:
                value = Decimal(row[label]) if row[label] else None
                assert value == (Decimal(str(source[field])) if source[field] is not None else None)
        settled()
        page.screenshot(path=str(output / 'snapshot-desktop.png'), full_page=True, animations='disabled')

        page.get_by_role('tab', name='Сборы').click()
        expect(page.get_by_role('heading', name='Состояние запросов', exact=True)).to_be_visible()
        settled()
        page.screenshot(path=str(output / 'operations-desktop.png'), full_page=True, animations='disabled')
        partial = next((q for q in queries if not q['last_success'] and q['last_status'] == 'partial'), None)
        if partial:
            select_query(partial['phrase'])
            page.get_by_role('tab', name='Товары').click()
            partial_rows = store.latest_observations(partial['id'], include_partial=True)
            expect(page.get_by_text(f'Неполный сбор: {len(partial_rows)} из '
                                   f'{partial_rows[0]["requested_top_n"]} товаров.', exact=True)).to_be_visible()
            with page.expect_download() as download:
                page.get_by_role('button', name='Скачать выдачу CSV').click()
            partial_path = output / 'partial_serp.csv'
            download.value.save_as(partial_path)
            exported = read_csv(partial_path)
            assert [int(r['Артикул']) for r in exported] == [r['nm_id'] for r in partial_rows]
            assert [int(r['Позиция']) for r in exported] == [r['position'] for r in partial_rows]
            assert all(r['Статус'] == 'Неполный' for r in exported)
            page.get_by_role('tab', name='Позиции').click()
            expect(page.locator('.js-plotly-plot').first).to_be_visible()
            markers = page.locator('.js-plotly-plot').first.evaluate(
                '(p) => p.data.flatMap(t => Array.from(t.marker.symbol))')
            assert markers and all(m == 'diamond-open' for m in markers), markers
            expect(page.get_by_role('button', name='Скачать историю CSV')).to_be_visible()
            settled()
            page.screenshot(path=str(output / 'partial-desktop.png'), full_page=True, animations='disabled')
            report['partial_visible'] = {'query': partial['phrase'], 'count': len(exported),
                                         'selectable': store.product_count(partial['id']),
                                         'markers': markers}

        select_query(complete['phrase'])
        page.get_by_role('tab', name='Позиции').click()
        selector = page.get_by_test_id('stExpander').filter(has_text='Выбрать товары')
        selector.locator('summary').click()
        search = page.get_by_role('textbox', name='Название или артикул')
        search.fill(str(expected[0]['nm_id']))
        page.get_by_role('button', name='Найти', exact=True).click()
        expect(page.get_by_text('Найдено товаров: 1', exact=True)).to_be_visible()
        search.fill('ПОСТЕЛЬНОЕ')
        search.press('Enter')
        expect(page.get_by_role('combobox', name='Найденные товары')).to_be_visible()
        search.fill('неттакоготовара9876543210')
        search.press('Enter')
        expect(page.get_by_text('Товары не найдены.', exact=False)).to_be_visible()
        assert plot.evaluate('(p) => p.data.length') == len(selected_ids)
        report['catalog_search'] = {'exact_id': 'PASS', 'cyrillic_case': 'PASS', 'empty_keeps_chart': 'PASS'}
        selector.locator('summary').click()
        # All configured phrases must open their actual latest snapshot, including short catalogs.
        checked_queries = []
        for query in queries:
            select_query(query['phrase'])
            rows = store.latest_observations(query['id'], include_partial=True)
            metric = page.get_by_test_id('stMetric').filter(has_text='В выдаче')
            expect(metric.get_by_test_id('stMetricValue')).to_have_text(str(len(rows)) if rows else '—')
            expect(page.locator('[data-testid="stException"]')).to_have_count(0)
            checked_queries.append({'query_id': query['id'], 'rows': len(rows)})
        report['query_navigation'] = checked_queries
        select_query(complete['phrase'])
        page.get_by_role('button', name='Обновить', exact=True).click()
        expect(page.locator('.js-plotly-plot').first).to_be_visible()
        report['refresh'] = 'PASS'
        expect(page.locator('[data-testid="stApp"]')).to_have_attribute('data-test-script-state', 'notRunning')
        page.locator('[data-testid="stSidebar"]').hover()
        page.locator('[data-testid="stSidebarCollapseButton"] button').click()
        expect(page.locator('[data-testid="stSidebar"]')).to_have_attribute('aria-expanded', 'false')
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.locator('[data-testid="stSidebar"]')).to_have_attribute('aria-expanded', 'false')
        page.wait_for_function('document.querySelector(\'[data-testid="stSidebar"]\')'
                               '.getBoundingClientRect().right <= 1')
        expect(page.locator('.js-plotly-plot').first).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        settled()
        page.get_by_test_id('stMain').evaluate('(el) => el.scrollTo(0, 0)')
        page.screenshot(path=str(output / 'history-mobile.png'), full_page=True, animations='disabled')
        expect(page.locator('[data-testid="stException"]')).to_have_count(0)
        assert not errors, errors
        assert not outside_requests, outside_requests
        browser.close()

    after = database_counts(store)
    assert before == after, 'Database changed during UI check; stop collector and retry'
    report.update(status='PASS', graph=graph, database_before=before, database_after=after,
                  external_requests=outside_requests, javascript_errors=errors,
                  completed_at=datetime.now(timezone.utc).isoformat())
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/app/data/dashboard-check') /
                        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    args = parser.parse_args()
    report = check(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'Artifacts: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
