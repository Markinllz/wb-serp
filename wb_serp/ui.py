"""Search analytics and query collection settings."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import html
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from wb_serp.charts import PALETTE, chart_product_history, chart_rank_changes, chart_turnover
from wb_serp.config import Settings
from wb_serp.store import Store
from wb_serp.presentation import next_collection, product_summaries

MSK = ZoneInfo("Europe/Moscow")
STATUS_LABELS = {
    "success": "Полный", "partial": "Неполный", "failed": "Ошибка",
    "blocked": "Нет доступа", "running": "Идёт сбор", "interrupted": "Прерван",
}

st.set_page_config(page_title="WB SERP — Позиции", layout="wide", initial_sidebar_state="auto")
st.markdown('<style>' + (Path(__file__).parent / 'assets/dashboard.css').read_text() + '</style>',
            unsafe_allow_html=True)


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


def show_product_cards(summaries: list[dict], titles: dict, colors: dict, metric: str) -> None:
    cards = []
    for row in summaries:
        nm_id = row['nm_id']
        value, delta = row['value'], row['delta']
        if value is None:
            shown = f"Вне топ-{row['depth']}" if row['state'] == 'outside' else 'Нет данных'
        else:
            shown = f"№ {value}" if metric == 'position' else f"{value / 100:,.2f} ₽".replace(',', ' ')
        if delta is None:
            change, tone = 'Нет сравнения', ''
        elif delta == 0:
            change, tone = 'Без изменений', ''
        else:
            good = delta > 0 if metric == 'position' else delta < 0
            tone = 'positive' if good else 'negative'
            change = (f"{'↑' if delta > 0 else '↓'} {abs(delta)} поз." if metric == 'position'
                      else f"{delta / 100:+,.2f} ₽".replace(',', ' '))
        color = PALETTE[colors[nm_id] % len(PALETTE)]
        style = ('solid', 'dashed', 'dotted')[(colors[nm_id] // len(PALETTE)) % 3]
        title = html.escape(titles[nm_id])
        cards.append(f'<div class="product-card"><div class="product-meta">'
                     f'<span class="product-swatch" style="border-color:{color};border-top-style:{style}"></span>'
                     f'Артикул {nm_id}</div><a href="https://www.wildberries.ru/catalog/{nm_id}/detail.aspx" '
                     f'target="_blank" rel="noopener noreferrer" title="{title}">{title}</a>'
                     f'<div class="product-footer"><span class="product-number">{shown}</span>'
                     f'<span class="product-delta {tone}">{change}</span></div>'
                     '<div class="product-hint">К предыдущему сбору в периоде</div></div>')
    st.markdown('<div class="product-grid">' + ''.join(cards) + '</div>', unsafe_allow_html=True)


def show_query_management(cfg: Settings, queries: list[dict]) -> None:
    st.markdown('#### Поисковые запросы')
    with st.form('add_query', clear_on_submit=True):
        phrase = st.text_input('Новый поисковый запрос', max_chars=300,
                               placeholder='Например, постельное белье лен евро')
        if st.form_submit_button('Добавить запрос', type='primary'):
            try:
                query_id, created = Store(cfg.database_path).add_query(phrase)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state['pending_query_id'] = query_id
                st.session_state['query_notice'] = (
                    'Запрос добавлен.' if created else 'Сбор запроса возобновлён.')
                st.rerun()
    st.markdown('##### Приостановить или возобновить сбор')
    options = {q['id']: q for q in queries}
    target_id = st.selectbox('Запрос для управления', options=list(options), key='managed_query',
                             format_func=lambda qid: options[qid]['phrase'])
    target = options[target_id]
    action = 'Поставить на паузу' if target['enabled'] else 'Возобновить сбор'
    if st.button(action, key='toggle_query'):
        Store(cfg.database_path).set_query_enabled(target_id, not bool(target['enabled']))
        st.session_state['query_notice'] = (
            'Запрос на паузе. История сохранена.' if target['enabled'] else 'Сбор запроса включён.')
        st.rerun()
    table = pd.DataFrame([{
        'Запрос': q['phrase'],
        'Сбор': 'Каждый час' if q['enabled'] else 'Пауза',
        'Последняя попытка': display_time(q['last_attempt']),
        'Товаров': q['last_count'],
    } for q in queries])
    st.dataframe(table, hide_index=True, width='stretch', height=440, placeholder='—')


def show_history(store: Store, query_id: int, latest_rows: list[dict], product_total: int,
                 top_n: int, start_date: date | None, end_date: date | None) -> int | None:
    if not product_total:
        st.info("Товаров пока нет. Результаты запусков — на вкладке «Сборы».")
        return
    chosen_key = f"chosen_products_{query_id}"
    if chosen_key not in st.session_state:
        st.session_state[chosen_key] = [row["nm_id"] for row in latest_rows[:3]]
    with st.expander("Выбрать товары"):
        search_key = f"catalog_search_{query_id}"
        applied_key = f"catalog_filter_{query_id}"
        with st.form(f"find_product_{query_id}"):
            search = st.text_input("Название или артикул", key=search_key,
                                   placeholder="Найти товар в истории запроса")
            if st.form_submit_button("Найти"):
                st.session_state[applied_key] = search
        term = st.session_state.get(applied_key, "")
        matches = store.search_products(query_id, term, limit=250)
        retained = store.products_by_ids(query_id, st.session_state[chosen_key])
        product_map = {p["nm_id"]: p for p in [*matches, *retained]}

        def product_label(nm_id: int) -> str:
            return f"{product_map[nm_id]['last_title'] or 'Без названия'} [{nm_id}]"

        if matches:
            match_ids = [p["nm_id"] for p in matches]
            # A fixed selectbox key can keep an obsolete formatted label when
            # search results change. Give each result set its own widget identity.
            fingerprint = sha256(repr([product_label(nm) for nm in match_ids]).encode()).hexdigest()[:16]
            result_key = f"catalog_result_{query_id}_{fingerprint}"
            found_id = st.selectbox("Найденные товары", options=match_ids,
                                    format_func=product_label, key=result_key)

            def add_to_chart(nm_id: int) -> None:
                chosen = list(st.session_state[chosen_key])
                if nm_id not in chosen and len(chosen) < 30:
                    st.session_state[chosen_key] = [*chosen, nm_id]

            st.button("Добавить на график", key=f"add_product_{query_id}",
                      on_click=add_to_chart, args=(found_id,),
                      disabled=found_id in st.session_state[chosen_key]
                      or len(st.session_state[chosen_key]) >= 30)
            if term:
                st.caption(f"Найдено товаров: {len(matches)}" if len(matches) < 250
                           else "Показаны первые 250 совпадений. Уточните поиск.")
        else:
            st.info("Товары не найдены. Попробуйте другое название или артикул.")
        selection_version = sha256(repr(st.session_state[chosen_key]).encode()).hexdigest()[:16]
        selected = st.multiselect(
            "На графике", options=[p["nm_id"] for p in retained],
            default=st.session_state[chosen_key], key=f"product_selection_{query_id}_{selection_version}",
            format_func=lambda nm: f"{nm}: {product_map[nm]['last_title'] or 'Без названия'}",
            max_selections=30,
        )
        # The version applies additions to the widget while preserving removals
        # from its client value. A fixed key would keep the old selection.
        st.session_state[chosen_key] = selected
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
    color_key = f"product_colors_{query_id}"
    colors = st.session_state.setdefault(color_key, {})
    for nm_id in selected:
        if nm_id not in colors:
            colors[nm_id] = len(colors)
    titles = {nm: product_map[nm]['last_title'] or f'Артикул {nm}' for nm in selected}
    mode = "position" if metric == "Позиции" else "price"
    if any(row[field] is not None for row in history):
        with st.container(key="history-panel"):
            chart = chart_product_history(history, selected, metric=mode, titles=titles, color_indices=colors)
            chart.update_layout(legend={"orientation": "h", "y": -0.23, "x": 0},
                                margin={"l": 8, "r": 8, "t": 12, "b": 30})
            st.plotly_chart(chart, width="stretch", theme=None,
                           config={"displayModeBar": False, "responsive": True, "scrollZoom": False})
    else:
        st.info("Выбранные товары не найдены в снимках за этот период." if field == "position"
                else "Цены выбранных товаров за этот период недоступны.")
    show_product_cards(product_summaries(history, selected, metric=mode), titles, colors, mode)
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
        st.caption(f"{display_time(before['started_at'])} → {display_time(after['started_at'])}")
        counts = {status: sum(p["change"] == status for p in products)
                  for status in ("up", "down", "entered", "left", "unchanged")}
        with st.container(key="movement-summary"):
            cards = []
            for status, label, tone in [("up", "Поднялись", "positive"),
                                       ("down", "Опустились", "negative"),
                                       ("entered", "Вошли в топ", ""),
                                       ("left", "Покинули топ", "")]:
                cards.append(f'<div class="movement-stat {tone}"><span>{label}</span>'
                             f'<strong>{counts[status]}</strong></div>')
            st.markdown('<div class="movement-grid">' + ''.join(cards) + '</div>',
                        unsafe_allow_html=True)
        st.caption(f"Без изменений: {counts['unchanged']}")
        view = st.radio("Сравнение выдачи", ["Перемещения товаров", "Обновление топа"],
                         horizontal=True, key=f"changes_view_{query_id}", label_visibility="collapsed")
        changed = counts["up"] + counts["down"]
        if view == "Перемещения товаров":
            if changed:
                st.plotly_chart(chart_rank_changes(products), width="stretch", theme=None,
                                config={"displayModeBar": False, "responsive": True, "scrollZoom": False})
                if changed > 12:
                    st.caption("12 наибольших перемещений")
            else:
                st.info("Позиции оставшихся в топе товаров не изменились.")
        else:
            turnover = store.snapshot_turnover(query_id, depth=depth,
                        since=to_date_bound(start_date, end=False), until=to_date_bound(end_date, end=True))
            st.markdown(f"##### Обновление топ-{depth}")
            st.caption("Доля новых товаров относительно предыдущей полной выдачи")
            st.plotly_chart(chart_turnover(turnover), width="stretch", theme=None,
                            config={"displayModeBar": False, "responsive": True, "scrollZoom": False})
        labels = {"up": "Поднялся", "down": "Опустился", "entered": "Вошёл в топ",
                  "left": "Вышел из топа", "unchanged": "Без изменений"}
        table = pd.DataFrame(products)
        table["change"] = table["change"].map(labels)
        for column in ("before", "after", "delta"):
            table[column] = table[column].astype("Int64")
        table = table.rename(columns={"nm_id": "Артикул", "title": "Название", "before": "Было",
                    "after": "Стало", "delta": "Изменение", "change": "Статус"})
        st.dataframe(table, hide_index=True, width="stretch", height=320, placeholder="—",
                     column_config={"Изменение": st.column_config.NumberColumn(format="%+d")})
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
        queries = store.queries_dashboard(include_paused=True)
    except Exception as exc:
        st.error(f"Не удалось открыть базу данных: {exc}")
        st.stop()
    if not queries:
        st.info("Пока нет запросов. Запустите сборщик.")
        st.stop()

    with st.sidebar:
        st.markdown('<div class="brand"><div class="brand-mark">WB</div><div><strong>SERP</strong>'
                    '<small>Позиции и динамика товаров</small></div></div>', unsafe_allow_html=True)
        lookup = {q["id"]: q for q in queries}
        if "pending_query_id" in st.session_state:
            pending = st.session_state.pop("pending_query_id")
            st.session_state["selected_query"] = pending
            st.session_state["managed_query"] = pending
        query_id = st.selectbox("Поисковый запрос", options=list(lookup), key="selected_query",
                                format_func=lambda qid: lookup[qid]["phrase"]
                                + (" (пауза)" if not lookup[qid]["enabled"] else ""))
        st.divider()
        st.markdown("**Период графика**")
        today = datetime.now(MSK).date()
        start_date = st.date_input("График с", value=today - timedelta(days=14),
                                   key="history_start", max_value=today, format="DD.MM.YYYY")
        end_date = st.date_input("По", value=today, key="history_end",
                                 max_value=today, format="DD.MM.YYYY")
        if st.button("Обновить", width="stretch", help="Перечитать сохранённые данные"):
            st.rerun()
        active_count = sum(q['enabled'] for q in queries)
        next_at = next_collection(datetime.now(timezone.utc), cfg.collection_period_seconds).astimezone(MSK)
        st.markdown(f'<div class="schedule">Следующий сбор<strong>{next_at:%H:%M} МСК</strong>'
                    f'<span>Активных запросов: {active_count}</span></div>', unsafe_allow_html=True)

    query = lookup[query_id]
    latest_rows = store.latest_observations(query_id, include_partial=True)
    runs = store.recent_runs(query_id, limit=200)
    product_total = store.product_count(query_id)
    with st.container(key="query-header"):
        notice = st.session_state.pop("query_notice", None)
        if notice:
            st.success(notice)
        st.markdown('<div class="context">ПОЗИЦИИ ТОВАРОВ</div>', unsafe_allow_html=True)
        st.markdown(f"<h1>{html.escape(query['phrase'])}</h1>", unsafe_allow_html=True)
        if not query['enabled']:
            st.info("Сбор на паузе")
        if latest_rows:
            latest = latest_rows[0]
            st.caption(f"Обновлено {display_time(latest['started_at'])}")
            if latest["run_status"] == "partial":
                st.warning(f"Неполный сбор: {len(latest_rows)} из {latest['requested_top_n']} товаров.")
        else:
            st.caption("Ожидается первый сбор")
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
                st.metric("Сборов", str(store.count_history_runs(query_id, include_partial=True)))

    overview, snapshot, operations, management = st.tabs(["Позиции", "Товары", "Сборы", "Запросы"])
    with overview:
        depth = show_history(store, query_id, latest_rows, product_total, cfg.top_n, start_date, end_date)
        if depth is not None and start_date is not None and end_date is not None and start_date <= end_date:
            show_changes(store, query_id, depth, start_date, end_date)
    with snapshot:
        st.markdown("#### Последняя выдача")
        st.caption("Нажмите на ссылку в таблице, чтобы открыть товар на Wildberries.")
        if latest_rows:
            table = snapshot_table(latest_rows)
            display = table.assign(Товар=table["Артикул"].map(lambda nm: f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"))
            st.dataframe(display, column_order=["Позиция", "Артикул", "Название", "Цена, ₽", "Рейтинг",
                         "Отзывы", "Доставка, дни", "Продавец", "Товар"], width="stretch", hide_index=True,
                         height=560, placeholder="—", column_config={
                "Товар": st.column_config.LinkColumn(display_text="Открыть ↗"),
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
        st.markdown("#### Состояние запросов")
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

    with management:
        show_query_management(cfg, queries)


if __name__ == "__main__":
    main()
