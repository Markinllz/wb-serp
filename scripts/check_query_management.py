"""Browser verification of query and collection controls on a disposable DB copy."""
from datetime import datetime, timezone
import json
import re

from playwright.sync_api import expect, sync_playwright

from wb_serp.config import Settings
from wb_serp.store import Store


def main():
    cfg = Settings.from_env()
    if not cfg.database_path.name.startswith('ui-check-'):
        raise RuntimeError('Use a disposable ui-check-*.db copy')
    store = Store(cfg.database_path)
    original = store.queries_dashboard(include_paused=True)
    phrase = 'Проверка управления запросами'
    tables = ('collection_runs', 'observations', 'product_catalog')
    with store.connect() as db:
        before = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}
    errors, outside = [], []
    expect.set_options(timeout=20000)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chromium', headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width':1440, 'height':1050})
        def local_only(route):
            if route.request.url.startswith('http://127.0.0.1:8501/'):
                route.continue_()
            else:
                outside.append(route.request.url)
                route.abort()
        context.route('**/*', local_only)
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('http://127.0.0.1:8501')
        sidebar = page.get_by_test_id('stSidebar')
        def settled():
            expect(page.get_by_test_id('stApp')).to_have_attribute('data-test-script-state', 'notRunning')
            expect(page.get_by_test_id('stException')).to_have_count(0)
        def nav(name):
            sidebar.get_by_test_id('stRadio').get_by_text(name, exact=True).click()
            settled()
        nav('Запросы')
        field = page.get_by_role('textbox', name='Новый поисковый запрос')
        field.fill(phrase)
        page.get_by_role('button', name='Добавить запрос', exact=True).click()
        expect(page.get_by_text('Запрос добавлен.', exact=True)).to_be_visible()
        added = next(q for q in store.queries_dashboard() if q['phrase'] == phrase)
        qid = added['id']
        page.get_by_role('button', name='Приостановить запрос', exact=True).click()
        expect(page.get_by_role('button', name='Включить запрос', exact=True)).to_be_visible()
        assert qid not in [q['id'] for q in store.enabled_queries()]
        page.get_by_role('button', name='Включить запрос', exact=True).click()
        expect(page.get_by_role('button', name='Приостановить запрос', exact=True)).to_be_visible()
        field.fill(phrase)
        page.get_by_role('button', name='Добавить запрос', exact=True).click()
        expect(page.get_by_text('Этот запрос уже собирается.', exact=True)).to_be_visible()
        page.get_by_role('button', name='Открыть аналитику', exact=True).click()
        expect(page.get_by_role('combobox', name=re.compile('Поисковый запрос'))).to_have_attribute('aria-label', re.compile(re.escape(phrase)))
        nav('Запросы')
        page.get_by_role('button', name='Удалить запрос', exact=True).click()
        expect(page.get_by_text('Запрос удалён из списка.', exact=False)).to_be_visible()
        assert qid not in [q['id'] for q in store.queries_dashboard(include_paused=True)]
        archive = page.get_by_test_id('stExpander').filter(has_text='Удалённые запросы')
        archive.locator('summary').click()
        page.get_by_role('button', name='Восстановить запрос').click()
        expect(page.get_by_role('button', name='Включить запрос', exact=True)).to_be_visible()
        assert store.product_count(qid) == 0
        nav('Сбор данных')
        page.get_by_role('button', name='Остановить сбор', exact=True).click()
        expect(page.get_by_role('button', name='Возобновить сбор', exact=True)).to_be_visible()
        assert not store.collection_options()['enabled']
        depth = page.get_by_role('spinbutton', name='Товаров на один запрос')
        for value in (50, 300, 200):
            depth.fill(str(value))
            page.get_by_role('button', name='Сохранить глубину').click()
            expect(page.get_by_text('Глубина сохранена.', exact=False)).to_be_visible()
            for _ in range(100):
                if store.collection_options()['top_n'] == value:
                    break
                page.wait_for_timeout(100)
            assert store.collection_options()['top_n'] == value
        page.reload()
        nav('Сбор данных')
        expect(page.get_by_role('spinbutton', name='Товаров на один запрос')).to_have_value('200')
        assert not store.collection_options()['enabled']
        page.get_by_role('button', name='Возобновить сбор', exact=True).click()
        expect(page.get_by_role('button', name='Остановить сбор', exact=True)).to_be_visible()
        assert store.collection_options()['enabled'] and store.collection_options()['revision'] == 1
        # Removing every query must leave a working path to add or restore one.
        for query in original:
            store.archive_query(query['id'])
        store.archive_query(qid)
        nav('Аналитика')
        expect(page.get_by_text('Пока нет запросов.', exact=False)).to_be_visible()
        nav('Запросы')
        expect(field).to_be_visible()
        for query in original:
            store.archive_query(query['id'], False)
            store.set_query_enabled(query['id'], bool(query['enabled']))
        nav('Аналитика')
        expect(page.locator('.js-plotly-plot').first).to_be_visible()
        output = cfg.database_path.parent/'dashboard-check'/cfg.database_path.stem
        output.mkdir(parents=True, exist_ok=True)
        settled()
        page.screenshot(path=str(output/'analytics.png'), full_page=True)
        assert not outside and not errors, (outside, errors)
        browser.close()
    with store.connect() as db:
        after = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}
    assert before == after
    report = {'status':'PASS', 'checked_at':datetime.now(timezone.utc).isoformat(),
              'add_pause_resume_delete_restore':True, 'open_query_analytics':True,
              'global_stop_resume':True, 'depth_50_300_200':True,
              'settings_survive_reload':True, 'empty_query_list_navigation':True,
              'history_unchanged':True, 'javascript_errors':errors, 'external_requests':outside}
    (output/'controls-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
