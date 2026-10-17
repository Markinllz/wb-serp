"""Exercise query controls on a disposable database copy, without WB requests."""

from datetime import datetime, timezone
import hashlib
import json

from playwright.sync_api import expect, sync_playwright

from wb_serp.cli import prepare
from wb_serp.config import Settings
from wb_serp.store import Store


def main():
    cfg = Settings.from_env()
    if not cfg.database_path.name.startswith("ui-check-"):
        raise RuntimeError("Use a disposable SQLite copy named ui-check-*.sqlite")
    store = Store(cfg.database_path)
    original = store.queries_dashboard(include_paused=True)
    if len(original) != 70 or not all(q["enabled"] for q in original):
        raise RuntimeError("Start with the unchanged 70-query fixture copy")
    with store.connect() as db:
        before = {
            t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("collection_runs", "observations", "product_catalog")
        }
    source_hash = hashlib.sha256(cfg.queries_path.read_bytes()).hexdigest()
    output = cfg.database_path.parent / "dashboard-check" / cfg.database_path.stem
    output.mkdir(parents=True, exist_ok=True)
    phrase = "Проверка интерфейса — дополнительный запрос"
    errors, outside = [], []
    expect.set_options(timeout=30000)
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel="chromium", headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 1050})

        def local_only(route):
            if route.request.url.startswith("http://127.0.0.1:8501/"):
                route.continue_()
            else:
                outside.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("http://127.0.0.1:8501", wait_until="domcontentloaded")
        expect(page.get_by_role("combobox", name="Поисковый запрос")).to_be_visible()
        expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
        page.get_by_role("tab", name="Запросы", exact=True).click()
        field = page.get_by_role("textbox", name="Новый поисковый запрос")
        field.fill(phrase)
        page.get_by_role("button", name="Добавить запрос", exact=True).click()
        expect(page.get_by_text("Запрос добавлен.", exact=False)).to_be_visible()
        expect(page.get_by_role("heading", name=phrase, exact=True)).to_be_visible()
        added = next(q for q in store.queries_dashboard() if q["phrase"] == phrase)
        qid = added["id"]
        assert len(store.enabled_queries()) == 71
        assert qid in [q["id"] for q in prepare(cfg).enabled_queries()]

        def manage(text):
            expect(page.get_by_test_id("stApp")).to_have_attribute(
                "data-test-script-state", "notRunning"
            )
            page.get_by_role("tab", name="Запросы", exact=True).click()
            box = page.get_by_role("combobox", name="Запрос для управления")
            box.fill(text)
            page.get_by_role("option", name=text, exact=True).click()
            expect(page.get_by_test_id("stApp")).to_have_attribute(
                "data-test-script-state", "notRunning"
            )

        page.get_by_role("button", name="Поставить на паузу", exact=True).click()
        expect(page.get_by_text("Запрос на паузе. История сохранена.", exact=True)).to_be_visible()
        assert qid not in [q["id"] for q in prepare(cfg).enabled_queries()]
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("combobox", name="Поисковый запрос")).to_be_visible()
        expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
        page.get_by_role("tab", name="Запросы", exact=True).click()
        manage(phrase)
        page.get_by_role("button", name="Возобновить сбор", exact=True).click()
        expect(page.get_by_text("Сбор запроса включён.", exact=True)).to_be_visible()
        assert qid in [q["id"] for q in store.enabled_queries()]
        field.fill(phrase)
        page.get_by_role("button", name="Добавить запрос", exact=True).click()
        expect(page.get_by_text("Этот запрос уже собирается.", exact=True)).to_be_visible()
        assert len(store.queries_dashboard(include_paused=True)) == 71

        manage(original[0]["phrase"])
        page.get_by_role("button", name="Поставить на паузу", exact=True).click()
        expect(
            page.get_by_text("Набор ТЗ: активно 69 из 70 исходных запросов.", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="Включить все исходные запросы", exact=True).click()
        expect(page.get_by_text("Все исходные запросы включены.", exact=True)).to_be_visible()
        assert len(store.enabled_queries()) == 71
        expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
        page.screenshot(path=str(output / "queries-desktop.png"), full_page=True)
        for q in store.enabled_queries():
            store.set_query_enabled(q["id"], False)
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("combobox", name="Поисковый запрос")).to_be_visible()
        expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
        expect(page.get_by_text("Сбор запроса на паузе.", exact=False)).to_be_visible()
        expect(page.get_by_test_id("stException")).to_have_count(0)
        expect(page.locator(".js-plotly-plot").first).to_be_visible()
        page.get_by_role("tab", name="Запросы", exact=True).click()
        page.get_by_role("button", name="Включить все исходные запросы", exact=True).click()
        expect(page.get_by_text("Все исходные запросы включены.", exact=True)).to_be_visible()
        assert len(store.enabled_queries()) == 70
        assert not next(q for q in store.queries_dashboard(include_paused=True) if q["id"] == qid)[
            "enabled"
        ]
        assert not errors and not outside, (errors, outside)
        browser.close()
    with store.connect() as db:
        after = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after
    assert hashlib.sha256(cfg.queries_path.read_bytes()).hexdigest() == source_hash
    report = {
        "status": "PASS",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "add": "PASS",
        "pause_resume": "PASS",
        "restart_choices": "PASS",
        "duplicate": "PASS",
        "restore_presets_keeps_custom": "PASS",
        "all_paused_history_access": "PASS",
        "source_70_unchanged": True,
        "history_before": before,
        "history_after": after,
        "javascript_errors": errors,
        "external_requests": outside,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
