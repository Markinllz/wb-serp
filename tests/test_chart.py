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
