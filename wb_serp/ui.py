"""Read-only analytics dashboard built for a single VPS / Streamlit."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import html
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from wb_serp.charts import chart_rank_history
from wb_serp.config import Settings
from wb_serp.store import Store

MSK = ZoneInfo("Europe/Moscow")

st.set_page_config(
    page_title="SERP Observatory · Wildberries", page_icon="📈", layout="wide",
    initial_sidebar_state="auto",
)

CSS = """
<style>
:root{--accent:#5848E5;--ink:#17213A;--muted:#6F7D96;--border:#E8EBF2}
[data-testid="stAppViewContainer"]{background:#F6F8FC;color:var(--ink)}
[data-testid="stSidebar"]{background:#FFFFFF;border-right:1px solid var(--border)}
[data-testid="stHeader"]{background:transparent}
.block-container{max-width:1320px;padding-top:2.1rem;padding-bottom:3rem}
h1,h2,h3{color:var(--ink)!important;letter-spacing:-.035em}
h1{font-weight:760!important}
[data-testid="stMetric"]{background:#FFFFFF;padding:19px 22px;border:1px solid var(--border);border-radius:15px;
 box-shadow:0 3px 18px rgba(25,40,77,.025)}
[data-testid="stMetricLabel"]{color:var(--muted);font-size:.78rem}
[data-testid="stMetricValue"]{font-weight:700;color:var(--ink);font-size:1.7rem}
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:16px!important}
.stButton>button[kind="primary"]{background:var(--accent);border-color:var(--accent)}
.hero{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.logo{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;
 background:linear-gradient(135deg,#6355F6,#28B5BD);font-size:23px;color:white;font-weight:700}
.eyebrow{color:#7368CC;text-transform:uppercase;letter-spacing:.14em;font-weight:700;font-size:11px}
.subline{color:var(--muted);font-size:.95rem;margin-top:-10px;margin-bottom:22px}
.notice{font-size:.86rem;color:var(--muted);padding:12px 15px;border-left:3px solid #9A91ED;
background:#F0EFFB;border-radius:6px}
[data-testid="stDataFrame"]{border:1px solid var(--border);border-radius:12px;overflow:hidden}
@media(max-width:700px){.block-container{padding-top:1.2rem}.hero{gap:8px}}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def display_time(value: str | None) -> str:
    if not value:
        return "—"
    return datetime.fromisoformat(value).astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def to_date_bound(d: date, *, end: bool) -> str:
    local = datetime.combine(d, time.max if end else time.min, tzinfo=MSK)
    return local.astimezone(timezone.utc).isoformat(timespec="microseconds")


def info_card(label: str, content: str) -> None:
    st.metric(label=label, value=content)


def main() -> None:
    cfg = Settings.from_env()
    store = Store(Path(cfg.database_path), readonly=True)
    if not cfg.database_path.exists():
        st.error("База ещё не инициализирована. Запустите контейнер collector и дождитесь подготовки схемы.")
        st.stop()

    st.markdown("""<div class="hero"><div class="logo">↗</div><div>
       <div class="eyebrow">Marketplace intelligence</div>
       <h1 style="margin:0;font-size:2.12rem">SERP Observatory</h1>
       </div></div>""", unsafe_allow_html=True)
    st.markdown("<div class='subline'>Аналитика поисковых позиций Wildberries · регулярные реальные снимки выдачи</div>",
                unsafe_allow_html=True)

    try:
        queries = store.queries_dashboard()
    except Exception as exc:
        st.error(f"Не удалось прочитать SQLite: {exc}")
        st.stop()
    if not queries:
        st.info("Пока нет запросов. Запустите collector для загрузки config/queries.txt.")
        st.stop()

    with st.sidebar:
        st.markdown("### SERP Observatory")
        st.caption("АНАЛИТИЧЕСКАЯ ПАНЕЛЬ")
        lookup = {q["id"]: q for q in queries}
        query_id = st.selectbox(
            "Поисковый запрос", options=list(lookup),
            format_func=lambda qid: lookup[qid]["phrase"],
            help="Формулировки сохранены как в исходном задании.",
        )
        st.divider()
        today = datetime.now(MSK).date()
        # Handle both 1-date and 2-date selection without surprising unpack errors.
        selected_range = st.date_input("История за период", value=(today - timedelta(days=14), today),
                                       max_value=today, format="DD.MM.YYYY")
        if len(selected_range) == 2:
            start_date, end_date = selected_range
        else:
            start_date, end_date = today - timedelta(days=14), today
            st.caption("Выберите вторую дату для задания диапазона.")
        st.caption(f"Глубина: top-{cfg.top_n} · Регион dest={cfg.destination}")
        if st.button("↻ Обновить данные", width="stretch"):
            st.rerun()
        st.divider()
        st.caption("ℹ️ Отчёты строятся только по полным успешным снимкам. Время отображается по Москве.")

    query = lookup[query_id]
    st.markdown(f"#### {html.escape(query['phrase'])}")
    st.caption("Поисковая фраза · без нормализации и исправления")

    runs = store.recent_runs(query_id, limit=200)
    success_runs = [r for r in runs if r["status"] == "success"]
    product_total = store.product_count(query_id)
    latest_rows = store.latest_observations(query_id)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        info_card("Успешных снимков (посл. 200)", str(len(success_runs)))
    with c2:
        info_card("Уникальных артикулов", f"{product_total:,}".replace(",", " "))
    with c3:
        info_card("Товаров в выдаче", str(len(latest_rows)) if latest_rows else "—")
    with c4:
        info_card("Последний полный сбор", datetime.fromisoformat(query["last_success"])
                  .astimezone(MSK).strftime("%d.%m %H:%M")
                  if query["last_success"] else "—")

    overview, snapshot, operations = st.tabs(["📉 Динамика позиций", "📋 Последняя выдача", "🩺 Состояние сборщика"])
    with overview:
        st.markdown("##### Изменение позиций")
        if not product_total:
            st.info("Для этого запроса ещё нет полного снимка. Проверьте вкладку «Состояние сборщика».")
        else:
            chosen_key = f"chosen_products_{query_id}"
            if chosen_key not in st.session_state:
                st.session_state[chosen_key] = [row["nm_id"] for row in latest_rows[:3]]
            search = st.text_input(
                "Поиск артикула или названия", key=f"catalog_search_{query_id}",
                placeholder="Например: 123456789 или сатин",
                help="Поиск по всей истории, включая товары, отсутствующие в последнем снимке.",
            )
            matches = store.search_products(query_id, search, limit=250)
            retained = store.products_by_ids(query_id, st.session_state[chosen_key])
            product_map = {p["nm_id"]: p for p in [*retained, *matches]}
            options = list(product_map)
            label_by_id = {
                p["nm_id"]: f"{p['nm_id']} · {(p['last_title'] or 'Без названия')[:65]}"
                for p in product_map.values()
            }
            selected = st.multiselect(
                "Товары для сравнения (до 30)", options=options,
                key=chosen_key, format_func=lambda nm: label_by_id[nm],
                help="Поиск ограничивает подсказки 250 результатами, но выбранные артикулы сохраняются.",
                max_selections=30,
            )
            st.caption(f"Найдено подсказок: {len(matches)} из {product_total:,} известных товаров. "
                       "Уточните поиск, если нужного артикула нет в списке.")
            depths = store.available_depths(query_id)
            chosen_depth = st.selectbox(
                "Глубина сравнения", options=depths,
                index=depths.index(cfg.top_n) if cfg.top_n in depths else 0,
                format_func=lambda n: f"Топ-{n}",
                help="Снимки разных глубин не смешиваются: отсутствие в top-100 "
                     "не равно отсутствию в top-200.",
            )
            if len(depths) > 1:
                st.caption("Существуют снимки разной глубины. На графике — только выбранная глубина.")
            if not selected:
                st.info("Выберите хотя бы один артикул для построения графика.")
            else:
                date_since = to_date_bound(start_date, end=False)
                date_until = to_date_bound(end_date, end=True)
                total_runs = store.count_success_runs(query_id, since=date_since,
                                                     until=date_until, depth=chosen_depth)
                if total_runs > 2200:
                    st.warning(f"За период найдено {total_runs} снимков. График показывает "
                               "только последние 2200; сузьте диапазон для точного анализа.")
                history = store.rank_history(query_id, selected,
                                             since=date_since, until=date_until,
                                             depth=chosen_depth)
                if history:
                    st.plotly_chart(chart_rank_history(history, selected),
                                    width="stretch", theme=None,
                                    config={"displaylogo": False, "responsive": True})
                    absent = sum(row["position"] is None for row in history)
                    st.caption(f"Пропуски: {absent}. Это отсутствие в top-{chosen_depth}, а не "
                               "обязательно исчезновение из поиска. Линии не соединяются через пропуски.")
                    export = pd.DataFrame(history)
                    export["position"] = export["position"].astype("Int64")
                    export["started_at"] = export["started_at"].map(display_time)
                    st.download_button("Скачать историю CSV", export.to_csv(index=False).encode("utf-8-sig"),
                                       "rank_history.csv", mime="text/csv")
                else:
                    st.warning("За выбранный период нет успешных снимков. Расширьте диапазон.")
        st.markdown("<div class='notice'>Отрицательная динамика графика означает ухудшение позиции: "
                    "№1 расположен наверху. Значение позиции не интерполируется между запусками.</div>",
                    unsafe_allow_html=True)

    with snapshot:
        st.markdown("##### Последний подтверждённый top-N")
        if latest_rows:
            table = pd.DataFrame(latest_rows)
            table["price_rub"] = table.pop("price_kopecks") / 100
            table = table.rename(columns={"position": "Позиция", "nm_id": "Артикул",
                "price_rub": "Цена, ₽", "rating": "Рейтинг", "reviews": "Отзывы",
                "delivery_days": "Доставка, дни", "seller": "Продавец", "title": "Название",
                "started_at": "Время", "requested_top_n": "Глубина"})
            st.dataframe(table.drop(columns=["Время", "Глубина"]), width="stretch",
                         hide_index=True, height=520)
            st.caption(f"Сбор: {display_time(latest_rows[0]['started_at'])} · "
                       f"top-{latest_rows[0]['requested_top_n']} · NULL = поле не было доступно")
            st.download_button("Скачать выдачу CSV", table.to_csv(index=False).encode("utf-8-sig"),
                               "latest_serp.csv", mime="text/csv")
        else:
            st.info("Нет подтверждённого полного снимка. Частичные результаты в выдачу не включаются.")

    with operations:
        st.markdown("##### Журнал запусков")
        st.caption("Ошибки и блокировки видны в журнале, но не интерпретируются как потеря позиции.")
        if runs:
            status_labels = {"success": "Успешно", "partial": "Неполный", "failed": "Ошибка",
                             "blocked": "Ограничен доступ", "running": "В процессе", "interrupted": "Прерван"}
            table = pd.DataFrame(runs)
            table["status"] = table["status"].map(status_labels)
            table["started_at"] = table["started_at"].map(display_time)
            table["completed_at"] = table["completed_at"].map(display_time)
            table = table.rename(columns={"started_at": "Начало", "completed_at": "Конец",
                "status": "Статус", "observed_count": "Собрано", "requested_top_n": "Цель",
                "error": "Диагностика", "destination": "Регион"})
            st.dataframe(table[["Начало", "Конец", "Статус", "Собрано", "Цель", "Регион", "Диагностика"]],
                         hide_index=True, width="stretch")
        else:
            st.info("Для запроса ещё не было запусков.")
        st.divider()
        st.markdown("##### Состояние всех 70 запросов")
        monitor = pd.DataFrame(queries)
        monitor["last_status"] = monitor["last_status"].fillna("Нет запусков")
        monitor["last_attempt"] = monitor["last_attempt"].map(display_time)
        monitor["last_success"] = monitor["last_success"].map(display_time)
        monitor = monitor.rename(columns={"phrase": "Запрос", "last_status": "Статус",
            "last_attempt": "Последний запуск", "last_success": "Последний успех",
            "last_count": "Найдено"})
        st.dataframe(monitor[["Запрос", "Статус", "Последний запуск", "Последний успех", "Найдено"]],
                     hide_index=True, width="stretch", height=350)


if __name__ == "__main__":
    main()
