"""Read-only search-position dashboard."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import html
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from wb_serp.charts import chart_product_history, chart_rank_changes
from wb_serp.config import Settings
from wb_serp.store import Store

MSK = ZoneInfo("Europe/Moscow")
STATUS_LABELS = {
    "success": "Полный", "partial": "Неполный", "failed": "Ошибка",
    "blocked": "Нет доступа", "running": "Идёт сбор", "interrupted": "Прерван",
}

st.set_page_config(page_title="WB SERP · Позиции", layout="wide", initial_sidebar_state="auto")
st.markdown("""
<style>
[data-testid="stSidebar"]{border-right:1px solid #e4e7ec}
[data-testid="stHeader"]{background:rgba(255,255,255,.94)}
.block-container{max-width:1440px;padding-top:2.4rem;padding-bottom:2rem}
h1{font-size:1.65rem!important;font-weight:650!important;line-height:1.3!important;
 letter-spacing:-.025em;padding-bottom:.35rem!important}
h2,h3{letter-spacing:-.02em}
[data-testid="stMetricLabel"]{color:#596273;font-size:.8rem}
[data-testid="stMetricValue"]{font-size:1.65rem;font-weight:600}
.st-key-summary{padding:.25rem 0 .8rem;border-bottom:1px solid #e4e7ec}
.st-key-summary [data-testid="stHorizontalBlock"]{flex-wrap:nowrap;gap:1rem}
.st-key-summary [data-testid="stColumn"]{min-width:0!important}
[data-testid="stTabs"] [role="tablist"]{gap:1.6rem}
[data-testid="stTabs"] [role="tab"]{font-size:.95rem}
[data-testid="stDataFrame"]{border:1px solid #e4e7ec;border-radius:6px;overflow:hidden}
[data-testid="stSidebar"] .block-container{padding-top:1.5rem}
@media(max-width:700px){
 .block-container{padding-top:2rem;padding-left:1rem;padding-right:1rem}
 h1{font-size:1.3rem!important}
 [data-testid="stMetricValue"]{font-size:1.3rem}
 [data-testid="stMetricLabel"]{font-size:.72rem}
}
</style>
""", unsafe_allow_html=True)


def display_time(value: str | None) -> str:
    if not value:
        return "—"
    return datetime.fromisoformat(value).astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def to_date_bound(d: date, *, end: bool) -> str:
    local = datetime.combine(d, time.max if end else time.min, tzinfo=MSK)
    return local.astimezone(timezone.utc).isoformat(timespec="microseconds")


def snapshot_table(rows: list[dict]) -> pd.DataFrame:
    table = pd.DataFrame(rows)
    table["price_rub"] = table.pop("price_kopecks") / 100
    table["started_at"] = table["started_at"].map(display_time)
    table["run_status"] = table["run_status"].map(STATUS_LABELS)
    for column in ("position", "nm_id", "reviews", "delivery_days"):
        table[column] = table[column].astype("Int64")
    return table.rename(columns={
        "position": "Позиция", "nm_id": "Артикул", "title": "Название", "price_rub": "Цена, ₽",
        "rating": "Рейтинг", "reviews": "Отзывы", "delivery_days": "Доставка, дни",
        "seller": "Продавец", "started_at": "Время", "requested_top_n": "Глубина",
        "run_status": "Статус",
    })


def show_history(store: Store, query_id: int, latest_rows: list[dict], product_total: int,
                 top_n: int, start_date: date | None, end_date: date | None) -> int | None:
    if not product_total:
        st.info("Товаров пока нет. Результаты запусков — на вкладке «Сборы».")
        return
    chosen_key = f"chosen_products_{query_id}"
    if chosen_key not in st.session_state:
        st.session_state[chosen_key] = [row["nm_id"] for row in latest_rows[:3]]
    search = st.text_input("Поиск артикула или названия", key=f"catalog_search_{query_id}")
    matches = store.search_products(query_id, search, limit=250)
    retained = store.products_by_ids(query_id, st.session_state[chosen_key])
    product_map = {p["nm_id"]: p for p in [*retained, *matches]}
    selected = st.multiselect(
        "Товары для сравнения", options=list(product_map), key=chosen_key,
        format_func=lambda nm: f"{nm} · {(product_map[nm]['last_title'] or 'Без названия')[:32]}",
        max_selections=30, help="До 30 артикулов из всей истории запроса",
    )
    if search:
        st.caption(f"Найдено: {len(matches)} · В истории: {product_total}")
    elif product_total > len(matches):
        st.caption(f"В списке {len(matches)} из {product_total} товаров. Остальные доступны через поиск.")
    depths = store.available_depths(query_id, include_partial=True)
    if not depths:
        st.info("Снимков для графика пока нет.")
        return
    chosen_depth = top_n if top_n in depths else depths[0]
    if len(depths) > 1:
        chosen_depth = st.selectbox("Глубина сравнения", options=depths,
                                    index=depths.index(chosen_depth),
                                    format_func=lambda n: f"Топ-{n}")
    if not selected:
        st.info("Выберите артикулы для сравнения.")
        return chosen_depth
    if start_date is None or end_date is None:
        st.info("Укажите начало и конец периода графика.")
        return
    if start_date > end_date:
        st.warning("Начало периода должно быть не позже окончания.")
        return
    date_since = to_date_bound(start_date, end=False)
    date_until = to_date_bound(end_date, end=True)
    total_runs = store.count_history_runs(query_id, since=date_since, until=date_until,
                                          depth=chosen_depth, include_partial=True)
    if total_runs > 2200:
        st.warning(f"Показаны последние 2200 из {total_runs} снимков. Сократите период.")
    history = store.rank_history(query_id, selected, since=date_since, until=date_until,
                                 depth=chosen_depth, include_partial=True)
    if not history:
        st.info("За выбранный период нет снимков. Измените период графика.")
        return chosen_depth
    metric = st.radio("Показатель", ["Позиции", "Цены"], horizontal=True,
                       key=f"history_metric_{query_id}", label_visibility="collapsed")
    field = "position" if metric == "Позиции" else "price_kopecks"
    if any(row[field] is not None for row in history):
        st.plotly_chart(chart_product_history(history, selected,
                        metric="position" if metric == "Позиции" else "price"),
                        width="stretch", theme=None,
                        config={"displaylogo": False, "responsive": True, "scrollZoom": False})
    else:
        st.info("Выбранные товары не найдены в снимках за этот период." if field == "position"
                else "Цены выбранных товаров за этот период недоступны.")
    if metric == "Цены":
        st.caption("Цена из ответа Wildberries. Персональные скидки покупателя могут отличаться.")
    absent = sum(row["position"] is None and row["run_status"] == "success" for row in history)
    partial = any(row["run_status"] == "partial" for row in history)
    if absent and metric == "Позиции":
        st.caption(f"Пропуски в полном топ-{chosen_depth}: {absent}.")
    if partial:
        st.caption("Пустой ромб — данные неполного сбора. Пропуск в нём означает отсутствие данных.")
    export = pd.DataFrame(history)
    for column in ("position", "price_kopecks", "reviews", "delivery_days"):
        export[column] = export[column].astype("Int64")
    export["started_at"] = export["started_at"].map(display_time)
    st.download_button("Скачать историю CSV", export.to_csv(index=False).encode("utf-8-sig"),
                       "rank_history.csv", mime="text/csv", on_click="ignore")
    return chosen_depth


def show_changes(store: Store, query_id: int, depth: int, start_date: date, end_date: date) -> None:
    with st.expander("Изменения выдачи"):
        comparison = store.compare_latest_snapshots(query_id, depth=depth,
                    since=to_date_bound(start_date, end=False), until=to_date_bound(end_date, end=True))
        if comparison is None:
            st.info("Для сравнения нужны два полных снимка одинаковой глубины за выбранный период.")
            return
        before, after = comparison["before"], comparison["after"]
        products = comparison["products"]
        st.caption(f"Вся выдача · топ-{depth} · {display_time(before['started_at'])} → "
                   f"{display_time(after['started_at'])}")
        counts = {status: sum(p["change"] == status for p in products)
                  for status in ("up", "down", "entered", "left")}
        st.caption(f"Выше: {counts['up']} · Ниже: {counts['down']} · "
                   f"Вошли: {counts['entered']} · Вышли: {counts['left']}")
        changed = counts["up"] + counts["down"]
        if changed:
            st.plotly_chart(chart_rank_changes(products), width="stretch", theme=None,
                            config={"displaylogo": False, "responsive": True, "scrollZoom": False})
            if changed > 12:
                st.caption("На графике — 12 наибольших изменений. Все товары — в таблице.")
        else:
            st.info("У общих товаров позиции не изменились.")
        labels = {"up": "Выше", "down": "Ниже", "entered": "Вошёл в топ",
                  "left": "Вышел из топа", "unchanged": "Без изменений"}
        table = pd.DataFrame(products)
        table["change"] = table["change"].map(labels)
        for column in ("before", "after", "delta"):
            table[column] = table[column].astype("Int64")
        table = table.rename(columns={"nm_id": "Артикул", "title": "Название", "before": "Было",
                    "after": "Стало", "delta": "Изменение", "change": "Статус"})
        st.dataframe(table, hide_index=True, width="stretch", height=320, placeholder="—",
                     column_config={"Изменение": st.column_config.NumberColumn(format="%+d")})
        st.caption("Изменение = было − стало. У вошедших и вышедших товаров числового изменения нет.")
        export = table.assign(Снимок_до=before["started_at"], Снимок_после=after["started_at"],
                               Глубина=depth, Регион=after["destination"])
        st.download_button("Скачать сравнение CSV", export.to_csv(index=False).encode("utf-8-sig"),
                           "rank_changes.csv", mime="text/csv", on_click="ignore")


def main() -> None:
    cfg = Settings.from_env()
    store = Store(Path(cfg.database_path), readonly=True)
    if not cfg.database_path.exists():
        st.error("База ещё не инициализирована. Запустите сборщик.")
        st.stop()
    try:
        queries = store.queries_dashboard()
    except Exception as exc:
        st.error(f"Не удалось открыть базу данных: {exc}")
        st.stop()
    if not queries:
        st.info("Пока нет запросов. Запустите сборщик.")
        st.stop()

    with st.sidebar:
        st.markdown("### WB SERP")
        st.caption("Позиции в поиске Wildberries")
        lookup = {q["id"]: q for q in queries}
        query_id = st.selectbox("Поисковый запрос", options=list(lookup),
                                format_func=lambda qid: lookup[qid]["phrase"])
        today = datetime.now(MSK).date()
        start_date = st.date_input("График с", value=today - timedelta(days=14),
                                   key="history_start", max_value=today, format="DD.MM.YYYY")
        end_date = st.date_input("По", value=today, key="history_end",
                                 max_value=today, format="DD.MM.YYYY")
        if st.button("Обновить", width="stretch", help="Перечитать сохранённые данные"):
            st.rerun()
        with st.expander("О данных"):
            st.caption(f"Время по Москве. Регион WB: {cfg.destination}.")
            st.caption("Позиция 1 — наверху графика. Разрыв линии — нет позиции в этом снимке. "
                       "Ошибочные сборы не участвуют в графике.")
            st.caption("Пустые характеристики не были доступны в ответе Wildberries.")

    query = lookup[query_id]
    latest_rows = store.latest_observations(query_id, include_partial=True)
    runs = store.recent_runs(query_id, limit=200)
    product_total = store.product_count(query_id)
    st.markdown(f"<h1>{html.escape(query['phrase'])}</h1>", unsafe_allow_html=True)
    if latest_rows:
        latest = latest_rows[0]
        st.caption(f"Снимок: {display_time(latest['started_at'])} · Топ-{latest['requested_top_n']}")
        if latest["run_status"] == "partial":
            st.warning(f"Неполный сбор: {len(latest_rows)} из {latest['requested_top_n']} товаров.")
    else:
        st.caption("Первый снимок ещё не получен")
    if runs and runs[0]["status"] in ("failed", "blocked", "interrupted"):
        st.warning("Последняя попытка сбора не завершена. Подробности — на вкладке «Сборы».")
    elif runs and runs[0]["status"] == "partial" and runs[0]["observed_count"] == 0:
        st.warning("Последний сбор не вернул товаров. Подробности — на вкладке «Сборы».")

    with st.container(key="summary"):
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("В выдаче", str(len(latest_rows)) if latest_rows else "—",
                      help="Товаров в последнем сохранённом снимке")
        with c2:
            st.metric("В истории", f"{product_total:,}".replace(",", " "),
                      help="Уникальных артикулов за всё время")
        with c3:
            st.metric("Снимков", str(store.count_history_runs(query_id, include_partial=True)))

    overview, snapshot, operations = st.tabs(["Позиции", "Товары", "Сборы"])
    with overview:
        depth = show_history(store, query_id, latest_rows, product_total, cfg.top_n, start_date, end_date)
        if depth is not None and start_date is not None and end_date is not None and start_date <= end_date:
            show_changes(store, query_id, depth, start_date, end_date)
    with snapshot:
        if latest_rows:
            table = snapshot_table(latest_rows)
            st.dataframe(table, column_order=["Позиция", "Артикул", "Название", "Цена, ₽", "Рейтинг",
                         "Отзывы", "Доставка, дни", "Продавец"], width="stretch", hide_index=True,
                         height=560, placeholder="—", column_config={
                "Позиция": st.column_config.NumberColumn(format="%d", width="small"),
                "Артикул": st.column_config.NumberColumn(format="%d"),
                "Название": st.column_config.TextColumn(width="medium"),
                "Продавец": st.column_config.TextColumn(width="medium"),
                "Цена, ₽": st.column_config.NumberColumn(format="%.2f ₽"),
                "Рейтинг": st.column_config.NumberColumn(format="%.1f"),
                "Отзывы": st.column_config.NumberColumn(format="%d"),
                "Доставка, дни": st.column_config.NumberColumn(format="%d"),
            })
            st.download_button("Скачать выдачу CSV", table.to_csv(index=False).encode("utf-8-sig"),
                               "latest_serp.csv", mime="text/csv", on_click="ignore")
        else:
            st.info("Сохранённых товаров пока нет.")
    with operations:
        st.markdown(f"#### Все запросы · {len(queries)}")
        monitor = pd.DataFrame(queries)
        monitor["last_status"] = monitor["last_status"].map(STATUS_LABELS).fillna("Нет запусков")
        for column in ("last_attempt", "last_success"):
            monitor[column] = monitor[column].map(display_time)
        monitor["last_count"] = monitor["last_count"].astype("Int64")
        monitor = monitor.rename(columns={"phrase": "Запрос", "last_status": "Статус",
            "last_attempt": "Последний запуск", "last_success": "Последний полный", "last_count": "Товаров"})
        st.dataframe(monitor[["Запрос", "Статус", "Товаров", "Последний запуск", "Последний полный"]],
                     hide_index=True, width="stretch", height=350, placeholder="—")
        st.markdown("#### Запуски выбранного запроса")
        if runs:
            table = pd.DataFrame(runs)
            table["status"] = table["status"].map(STATUS_LABELS)
            for column in ("started_at", "completed_at"):
                table[column] = table[column].map(display_time)
            table = table.rename(columns={"started_at": "Начало", "completed_at": "Конец",
                "status": "Статус", "observed_count": "Товаров", "requested_top_n": "Цель", "error": "Причина"})
            st.dataframe(table[["Начало", "Конец", "Статус", "Товаров", "Цель", "Причина"]],
                         hide_index=True, width="stretch", placeholder="—")
        else:
            st.info("Запусков пока нет.")


if __name__ == "__main__":
    main()
