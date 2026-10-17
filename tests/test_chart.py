from wb_serp.charts import chart_rank_history


def test_inverted_axis_and_disconnect_nulls():
    hist = [
        {"nm_id": 1, "started_at": "2026-10-14T12:00:00+00:00", "position": 1, "requested_top_n": 200},
        {"nm_id": 1, "started_at": "2026-10-15T12:00:00+00:00", "position": None, "requested_top_n": 200},
        {"nm_id": 1, "started_at": "2026-10-16T12:00:00+00:00", "position": 8, "requested_top_n": 200},
    ]
    chart = chart_rank_history(hist, [1])
    assert chart.layout.yaxis.autorange == "reversed"
    assert chart.data[0].connectgaps is False
    assert list(chart.data[0].y) == [1, None, 8]


def test_narrow_rank_range_has_integer_ticks_and_partial_marker():
    hist = [{'nm_id': 1, 'started_at': '2026-10-16T12:00:00+00:00', 'position': i,
             'requested_top_n': 200, 'run_status': 'partial' if i == 2 else 'success'}
            for i in (1, 2, 3)]
    chart = chart_rank_history(hist, [1])
    assert chart.layout.yaxis.dtick == 1
    assert list(chart.data[0].marker.symbol) == ['circle', 'diamond-open', 'circle']
    assert chart.data[0].customdata[1][2] == 'Неполный сбор'


def test_price_graph_keeps_zero_nulls_and_kopeck_precision():
    from wb_serp.charts import chart_product_history
    hist = [{'nm_id': 7, 'started_at': '2026-10-16T12:00:00+00:00', 'position': 1,
             'requested_top_n': 200, 'price_kopecks': price} for price in (0, None, 10099)]
    chart = chart_product_history(hist, [7], metric='price')
    assert list(chart.data[0].y) == [0, None, 100.99]
    assert chart.data[0].connectgaps is False
    assert chart.layout.yaxis.autorange is True
    assert chart.layout.yaxis.title.text == 'Цена, ₽'


def test_movers_chart_only_uses_observed_numeric_changes():
    from wb_serp.charts import chart_rank_changes
    products = [{'nm_id': 1, 'before': 20, 'after': 5, 'delta': 15},
                {'nm_id': 2, 'before': 5, 'after': 20, 'delta': -15},
                {'nm_id': 3, 'before': None, 'after': 1, 'delta': None},
                {'nm_id': 4, 'before': 10, 'after': None, 'delta': None},
                {'nm_id': 5, 'before': 3, 'after': 3, 'delta': 0}]
    chart = chart_rank_changes(products)
    assert list(chart.data[0].y) == ['2', '1']
    assert list(chart.data[0].x) == [-15, 15]
    assert chart.data[0].customdata[0] == ['2', 5, 20]
