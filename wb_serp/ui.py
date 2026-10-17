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
from wb_serp.runtime import read_worker_status

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
    table['card_price_rub'] = table.pop('card_price_kopecks') / 100
    table['card_price_kind'] = table['card_price_kind'].map(
        {'wallet':'С WB Кошельком', 'displayed':'Не указаны'})
    table["started_at"] = table["started_at"].map(display_time)
    table["run_status"] = table["run_status"].map(STATUS_LABELS)
    for column in ("position", "nm_id", "reviews", "delivery_days"):
        table[column] = table[column].astype("Int64")
    return table.rename(columns={
        "position": "Позиция", "nm_id": "Артикул", "title": "Название", "price_rub": "Без Кошелька, ₽",
        "card_price_rub": "Цена на WB, ₽", "card_price_kind": "Условия цены",
        "rating": "Рейтинг", "reviews": "Отзывы", "delivery_days": "Доставка, дни",
        "seller": "Продавец", "started_at": "Время", "requested_top_n": "Глубина",
        "run_status": "Статус",
    })


def live_collection(cfg: Settings) -> tuple[dict, dict]:
    runtime = read_worker_status(cfg.database_path)
    now = datetime.now(timezone.utc)
    slot = runtime.get("scheduled_for") or datetime.fromtimestamp(
        int(now.timestamp()) // cfg.collection_period_seconds * cfg.collection_period_seconds,
        timezone.utc).isoformat(timespec="seconds")
    stats = Store(cfg.database_path, readonly=True).collection_stats(
        slot=slot, depth=runtime.get("top_n", cfg.top_n), query_ids=runtime.get("query_ids"))
    return runtime, stats


def number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


@st.fragment(run_every="5s")
def show_live_status(cfg: Settings) -> None:
    runtime, stats = live_collection(cfg)
    options = Store(cfg.database_path, readonly=True).collection_options(cfg.top_n)
    if runtime.get('alive'):
        if runtime['state'] == 'collecting':
            label, tone = ('Завершается текущий запрос' if not options['enabled'] else 'Идёт сбор'), 'busy'
        elif not options['enabled']:
            label, tone = 'Сбор на паузе', 'ready'
        elif stats['counts']['blocked']:
            label, tone = 'WB ограничил сбор', 'error'
        elif stats['counts']['failed'] or stats['counts']['interrupted']:
            label, tone = 'Сбор завершён с ошибками', 'error'
        else:
            label, tone = 'Ожидание следующего сбора', 'ready'
    else:
        label = {'stopped': 'Сборщик остановлен', 'error': 'Ошибка сборщика',
                 'unknown': 'Нет связи со сборщиком'}.get(runtime.get('state'), 'Нет связи со сборщиком')
        tone = 'error'
    current = stats['current_query'] if runtime.get('alive') and runtime['state'] == 'collecting' else None
    progress = ''
    if stats['target'] and (runtime.get('scheduled_for') or stats['processed']):
        hour = datetime.fromisoformat(stats['slot']).astimezone(MSK).strftime('%H:%M')
        progress = (f'<div class="live-progress">Сбор за {hour}<strong>{stats["processed"]} из {stats["target"]}</strong></div>'
                    f'<progress aria-label="Обработано запросов" value="{stats["processed"]}" max="{stats["target"]}"></progress>'
                    f'<small>Сохранено в этом сборе: {number(stats["cycle_observations"])}</small>')
    if current:
        progress += f'<p class="live-query" title="{html.escape(current)}">{html.escape(current)}</p>'
    latest = display_time(stats['last_saved_at'])
    next_at = next_collection(datetime.now(timezone.utc), cfg.collection_period_seconds).astimezone(MSK)
    next_label = f'Следующий сбор <strong>{next_at:%H:%M} МСК</strong>' if options['enabled'] else 'Автоматический сбор приостановлен'
    st.markdown(f'<div class="live-panel"><div class="live-title">Сбор данных</div>'
                f'<div class="live-status {tone}"><i></i>{label}</div>'
                f'<div class="live-totals"><div><span>Наблюдений</span><strong>{number(stats["observations"])}</strong></div>'
                f'<div><span>Товаров</span><strong>{number(stats["products"])}</strong></div></div>'
                f'{progress}<div class="live-saved">Последнее сохранение<br>{latest}</div>'
                f'<div class="live-next">{next_label}</div></div>',
                unsafe_allow_html=True)


@st.fragment(run_every="10s")
def show_collection_overview(cfg: Settings, query_id: int | None) -> None:
    store = Store(cfg.database_path, readonly=True)
    _, stats = live_collection(cfg)
    st.markdown("#### Накопленные данные")
    columns = st.columns(4)
    for column, label, field, help_text in zip(columns,
            ('Наблюдений', 'Уникальных товаров', 'Снимков выдачи', 'Активных запросов'),
            ('observations', 'products', 'snapshots', 'active_queries'),
            ('Один товар в одном полном или неполном сборе. Ошибочные попытки не учитываются.',
             'Разные артикулы во всех запросах, без повторного подсчёта.',
             'Сохранённые полные и неполные выдачи с товарами.', 'Запросы с включённым сбором.'), strict=True):
        with column:
            st.metric(label, number(stats[field]), help=help_text)
    queries = store.queries_dashboard(include_paused=True)
    st.markdown("#### Состояние запросов")
    if not queries:
        st.info("Добавьте поисковый запрос в разделе «Запросы».")
        return
    monitor = pd.DataFrame(queries)
    monitor["last_status"] = monitor["last_status"].map(STATUS_LABELS).fillna("Нет запусков")
    for column in ("last_attempt", "last_success"):
        monitor[column] = monitor[column].map(display_time)
    monitor["last_count"] = monitor["last_count"].astype("Int64")
    monitor = monitor.rename(columns={"phrase": "Запрос", "last_status": "Статус",
        "last_attempt": "Последний запуск", "last_success": "Последний полный", "last_count": "Товаров"})
    st.dataframe(monitor[["Запрос", "Статус", "Товаров", "Последний запуск", "Последний полный"]],
                 hide_index=True, width="stretch", height=350, placeholder="—")
    st.markdown("#### История сборов")
    query_map = {q['id']: q['phrase'] for q in queries}
    query_id = st.selectbox('Запрос для истории сборов', options=list(query_map),
                           index=list(query_map).index(query_id) if query_id in query_map else 0,
                           key='runs_query', format_func=query_map.get)
    runs = store.recent_runs(query_id, limit=200) if query_id else []
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
    store = Store(cfg.database_path)
    st.markdown('## Поисковые запросы')
    with st.form('add_query', clear_on_submit=True):
        phrase = st.text_input('Новый поисковый запрос', max_chars=300,
                               placeholder='Например, постельное белье лен евро')
        if st.form_submit_button('Добавить запрос', type='primary'):
            try:
                query_id, created = store.add_query(phrase)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state['pending_query_id'] = query_id
                st.session_state['query_notice'] = 'Запрос добавлен.' if created else 'Сбор запроса включён.'
                st.rerun()
    if queries:
        options = {q['id']: q for q in queries}
        target_id = st.selectbox('Выберите запрос', options=list(options), key='managed_query',
                                format_func=lambda qid: options[qid]['phrase'])
        target = options[target_id]
        first, second, third = st.columns(3)
        with first:
            if st.button('Открыть аналитику', key='open_query', width='stretch', type='primary'):
                st.session_state['pending_query_id'] = target_id
                st.session_state['pending_view'] = 'Аналитика'
                st.rerun()
        with second:
            if st.button('Приостановить запрос' if target['enabled'] else 'Включить запрос',
                         key='toggle_query', width='stretch'):
                store.set_query_enabled(target_id, not bool(target['enabled']))
                st.rerun()
        with third:
            if st.button('Удалить запрос', key='archive_query', width='stretch'):
                store.archive_query(target_id)
                st.session_state['query_notice'] = 'Запрос удалён из списка. Его можно восстановить ниже.'
                st.rerun()
        table = pd.DataFrame([{'Запрос': q['phrase'],
            'Сбор': 'Каждый час' if q['enabled'] else 'Пауза',
            'Последнее обновление': display_time(q['last_attempt']), 'Товаров': q['last_count']}
            for q in queries])
        st.dataframe(table, hide_index=True, width='stretch', height=440, placeholder='—')
    archived = [q for q in store.queries_dashboard(include_paused=True, include_archived=True) if q['archived']]
    if archived:
        with st.expander('Удалённые запросы'):
            items = {q['id']: q['phrase'] for q in archived}
            archived_id = st.selectbox('Запрос для восстановления', options=list(items), format_func=items.get)
            if st.button('Восстановить запрос'):
                store.archive_query(archived_id, False)
                st.session_state['pending_query_id'] = archived_id
                st.session_state['query_notice'] = 'Запрос восстановлен на паузе. История доступна в аналитике.'
                st.rerun()


def show_collection_controls(cfg: Settings) -> None:
    store = Store(cfg.database_path)
    options = store.collection_options(cfg.top_n)
    st.markdown('## Сбор данных')
    runtime = read_worker_status(cfg.database_path)
    if options['enabled']:
        if st.button('Остановить сбор', type='primary', key='stop_collection'):
            store.set_collection_options(enabled=False)
            st.rerun()
        st.caption('Остановка завершает текущий запрос и ставит следующие сборы на паузу.')
    else:
        st.info('Завершается текущий запрос.' if runtime.get('alive') and runtime.get('state') == 'collecting'
                else 'Сбор остановлен. История доступна для просмотра.')
        if st.button('Возобновить сбор', type='primary', key='resume_collection'):
            store.set_collection_options(enabled=True)
            st.rerun()
    if 'collection_top_n' not in st.session_state:
        st.session_state['collection_top_n'] = options['top_n']
    with st.form('collection_depth'):
        depth = st.number_input('Товаров на один запрос', min_value=1, max_value=1000,
                                key='collection_top_n', step=50)
        if st.form_submit_button('Сохранить глубину'):
            store.set_collection_options(top_n=int(depth))
            st.success('Глубина сохранена. Применится к следующему циклу.')
    st.caption('Можно собирать от 1 до 1000 товаров. Если WB вернёт меньше, сохранятся доступные товары.')


def show_history(store: Store, query_id: int, latest_rows: list[dict], product_total: int,
                 top_n: int, start_date: date | None, end_date: date | None) -> int | None:
    if not product_total:
        st.info("Товаров пока нет. Результаты запусков доступны в разделе «Сбор данных».")
        return
    chosen_key = f"chosen_products_{query_id}"
    if chosen_key not in st.session_state:
        st.session_state[chosen_key] = [row["nm_id"] for row in latest_rows[:3]]
    st.markdown("### 1. Выберите товары")
    with st.container(key="product-picker"):
        search_key = f"catalog_search_{query_id}"
        applied_key = f"catalog_filter_{query_id}"
        with st.form(f"find_product_{query_id}"):
            search_column, find_column = st.columns([5, 1], vertical_alignment='bottom')
            with search_column:
                search = st.text_input("Название или артикул", key=search_key,
                                       placeholder="Найти товар в истории запроса")
            with find_column:
                if st.form_submit_button("Найти", width='stretch'):
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
            result_column, add_column = st.columns([4, 2], vertical_alignment='bottom')
            with result_column:
                found_id = st.selectbox("Результаты поиска", options=match_ids,
                                        format_func=product_label, key=result_key)

            def add_to_chart(nm_id: int) -> None:
                chosen = list(st.session_state[chosen_key])
                if nm_id not in chosen and len(chosen) < 30:
                    st.session_state[chosen_key] = [*chosen, nm_id]

            with add_column:
                st.button("Уже выбран" if found_id in st.session_state[chosen_key] else "Добавить на график",
                          key=f"add_product_{query_id}", width='stretch',
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
            "Выбранные товары", options=[p["nm_id"] for p in retained],
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
    st.markdown("### 2. Динамика выбранных товаров")
    metric = st.radio("Показатель", ["Позиции", "Цены"], horizontal=True,
                       key=f"history_metric_{query_id}", label_visibility="collapsed")
    price_field = 'price_kopecks'
    if metric == 'Цены':
        basis = st.radio('Цена', ['На карточке WB', 'Без WB Кошелька'], horizontal=True,
                         index=0 if any(r.get('card_price_kopecks') is not None for r in history) else 1,
                         key=f'price_basis_{query_id}')
        price_field = 'card_price_kopecks' if basis == 'На карточке WB' else 'price_kopecks'
        if price_field == 'card_price_kopecks' and any(r.get('card_price_kind') == 'wallet' for r in history):
            st.caption('Цена с WB Кошельком действует при оплате Кошельком.')
    field = "position" if metric == "Позиции" else price_field
    color_key = f"product_colors_{query_id}"
    colors = st.session_state.setdefault(color_key, {})
    for nm_id in selected:
        if nm_id not in colors:
            colors[nm_id] = len(colors)
    titles = {nm: product_map[nm]['last_title'] or f'Артикул {nm}' for nm in selected}
    mode = "position" if metric == "Позиции" else "price"
    if any(row[field] is not None for row in history):
        with st.container(key="history-panel"):
            chart = chart_product_history(history, selected, metric=mode, titles=titles, color_indices=colors, price_field=price_field)
            chart.update_layout(legend={"orientation": "h", "y": -0.23, "x": 0},
                                margin={"l": 8, "r": 8, "t": 12, "b": 30})
            st.plotly_chart(chart, width="stretch", theme=None,
                           config={"displayModeBar": False, "responsive": True, "scrollZoom": False})
    else:
        st.info("Выбранные товары не найдены в снимках за этот период." if field == "position"
                else "Цены выбранных товаров за этот период недоступны.")
    show_product_cards(product_summaries(history, selected, metric=mode, price_field=price_field), titles, colors, mode)
    latest_products = {}
    for row in history:
        if row['position'] is not None:
            latest_products[row['nm_id']] = row
    st.markdown('### 3. Характеристики выбранных товаров')
    if latest_products:
        attributes = snapshot_table([latest_products[nm] for nm in selected if nm in latest_products])
        st.dataframe(attributes, column_order=['Артикул', 'Название', 'Время', 'Позиция', 'Цена на WB, ₽', 'Условия цены', 'Без Кошелька, ₽',
            'Рейтинг', 'Отзывы', 'Доставка, дни', 'Продавец'], hide_index=True, width='stretch', placeholder='—')
        st.download_button('Скачать характеристики CSV', attributes.to_csv(index=False).encode('utf-8-sig'),
                           'selected_products.csv', mime='text/csv', on_click='ignore')
    export = pd.DataFrame(history)
    for column in ("position", "price_kopecks", "card_price_kopecks", "reviews", "delivery_days"):
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
        st.error('База ещё не инициализирована. Запустите сборщик.')
        st.stop()
    try:
        queries = store.queries_dashboard(include_paused=True)
    except Exception as exc:
        st.error(f'Не удалось открыть базу данных: {exc}')
        st.stop()
    lookup = {q['id']: q for q in queries}
    if 'pending_view' in st.session_state:
        st.session_state['view'] = st.session_state.pop('pending_view')
    if 'pending_query_id' in st.session_state:
        pending = st.session_state.pop('pending_query_id')
        if pending in lookup:
            st.session_state['selected_query'] = pending
            st.session_state['managed_query'] = pending
    for key in ('selected_query', 'managed_query'):
        if st.session_state.get(key) not in lookup:
            st.session_state.pop(key, None)
        else:
            # Keep selections when their widgets are absent on another page.
            st.session_state[key] = st.session_state[key]
    with st.sidebar:
        st.markdown('<div class="brand"><div class="brand-mark">WB</div><div><strong>SERP</strong>'
                    '<small>Позиции и динамика товаров</small></div></div>', unsafe_allow_html=True)
        view = st.radio('Раздел', ['Аналитика', 'Запросы', 'Сбор данных'], key='view', label_visibility='collapsed')
        st.divider()
        show_live_status(cfg)
        if st.button('Обновить данные', width='stretch'):
            st.rerun()
    notice = st.session_state.pop('query_notice', None)
    if notice:
        st.success(notice)
    if view == 'Запросы':
        show_query_management(cfg, queries)
        return
    if view == 'Сбор данных':
        show_collection_controls(cfg)
        qid = st.session_state.get('selected_query', next(iter(lookup), None))
        show_collection_overview(cfg, qid)
        return
    st.markdown('## Аналитика товаров')
    if not queries:
        st.info('Пока нет запросов. Добавьте первый в разделе «Запросы».')
        return
    query_id = st.selectbox('Поисковый запрос', options=list(lookup), key='selected_query',
                            format_func=lambda qid: lookup[qid]['phrase']
                            + (' (пауза)' if not lookup[qid]['enabled'] else ''))
    query = lookup[query_id]
    latest_rows = store.latest_observations(query_id, include_partial=True)
    runs = store.recent_runs(query_id, limit=200)
    product_total = store.product_count(query_id)
    if not query['enabled']:
        st.info('Сбор этого запроса на паузе.')
    if latest_rows:
        latest = latest_rows[0]
        st.caption(f"Обновлено {display_time(latest['started_at'])}")
        if latest['run_status'] == 'partial':
            st.warning(f"Неполный сбор: {len(latest_rows)} из {latest['requested_top_n']} товаров.")
    if runs and runs[0]['status'] in ('failed', 'blocked', 'interrupted'):
        st.warning('Последняя попытка не завершена. Подробности в разделе «Сбор данных».')
    with st.container(key='summary'):
        for column, label, value in zip(st.columns(3), ['Товаров в последней выдаче', 'Товаров за всё время', 'Сохранённых выдач'],
                [len(latest_rows) if latest_rows else '—', number(product_total),
                 store.count_history_runs(query_id, include_partial=True)], strict=True):
            with column:
                st.metric(label, value)
    overview, snapshot = st.tabs(['Динамика товаров', 'Вся выдача'])
    with overview:
        today = datetime.now(MSK).date()
        left, right = st.columns(2)
        with left:
            start_date = st.date_input('График с', value=today - timedelta(days=14), key='history_start',
                                      max_value=today, format='DD.MM.YYYY')
        with right:
            end_date = st.date_input('По', value=today, key='history_end', max_value=today, format='DD.MM.YYYY')
        depth = show_history(store, query_id, latest_rows, product_total,
                             store.collection_options(cfg.top_n)['top_n'], start_date, end_date)
        if depth is not None and start_date is not None and end_date is not None and start_date <= end_date:
            show_changes(store, query_id, depth, start_date, end_date)
    with snapshot:
        st.markdown('### Последняя выдача')
        if latest_rows:
            table = snapshot_table(latest_rows)
            display = table.assign(Товар=table['Артикул'].map(lambda nm: f'https://www.wildberries.ru/catalog/{nm}/detail.aspx'))
            st.dataframe(display, column_order=['Позиция', 'Артикул', 'Название', 'Цена на WB, ₽', 'Условия цены', 'Без Кошелька, ₽', 'Рейтинг',
                'Отзывы', 'Доставка, дни', 'Продавец', 'Товар'], width='stretch', hide_index=True,
                height=560, placeholder='—', column_config={
                    'Товар': st.column_config.LinkColumn(display_text='Открыть ↗'),
                    'Артикул': st.column_config.NumberColumn(format='%d'),
                    'Цена на WB, ₽': st.column_config.NumberColumn(format='%.2f ₽'),
                    'Без Кошелька, ₽': st.column_config.NumberColumn(format='%.2f ₽'),
                    'Доставка, дни': st.column_config.NumberColumn(format='%d')})
            st.download_button('Скачать выдачу CSV', table.to_csv(index=False).encode('utf-8-sig'),
                               'latest_serp.csv', mime='text/csv', on_click='ignore')
        else:
            st.info('Сохранённых товаров пока нет.')


if __name__ == '__main__':
    main()
